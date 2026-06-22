"""Google Maps Data Scraper — keyless, by scraping Google Maps through a proxy.

No API key: it fetches google.com/maps/search/<query> through a proxy (NEVER the real IP) and
parses the page's embedded `APP_INITIALIZATION_STATE` data blob. Google blocks datacenter/free
IPs hard (429 / CAPTCHA), so this REQUIRES a paid (ideally residential) PROXY_URL — the free pool
cannot reach Maps. With no proxy it returns 0 with a setup note.

A query is a natural-language search like "restaurants in Surat, India" (the UI builds these from
categories × locations). The "Search by Domains" path searches the domain and matches results
whose website host equals the input domain.

NOTE: Google's blob is an obfuscated, unnamed nested-array format. Places are located by a
structural signature (name + coordinates) rather than fixed paths, which is resilient to most
shifts; the per-field indices (phone/hours/etc.) are best-effort and may need tuning against a
live response once a working proxy is available.
"""
import asyncio
import json
from datetime import datetime
from urllib.parse import quote, urlparse

from curl_cffi import requests as cffi

from .config import settings

GMAPS_COLUMNS = [
    "query", "name", "category", "address", "phone", "website", "rating", "reviews",
    "price_level", "business_status", "latitude", "longitude", "hours", "maps_url", "place_id",
]


def _blank_row():
    return {c: "" for c in GMAPS_COLUMNS}


# ---------------- fetch (paid proxy only — Google blocks free/datacenter IPs) ----------------

def _maps_url(query: str, region: str, language: str) -> str:
    return (f"https://www.google.com/maps/search/{quote(query)}/"
            f"?hl={language or 'en'}&gl={(region or 'us').lower()}&authuser=0")


def _fetch_sync(query: str, region: str, language: str) -> str | None:
    """Fetch a Google Maps search page through the paid proxy. Returns HTML or None."""
    proxy = settings.PROXY_URL.strip()
    if not proxy:
        return None
    try:
        r = cffi.get(_maps_url(query, region, language), impersonate="chrome",
                     headers={"Accept-Language": (language or "en")},
                     cookies={"CONSENT": "YES+"},
                     proxies={"http": proxy, "https": proxy},
                     timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
    except Exception:
        return None
    if r.status_code != 200 or "APP_INITIALIZATION_STATE" not in (r.text or ""):
        return None
    return r.text


# ---------------- blob extraction + place parsing ----------------

def _balanced(s: str, start: int) -> str | None:
    """Return the JSON array/object starting at `start` (bracket-matched, string-aware)."""
    open_ch = s[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_str = esc = False
    for j in range(start, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:j + 1]
    return None


def _extract_state(html: str):
    """Parse the window.APP_INITIALIZATION_STATE array out of the page."""
    i = html.find("APP_INITIALIZATION_STATE=")
    if i == -1:
        return None
    start = html.find("[", i)
    if start == -1:
        return None
    raw = _balanced(html, start)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _result_payloads(state) -> list:
    """The place list lives in inner strings that begin with )]}' — collect + parse each."""
    out = []
    stack = [state]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if cur.startswith(")]}'"):
                b = cur.find("[")
                if b != -1:
                    try:
                        out.append(json.loads(cur[b:]))
                    except Exception:
                        pass
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def _is_place(p) -> bool:
    """A Google Maps place array: index 11 = name (str), index 9 = [_, _, lat, lng]."""
    try:
        return (isinstance(p, list) and len(p) > 11 and isinstance(p[11], str) and p[11]
                and isinstance(p[9], list) and len(p[9]) >= 4
                and isinstance(p[9][2], (int, float)) and isinstance(p[9][3], (int, float)))
    except Exception:
        return False


def _find_places(obj) -> list:
    """Recursively collect place-shaped arrays (signature match — resilient to path changes)."""
    found, stack = [], [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            if _is_place(cur):
                found.append(cur)
            else:
                stack.extend(cur)
    return found


def _g(arr, *path, default=""):
    cur = arr
    for k in path:
        try:
            cur = cur[k]
        except (IndexError, KeyError, TypeError):
            return default
    return cur if cur is not None else default


def _addr(p) -> str:
    a = _g(p, 18)
    if isinstance(a, str) and a:
        return a
    lines = _g(p, 2, default=[])
    return ", ".join(x for x in lines if isinstance(x, str)) if isinstance(lines, list) else ""


def _hours(p) -> str:
    h = _g(p, 34, 1, default=[])
    if not isinstance(h, list):
        return ""
    out = []
    for item in h:
        try:
            day = item[0]
            spans = item[1]
            out.append(f"{day}: {', '.join(spans)}" if isinstance(spans, list) else str(day))
        except Exception:
            continue
    return "; ".join(out)


def _place_row(p, query: str) -> dict:
    row = _blank_row()
    row["query"] = query
    row["name"] = _g(p, 11)
    row["place_id"] = _g(p, 10)
    cats = _g(p, 13, default=[])
    row["category"] = cats[0] if isinstance(cats, list) and cats else (cats if isinstance(cats, str) else "")
    row["address"] = _addr(p)
    row["phone"] = _g(p, 178, 0, 0) or _g(p, 178, 0, 3)
    row["website"] = _g(p, 7, 0)
    rating = _g(p, 4, 7)
    row["rating"] = str(rating) if rating != "" else ""
    reviews = _g(p, 4, 8)
    row["reviews"] = str(reviews) if reviews != "" else ""
    price = _g(p, 4, 2)
    row["price_level"] = price if isinstance(price, str) else ""
    row["latitude"] = str(_g(p, 9, 2))
    row["longitude"] = str(_g(p, 9, 3))
    row["hours"] = _hours(p)
    pid = row["place_id"]
    row["maps_url"] = f"https://www.google.com/maps/place/?q=place_id:{pid}" if pid else ""
    row["business_status"] = "OPERATIONAL"      # Maps drops permanently-closed from search
    return row


def _parse(html: str, query: str) -> list[dict]:
    state = _extract_state(html)
    if not state:
        return []
    rows, seen = [], set()
    for payload in _result_payloads(state):
        for p in _find_places(payload):
            row = _place_row(p, query)
            key = row["place_id"] or (row["name"], row["latitude"], row["longitude"])
            if key in seen:
                continue
            seen.add(key)
            if row["name"]:
                rows.append(row)
    return rows


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in GMAPS_COLUMNS}


# ---------------- Quick Filters ----------------

def _passes_filters(row: dict, filters: list[str] | None) -> bool:
    if not filters:
        return True
    for f in filters:
        if f == "with_website" and not row["website"]:
            return False
        if f == "without_website" and row["website"]:
            return False
        if f == "operational" and row["business_status"] != "OPERATIONAL":
            return False
        if f == "with_phone" and not row["phone"]:
            return False
        if f in ("good_rating", "bad_rating"):
            try:
                rating = float(row["rating"])
            except (TypeError, ValueError):
                return False
            if f == "good_rating" and rating < 4:
                return False
            if f == "bad_rating" and rating > 3:
                return False
    return True


# ---------------- search ----------------

async def search_query(query: str, limit: int | None, region: str, language: str,
                       filters: list[str] | None = None, skip: int = 0,
                       stopped=None) -> list[dict]:
    """Scrape one Google Maps search, applying Quick Filters + a `skip` offset. One page of
    results (~20) per query — Maps' deeper pagination is not fetched in this version."""
    if not settings.PROXY_URL.strip():
        return []
    if stopped and stopped():
        return []
    html = await asyncio.to_thread(_fetch_sync, query, region, language)
    if not html:
        return []
    rows = [r for r in _parse(html, query) if _passes_filters(r, filters)]
    sliced = rows[skip:]
    return sliced[:limit] if limit else sliced


async def run_job(job_id: str, queries: list[str], limit: int | None,
                  region: str = "US", language: str = "en",
                  filters: list[str] | None = None, skip: int = 0, dedupe: bool = True) -> None:
    """Background task: scrape each query through the paid proxy and store the place rows."""
    from .scraper import STOP_REQUESTS
    from .db import jobs, gmaps_results
    total = 0
    seen: set[str] = set()

    def stopped() -> bool:
        return job_id in STOP_REQUESTS

    try:
        if not settings.PROXY_URL.strip():
            STOP_REQUESTS.discard(job_id)
            await jobs.update_one({"job_id": job_id}, {"$set": {
                "status": "done", "total_scraped": 0, "finished_at": datetime.utcnow(),
                "note": ("Google Maps is scraped keyless through a proxy — set a paid (residential) "
                         "PROXY_URL in .env. Google blocks the free pool. No real IP is used.")}})
            return

        for q in queries:
            if stopped():
                break
            rows = await search_query(q, limit, region, language, filters, skip, stopped)
            if dedupe:
                kept = []
                for r in rows:
                    pid = r.get("place_id")
                    if pid and pid in seen:
                        continue
                    if pid:
                        seen.add(pid)
                    kept.append(r)
                rows = kept
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await gmaps_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        STOP_REQUESTS.discard(job_id)
        done = {"status": "stopped" if stopped() else "done",
                "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total and not stopped():
            done["note"] = ("0 places — Google likely blocked the proxy, or the blob format shifted. "
                            "A clean residential PROXY_URL is required for Maps scraping.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})


# ---------------- Google Maps Search by Domains ----------------

GMAPS_DOMAIN_COLUMNS = [
    "input_domain", "name", "category", "address", "phone", "website", "rating", "reviews",
    "price_level", "business_status", "latitude", "longitude", "hours", "maps_url",
    "place_id", "website_match",
]


def _domain_host(s: str) -> str:
    """Normalise a domain or URL to a bare host (lowercase, no scheme/path/www)."""
    s = (s or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    host = (urlparse(s).netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def to_export_domain(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in GMAPS_DOMAIN_COLUMNS}


async def search_by_domain(domain: str, limit: int, region: str, language: str,
                           stopped=None) -> list[dict]:
    """Find Google Maps place(s) for one domain: search the domain, prefer results whose own
    website host matches it."""
    host = _domain_host(domain)
    if not host or not settings.PROXY_URL.strip():
        return []
    if stopped and stopped():
        return []
    html = await asyncio.to_thread(_fetch_sync, host, region, language)
    if not html:
        return []
    matched, others = [], []
    for base in _parse(html, domain):
        row = {c: base.get(c, "") for c in GMAPS_DOMAIN_COLUMNS}
        row["input_domain"] = host
        w = _domain_host(base.get("website") or "")
        is_match = bool(w) and (w == host or w.endswith("." + host) or host.endswith("." + w))
        row["website_match"] = "Yes" if is_match else ""
        (matched if is_match else others).append(row)
    out = matched + others
    return out[:limit] if limit else out


async def run_job_domains(job_id: str, domains: list[str], limit: int,
                          region: str = "US", language: str = "en") -> None:
    """Background task: find the Google Maps place(s) for each domain and store the rows."""
    from .scraper import STOP_REQUESTS
    from .db import jobs, gmaps_domain_results
    total = 0

    def stopped() -> bool:
        return job_id in STOP_REQUESTS

    try:
        if not settings.PROXY_URL.strip():
            STOP_REQUESTS.discard(job_id)
            await jobs.update_one({"job_id": job_id}, {"$set": {
                "status": "done", "total_scraped": 0, "finished_at": datetime.utcnow(),
                "note": ("Google Maps is scraped keyless through a proxy — set a paid (residential) "
                         "PROXY_URL in .env. Google blocks the free pool. No real IP is used.")}})
            return

        for d in domains:
            if stopped():
                break
            rows = await search_by_domain(d, limit, region, language, stopped)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await gmaps_domain_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "stopped" if stopped() else "done",
            "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
