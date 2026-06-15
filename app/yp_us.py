"""yellowpages.com (US) scraper.

The US site is behind Cloudflare and geo-blocks non-US IPs outright, so plain httpx
(any headers) and even a headless browser get a hard 403 from a non-US IP. The working
recipe, verified live, is: a US exit IP + a real Chrome TLS fingerprint. We get the TLS
fingerprint from `curl_cffi` (impersonate="chrome") and the US IP from either a paid
proxy (settings.PROXY_URL) or a rotating pool of free US proxies.

curl_cffi is synchronous; callers wrap fetch_us_page() with asyncio.to_thread so the
FastAPI event loop is never blocked.
"""
import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

BASE = "https://www.yellowpages.com"
SEARCH = BASE + "/search"

# free US proxy sources (country-tagged); validated against a real search response
US_PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=US&ssl=all&anonymity=all",
    "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&proxy_format=ipport&format=text&country=US",
    "https://proxylist.geonode.com/api/proxy-list?limit=200&protocols=http%2Chttps&country=US&format=text",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/US/data.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/http.txt",
]

PROBE_TIMEOUT = 10      # short per-proxy timeout while probing (vs REQUEST_TIMEOUT for paid)
PROBE_WORKERS = 30      # how many proxies to probe concurrently
POOL_SIZE = 12          # how many validated proxies to keep warm

_GOOD: list[str] = []   # proxies that returned real listings, newest-working first
_BAD: set[str] = set()
_LOCK = threading.Lock()  # guards _GOOD / _BAD (page fetches run concurrently)


def _impersonated_get(url: str, params: dict, proxy: str | None, timeout: int | None = None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return cffi.get(url, params=params, impersonate="chrome", proxies=proxies,
                    timeout=timeout or settings.REQUEST_TIMEOUT, verify=False)


def _is_valid_yp_page(text: str) -> bool:
    """True for a real yellowpages.com response — INCLUDING a valid page with zero listings
    or YP's HTTP-404 "Invalid Search" page (returned for an unrecognized location, e.g. a
    Canadian city). All of these render the search form (`search_terms`); a Cloudflare block
    page does not. We deliberately do NOT require `business-name` or HTTP 200 here: an empty
    or invalid-search result is a *successful* fetch, not a blocked proxy. Requiring listings
    made 0-result searches misread every working proxy as "blocked", exhausting the whole
    pool and hanging the job at "running / 0 records" while the UI polled forever."""
    return "search_terms" in text and "you have been blocked" not in text


_IPPORT = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})")


def _fetch_candidates() -> list[str]:
    """Pull free US proxies from all sources. Handles both bare `ip:port` and
    scheme-prefixed (`http://ip:port`) lines; normalises to `http://ip:port`."""
    found: list[str] = []
    seen = set()
    with httpx.Client(timeout=20, follow_redirects=True) as c:
        for url in US_PROXY_SOURCES:
            try:
                r = c.get(url)
                for m in _IPPORT.findall(r.text):
                    px = "http://" + m
                    if px not in seen and px not in _BAD:
                        seen.add(px); found.append(px)
            except Exception:
                pass
    return found


def _mark_good(px: str):
    with _LOCK:
        if px in _GOOD:
            _GOOD.remove(px)
        _GOOD.insert(0, px)        # newest-working first
        del _GOOD[POOL_SIZE:]      # keep the pool bounded


def _mark_bad(px: str):
    with _LOCK:
        _BAD.add(px)
        if px in _GOOD:
            _GOOD.remove(px)


def _probe(px: str, params: dict):
    """Try one proxy with a short timeout. Returns (px, html, status) where status is
    'good', 'blocked' (Cloudflare refused this IP — unusable), or 'dead' (timeout/conn
    error — likely just slow/overloaded, worth retrying later)."""
    try:
        r = _impersonated_get(SEARCH, params, px, timeout=PROBE_TIMEOUT)
        # A real YP response — results, an empty SERP, or a 404 "Invalid Search" page — is a
        # successful fetch. Only a block/garbage page (no search form) is "blocked".
        if _is_valid_yp_page(r.text):
            return px, r.text, "good"
        return px, None, "blocked"   # reachable proxy but YP refused it
    except Exception:
        return px, None, "dead"      # transient — do NOT permanently blacklist


def _record(px: str, status: str):
    if status == "good":
        _mark_good(px)
    elif status == "blocked":
        _mark_bad(px)
    # 'dead' -> leave alone; it may work next time (free proxies are flaky/slow)


def _probe_batch(proxies: list[str], params: dict) -> str | None:
    """Probe many proxies CONCURRENTLY; return the first valid HTML. Abandons the rest
    as soon as one succeeds. Only confirmed-blocked proxies are blacklisted."""
    if not proxies:
        return None
    ex = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
    try:
        futs = [ex.submit(_probe, px, params) for px in proxies]
        for fut in as_completed(futs):
            px, html, status = fut.result()
            if html:
                _mark_good(px)
                return html
            _record(px, status)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return None


def ensure_pool(params: dict, want: int = POOL_SIZE) -> int:
    """Warm the proxy pool: probe candidates concurrently and keep up to `want` good ones.
    Returns the number of good proxies now available."""
    with _LOCK:
        have = len(_GOOD)
    if have >= want:
        return have
    candidates = [p for p in _fetch_candidates() if p not in _GOOD][:150]
    ex = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
    try:
        futs = [ex.submit(_probe, px, params) for px in candidates]
        for fut in as_completed(futs):
            px, html, status = fut.result()
            if html:
                _mark_good(px)
                with _LOCK:
                    if len(_GOOD) >= want:
                        break
            else:
                _record(px, status)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    with _LOCK:
        return len(_GOOD)


def _fetch_sync(params: dict) -> str:
    """Fetch one search page through a US proxy. Tries the warm pool first (in parallel),
    then probes fresh candidates in parallel. Raises if everything is blocked/dead."""
    # A paid US proxy from .env is reliable — use it directly, no pool.
    if settings.PROXY_URL.strip():
        r = _impersonated_get(SEARCH, params, settings.PROXY_URL.strip())
        if _is_valid_yp_page(r.text):
            return r.text
        raise RuntimeError(
            f"PROXY_URL reached yellowpages.com but got status {r.status_code} / blocked. "
            f"Is it a US exit IP?"
        )

    # 1) warm pool first (parallel probe of the small known-good set)
    with _LOCK:
        warm = list(_GOOD)
    html = _probe_batch(warm, params)
    if html:
        return html

    # 2) refill from fresh candidates and probe them in parallel
    candidates = [p for p in _fetch_candidates() if p not in _GOOD][:120]
    html = _probe_batch(candidates, params)
    if html:
        return html

    # 3) recovery: the blacklist may have starved the pool (free proxies are flaky and
    # a working one can get banned after a single hiccup). Clear it and try every
    # candidate once more from a clean slate before giving up.
    with _LOCK:
        _BAD.clear()
    html = _probe_batch(_fetch_candidates()[:200], params)
    if html:
        return html

    raise RuntimeError(
        "Could not reach yellowpages.com through any free US proxy. "
        "Retry, or set a paid US PROXY_URL in .env for reliability."
    )


async def fetch_us_page(search: str, location: str, page: int) -> str:
    params = {"search_terms": search, "geo_location_terms": location, "page": str(page)}
    return await asyncio.to_thread(_fetch_sync, params)


def parse_us_total(html: str) -> int | None:
    """'Showing 1-30 of 3000' -> 3000 (read from the pagination/showing-count line)."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one(".showing-count") or soup.select_one(".pagination")
    text = el.get_text(" ", strip=True) if el else html
    m = re.search(r"Showing\s+[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)", text, re.I)
    return int(m.group(1).replace(",", "")) if m else None


# Group label (as YP renders it) -> our normalized key for the UI tabs.
_FILTER_GROUPS = {"Category": "categories", "Features": "features", "Neighborhoods": "neighborhoods"}


def parse_us_filters(html: str) -> dict:
    """Extract YP's own filter options, embedded in the page as
    `"filters":{"Filters":[{"Label":"Category","Options":[{"Label","Key","Value","Count"}]},...]}`.

    Returns the three tabs YP shows, e.g.::

        {"categories":   [{"label","value","count"}, ...],   # Key=headingtext, value=label
         "features":     [{"label","value","count"}, ...],   # e.g. Coupons -> COUPON
         "neighborhoods":[{"label","value","count"}, ...]}

    YP applies these client-side via AJAX (not URL params), so we use them only to mirror
    YP's option list in the UI; the actual filtering is done on the scraped data.
    """
    empty = {v: [] for v in _FILTER_GROUPS.values()}
    i = html.find('"filters":{"Filters":')
    if i == -1:
        return empty
    start = html.find("[", i)
    if start == -1:
        return empty
    # balanced-bracket scan (option labels can contain '[' ']' so we can't regex this)
    depth = 0
    end = -1
    for j in range(start, len(html)):
        ch = html[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end == -1:
        return empty
    try:
        groups = json.loads(html[start:end])
    except (ValueError, json.JSONDecodeError):
        return empty

    out = {v: [] for v in _FILTER_GROUPS.values()}
    for g in groups:
        key = _FILTER_GROUPS.get(g.get("Label"))
        if not key:
            continue
        for o in g.get("Options", []):
            label = o.get("Label")
            if not label:
                continue
            out[key].append({
                "label": label,
                "value": o.get("Value", label),
                "count": o.get("Count"),
            })
    return out


async def get_filters(search: str, location: str) -> dict:
    """Fetch page 1 of a search and return YP's filter options for the modal."""
    params = {"search_terms": search, "geo_location_terms": location, "page": "1"}
    html = await asyncio.to_thread(_fetch_sync, params)
    return parse_us_filters(html)


def _txt(node):
    return node.get_text(" ", strip=True) if node else None


def parse_us_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div.search-results.organic div.result") or soup.select("div.result")
    out = []
    for c in cards:
        name_el = c.select_one("a.business-name")
        name = _txt(name_el)
        if not name:
            continue

        phone = _txt(c.select_one(".phones.phone.primary")) or _txt(c.select_one(".phone"))

        street = _txt(c.select_one(".street-address"))
        locality = _txt(c.select_one(".locality"))  # "New York, NY 10016"
        city = pincode = None
        if locality:
            m = re.search(r"(\d{5})(?:-\d{4})?$", locality)
            if m:
                pincode = m.group(1)
                city = locality[:m.start()].strip().rstrip(",").strip()
            else:
                city = locality

        cats = [a.get_text(strip=True) for a in c.select(".categories a")]
        cat_list = list(dict.fromkeys([x for x in cats if x]))  # deduped, order-preserving
        category = ", ".join(cat_list) or None

        reviews = _txt(c.select_one(".ratings .count"))  # "(1)"
        if reviews:
            rm = re.search(r"\d+", reviews)
            reviews = rm.group(0) if rm else None

        rating = None
        rclass = c.select_one(".result-rating")
        if rclass:
            words = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5"}
            cls = " ".join(rclass.get("class", []))
            whole = next((v for k, v in words.items() if k in cls), None)
            if whole:
                rating = whole + (".5" if "half" in cls else ".0")

        web = c.select_one("a.track-visit-website")
        website = web.get("href") if web else None

        dir_el = c.select_one("a.directions, a.track-map-it")
        directions = urljoin(BASE, dir_el.get("href")) if dir_el and dir_el.get("href") else None

        img = c.select_one(".media-thumbnail img") or c.select_one("img")
        image = (img.get("src") or img.get("data-src")) if img else None
        if image and image.startswith("//"):
            image = "https:" + image

        snippet = _txt(c.select_one(".snippet"))
        if snippet:
            snippet = re.sub(r"^From Business:\s*", "", snippet)

        years = _txt(c.select_one(".years-in-business"))
        if years:
            ym = re.search(r"\d+", years)
            years = ym.group(0) if ym else None

        href = name_el.get("href") if name_el else None

        out.append({
            "name": name,
            "phone": phone,
            "category": category,        # joined, for display
            "categories": cat_list,      # list, for exact category-filter matching

            "area": street,
            "city": city,
            "pincode": pincode,
            "rating": rating,
            "reviews_count": reviews,
            "open_status": _txt(c.select_one(".open-status")),
            "email": None,            # not exposed on the US results page
            "website": website,
            "directions": directions,
            "image": image,
            "years_in_business": years,
            "description": snippet,
            "source_url": urljoin(BASE, href) if href else None,
        })
    return out
