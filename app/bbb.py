"""BBB Business Scraper — bbb.org search results.

bbb.org renders a Next/React page but embeds the full search results as JSON in
`window.__PRELOADED_STATE__` (`searchResult.results`), so we just extract that — no HTML scraping.
All traffic goes through a proxy (NEVER the real IP): a paid PROXY_URL if set, otherwise the free
US pool — which BBB usually 403-blocks, so BBB returns 0/blocked on the free tier until a paid
PROXY_URL is set. A query may be a plain search term OR any bbb.org URL (search / category page).
"""
import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse, parse_qs, urlencode

from curl_cffi import requests as cffi

from .config import settings

BASE = "https://www.bbb.org"
MAX_PAGES = 15        # BBB caps its result pages at 15
_BBB_TIMEOUT = 12     # short per-proxy timeout so blocked/dead proxies fail fast
_GOOD_PROXY = None    # last proxy that passed bbb.org — reused before re-rotating
_PIN_LOCK = threading.Lock()


def _ok(r) -> bool:
    """A real BBB page (not a 403 block) — has the embedded results JSON."""
    return r is not None and r.status_code == 200 and "__PRELOADED_STATE__" in r.text


def _try(url: str, px: str):
    try:
        r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                     timeout=_BBB_TIMEOUT, verify=False, allow_redirects=True)
        return r if _ok(r) else None
    except Exception:
        return None


def _proxied_get(url: str):
    """Fetch through a free proxy (NEVER the real IP). bbb.org 403s only SOME proxies, so: reuse the
    last known-good proxy if we have one, otherwise rotate the pool until one passes and pin it.
    Raises if none pass."""
    global _GOOD_PROXY
    pinned = _GOOD_PROXY
    if pinned:                       # fast path: the proxy that worked last time
        r = _try(url, pinned)
        if r is not None:
            return r
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 8)
    seen, candidates = {pinned}, []
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px not in seen:
            seen.add(px)
            candidates.append(px)
    for px in candidates[:15]:
        r = _try(url, px)
        if r is not None:
            with yp_us._LOCK:        # promote so the pool reuses it too
                if px in yp_us._GOOD:
                    yp_us._GOOD.remove(px)
                yp_us._GOOD.insert(0, px)
            with _PIN_LOCK:
                _GOOD_PROXY = px     # pin for subsequent profile fetches
            return r
    raise RuntimeError("no free proxy passed bbb.org")


def _get(url: str):
    """Fetch through a proxy — NEVER the real IP. Paid PROXY_URL if set, else rotate the free pool
    (bbb.org 403s only some free proxies, so we try several until one passes)."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                        timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
    return _proxied_get(url)


def _search_url(query: str, page: int) -> str:
    """A query may be a full bbb.org URL (use as-is, set page) or a search term (build /search)."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        u = urlparse(q)
        params = parse_qs(u.query)
        params["page"] = [str(page)]
        return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(params, doseq=True)}"
    url = f"{BASE}/search?find_text={quote(q)}&find_country=USA"
    return url + (f"&page={page}" if page > 1 else "")


def _clean(s):
    """BBB highlights matched terms with <em> tags in names — strip any HTML."""
    return re.sub(r"<[^>]+>", "", s).strip() if isinstance(s, str) else s


def _parse(html: str):
    """Return (rows, total_results, total_pages) from the page's __PRELOADED_STATE__ JSON."""
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});", html or "", re.S)
    if not m:
        return [], 0, 0
    try:
        sr = (json.loads(m.group(1)).get("searchResult") or {})
    except (ValueError, json.JSONDecodeError):
        return [], 0, 0
    out = []
    for b in sr.get("results") or []:
        if not isinstance(b, dict) or not b.get("businessName"):
            continue
        out.append({
            "name": _clean(b.get("businessName")),
            "category": b.get("tobText"),
            "rating": b.get("rating"),
            "accredited": "Yes" if b.get("bbbMember") else "No",
            "phone": (b.get("phone") or [None])[0],
            "address": b.get("address"),
            "city": b.get("city"),
            "state": b.get("state"),
            "postalcode": b.get("postalcode"),
            "service_area": b.get("serviceAreasSummary"),
            "url": (BASE + b["reportUrl"]) if b.get("reportUrl") else None,
            "images": b.get("logoUri") or None,   # business's own logo (when uploaded)
        })
    return out, sr.get("totalResults") or 0, sr.get("totalPages") or 0


def _profile_to_row(html: str, url: str) -> dict | None:
    """Build one full business row from a bbb.org PROFILE page (basic + deep fields)."""
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});", html or "", re.S)
    if not m:
        return None
    try:
        bp = (json.loads(m.group(1)).get("businessProfile") or {})
    except (ValueError, json.JSONDecodeError):
        return None
    if not bp:
        return None
    addr = (bp.get("location") or {}).get("postalAddress") or {}
    cats = [c.get("title") for c in ((bp.get("categories") or {}).get("links") or []) if c.get("title")]
    row = {
        "query": url,
        "name": _clean((bp.get("names") or {}).get("primary")),
        "category": ", ".join(cats[:2]) or None,
        "rating": (bp.get("rating") or {}).get("bbbRating"),
        "accredited": "Yes" if (bp.get("accreditationInformation") or {}).get("isAccredited") else "No",
        "phone": (bp.get("contactInformation") or {}).get("phoneNumber"),
        "address": addr.get("addressLine1"),
        "city": addr.get("city"),
        "state": addr.get("stateCode"),
        "postalcode": addr.get("zipCode"),
        "service_area": None,
        "url": (BASE + (bp.get("urls") or {}).get("profile", "")) if (bp.get("urls") or {}).get("profile") else url,
    }
    if not row["name"]:
        return None
    row.update({k: v for k, v in _parse_profile(html).items() if v not in (None, "", [])})
    return row


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """Scrape bbb.org for `query` — a search term, a search/category URL, or a single business
    profile URL. `limit` caps the rows (blank/None = all available, up to MAX_PAGES)."""
    q = (query or "").strip()
    if q.lower().startswith("http") and "/profile/" in q.lower():
        try:
            row = _profile_to_row(_proxied_get(q).text, q)
        except Exception:
            return []
        return [row] if row else []
    rows, page, last = [], 1, MAX_PAGES
    while page <= last:
        try:
            r = _get(_search_url(query, page))
        except Exception:
            break   # no proxy passed this round — finish quietly; the pool already rotated proxies
        if r.status_code != 200:
            break
        page_rows, _total, pages = _parse(r.text)
        if not page_rows:
            break
        last = min(MAX_PAGES, pages or 1)
        for x in page_rows:
            x["query"] = query
        rows += page_rows
        if limit and len(rows) >= limit:
            break
        page += 1
    return rows[:limit] if limit else rows


_COUNTRY = {1: "Canada", 2: "USA", 3: "Mexico"}


def _decode_email(s):
    """BBB obfuscates emails like '!~xK_bL!Info__at__iBuyStores__dot__com!~xK_bL!'."""
    if not isinstance(s, str) or not s:
        return None
    s = re.sub(r"!~[^!]*!", "", s).replace("__at__", "@").replace("__dot__", ".").strip()
    return s or None


def _date_year(s):
    m = re.match(r"(\d{4})", s or "")
    return m.group(1) if m else None


def _parse_profile(html: str) -> dict:
    """Pull the deep fields Outscraper exposes from a bbb.org profile page's __PRELOADED_STATE__."""
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});", html or "", re.S)
    if not m:
        return {}
    try:
        bp = (json.loads(m.group(1)).get("businessProfile") or {})
    except (ValueError, json.JSONDecodeError):
        return {}
    ci = bp.get("contactInformation") or {}
    org = bp.get("orgDetails") or {}
    dates = bp.get("dates") or {}
    rev = bp.get("reviewsComplaintsSummary") or {}
    loc = bp.get("location") or {}
    cats = [c.get("title") for c in ((bp.get("categories") or {}).get("links") or []) if c.get("title")]
    contacts = ci.get("contacts") or []
    contact = ""
    if contacts:
        nm = contacts[0].get("name") or {}
        contact = " ".join(x for x in (nm.get("prefix"), nm.get("first"), nm.get("last")) if x).strip()
        if contacts[0].get("title"):
            contact = f"{contact} ({contacts[0]['title']})".strip()
    lic = [l.get("name") or l.get("value") for l in ((org.get("license") or {}).get("details") or [])]
    out = {
        "profile_id": bp.get("id") or bp.get("businessId"),
        "type_of_entity": (org.get("typeOfEntity") or {}).get("name"),
        "num_employees": org.get("numEmployees") or None,
        "years_in_business": org.get("yearsInBusiness") or None,
        "business_start": _date_year(dates.get("businessStart")),
        "website": (bp.get("urls") or {}).get("primary"),
        "primary_category": cats[0] if cats else None,
        "all_categories": ", ".join(cats) or None,
        "contact_information": contact or None,
        "email": _decode_email(ci.get("emailAddress")),
        "additional_emails": ", ".join(filter(None, (e.get("value") for e in ci.get("additionalEmailAddresses") or []))) or None,
        "additional_phone": ", ".join(filter(None, (p.get("value") for p in ci.get("additionalPhoneNumbers") or []))) or None,
        "fax_numbers": ", ".join(filter(None, (f.get("value") for f in ci.get("additionalFaxNumbers") or []))) or None,
        "average_rating": rev.get("averageOfReviewStarRatings") or None,
        "reviews_total": rev.get("reviewsTotal") or None,
        "country": _COUNTRY.get(loc.get("countryCode")),
        "displayed_address": loc.get("formattedAddress"),
        "images": (bp.get("media") or {}).get("logo") or None,
        "licenses": ", ".join(filter(None, lic)) or None,
        "social_media": None,  # rarely present on BBB; left empty (also empty in Outscraper)
    }
    return out


def _enrich_one(r: dict) -> None:
    url = r.get("url")
    if not url or r.get("type_of_entity") or r.get("years_in_business"):
        return  # no url, or already enriched (e.g. a profile-URL query)
    extra = {}
    for _attempt in range(2):  # free proxies flaky — a couple of tries (pinned proxy helps)
        try:
            extra = _parse_profile(_proxied_get(url).text)
            if extra.get("website") or extra.get("years_in_business") or extra.get("type_of_entity"):
                break
        except Exception:
            continue
    for k, v in extra.items():
        if v not in (None, "", []):
            r[k] = v


def enrich_profiles_sync(rows: list[dict]) -> None:
    """Fetch each business's profile page (rotating proxy, no real IP) and merge the deep fields,
    CONCURRENTLY (the slow part is the per-business fetch). Best-effort — a blocked/failed profile
    just keeps the search-level fields."""
    targets = [r for r in rows if r.get("url")]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
        list(ex.map(_enrich_one, targets))


async def search(query: str, limit: int | None = None) -> list[dict]:
    rows = await asyncio.to_thread(search_sync, query, limit)
    await asyncio.to_thread(enrich_profiles_sync, rows)
    return rows


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    """Background task: scrape each query and store the result rows."""
    from datetime import datetime
    from .db import jobs, bbbresults
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await bbbresults.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
