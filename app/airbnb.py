"""Airbnb Search Scraper — listings from an airbnb.com search (API-only).

Fetches results from Airbnb's internal StaysSearch GraphQL API (/api/v3/StaysSearch) directly —
no headless browser, no HTML crawling. Every request goes through a proxy (NEVER the real IP).
The API is behind PerimeterX and the persisted-query hash lives in a JS bundle the free pool can't
reach, so this path REQUIRES a paid (ideally residential) PROXY_URL. With no paid proxy — or if
Airbnb rotates the query hash — it returns 0 with a clear note; there is no HTML-crawl fallback.

It needs three things, assembled per request:
  1. the public web API key (stable),
  2. the request variables — which Airbnb embeds in the search page as "niobeClientData", and
  3. the operation's sha256 hash — scraped from a JS bundle and cached (it rotates per release).

A query is an airbnb.com search URL (…/s/<place>/homes) or a bare location keyword. Each query
yields up to `limit` listings (Airbnb caps search at ~15 pages × 18 ≈ 270).
"""
import asyncio
import json
import re
import threading
from datetime import datetime
from urllib.parse import quote

from curl_cffi import requests as cffi

from .config import settings

BASE = "https://www.airbnb.com"
PER_PAGE = 18          # Airbnb returns ~18 listings per page
PAGE_CAP = 15          # and only lets you page through ~15 pages (~270 listings) per search

# one row per listing
AIRBNB_SEARCH_COLUMNS = [
    "query", "listing_id", "name", "price", "rating", "reviews",
    "location", "url", "image", "badge",
]


def _blank_row():
    return {c: "" for c in AIRBNB_SEARCH_COLUMNS}


# ---------------- URL building + pagination cursors ----------------

def _search_url(query: str) -> str:
    """A query may be a full airbnb.com search URL or a bare location keyword -> /s/<place>/homes."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q
    return f"{BASE}/s/{quote(q)}/homes"


# Airbnb embeds the pagination cursors (one per page, items_offset 0/18/36/…) in the page JSON as
# "pageCursors":[...]. We read those and set each on staysSearchRequest.cursor for the API call.
_CURSORS_RE = re.compile(r'"pageCursors"\s*:\s*\[(.*?)\]', re.S)


def _page_cursors(html: str) -> list[str]:
    m = _CURSORS_RE.search(html or "")
    return re.findall(r'"([A-Za-z0-9_\-=]+)"', m.group(1)) if m else []


# ---------------- Airbnb internal API (StaysSearch GraphQL) ----------------

API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t"   # public web api key (stable)
_OP_HASH = None                              # cached StaysSearch persisted-query hash
_OP_LOCK = threading.Lock()


def _curl(url: str, proxy: str | None, headers: dict | None = None, timeout: int = 20):
    return cffi.get(url, impersonate="chrome", headers=headers or {},
                    proxies={"http": proxy, "https": proxy} if proxy else None,
                    timeout=timeout, verify=False, allow_redirects=True)


def _ok(r) -> bool:
    """A real airbnb.com response, not a PerimeterX / captcha interstitial."""
    if r is None or r.status_code != 200:
        return False
    low = (r.text or "").lower()
    if any(s in low for s in ("px-captcha", "/cdn-cgi/challenge", "please verify",
                              "access to this page has been denied")):
        return False
    return "airbnb" in low


def _balanced_json(s: str, start: int) -> str | None:
    """Return the JSON array/object substring starting at index `start` (bracket-matched, string-
    aware) — Airbnb's embedded blobs contain brackets inside strings, so we can't regex them."""
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


def _bootstrap_variables(html: str) -> dict | None:
    """Extract the exact StaysSearch GraphQL variables the page used, from its embedded
    "niobeClientData" bootstrap. Returns the variables dict (incl. staysSearchRequest), or None."""
    idx = html.find('"niobeClientData"')
    if idx == -1:
        return None
    arr_start = html.find("[", idx)
    raw = _balanced_json(html or "", arr_start) if arr_start != -1 else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    for entry in data:
        if (isinstance(entry, list) and entry and isinstance(entry[0], str)
                and entry[0].startswith("StaysSearch") and len(entry) > 1
                and isinstance(entry[1], dict)):
            return entry[1].get("variables")
    return None


def _bundle_op_hash(html: str, proxy: str | None) -> str | None:
    """Scan Airbnb's JS bundles for the StaysSearch persisted-query sha256 hash. Cached once found
    (it only changes when Airbnb ships a new frontend build)."""
    global _OP_HASH
    if _OP_HASH:
        return _OP_HASH
    srcs = re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html or "")
    srcs.sort(key=lambda s: ("stayssearch" not in s.lower(), "search" not in s.lower()))
    for s in srcs[:30]:
        try:
            txt = _curl(s, proxy, timeout=15).text or ""
        except Exception:
            continue
        m = (re.search(r'StaysSearch["\']?\s*[,:]\s*["\']([0-9a-f]{64})', txt)
             or re.search(r'["\']([0-9a-f]{64})["\']\s*[,:]\s*["\']?StaysSearch', txt))
        if m:
            with _OP_LOCK:
                _OP_HASH = m.group(1)
            return _OP_HASH
    return None


def _api_price(sr: dict) -> str:
    txt = json.dumps(sr)
    m = re.search(r'"price"\s*:\s*"([^"]*\d[^"]*)"', txt)
    if m:
        return m.group(1)
    m = re.search(r"[₹$€£]\s?[\d,]+", txt)
    return m.group(0) if m else ""


def _api_image(listing: dict) -> str:
    pics = listing.get("contextualPictures") or listing.get("contextualPicturesData") or []
    if isinstance(pics, list):
        for p in pics:
            if isinstance(p, dict) and (p.get("picture") or p.get("pictureUrl")):
                return p.get("picture") or p.get("pictureUrl")
    return listing.get("mainPictureUrl") or ""


def _api_listing_rows(data: dict, query: str) -> list[dict]:
    """Best-effort parse of a StaysSearch GraphQL response into listing rows. Walks for any
    `searchResults` array (its exact path varies by release), then reads each result's listing."""
    results = []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            sr = cur.get("searchResults")
            if isinstance(sr, list):
                results.extend(sr)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    rows, seen = [], set()
    for sr in results:
        if not isinstance(sr, dict):
            continue
        listing = sr.get("listing")
        if not isinstance(listing, dict):
            continue
        lid = str(listing.get("id") or "")
        name = listing.get("name") or listing.get("title") or ""
        if not (lid or name) or lid in seen:
            continue
        seen.add(lid)
        a11y = listing.get("avgRatingA11yLabel") or ""
        rm = re.search(r"([0-5](?:\.\d+)?)", a11y)
        rating = rm.group(1) if rm else (str(listing.get("avgRatingLocalized") or "").split() or [""])[0]
        rev = ""
        rvm = re.search(r"([\d,]+)\s+review", a11y, re.I)
        if rvm:
            rev = rvm.group(1).replace(",", "")
        row = _blank_row()
        row.update(query=query, listing_id=lid, name=name, price=_api_price(sr), rating=rating,
                   reviews=rev, location=listing.get("localizedCityName") or "",
                   url=f"{BASE}/rooms/{lid}" if lid else "", image=_api_image(listing), badge="")
        rows.append(row)
    return rows


def _via_api(url: str, query: str, max_pages: int) -> list[dict]:
    """Fetch listings through Airbnb's StaysSearch GraphQL API (no browser). Requires a paid
    PROXY_URL — the free pool cannot reach the JS bundle/API. Returns [] if unavailable."""
    proxy = settings.PROXY_URL.strip()
    if not (proxy and settings.AIRBNB_API):
        return []
    try:
        boot = _curl(url, proxy)
    except Exception:
        return []
    html = boot.text if boot is not None else ""
    if not _ok(boot):
        return []
    base_vars = _bootstrap_variables(html)
    op_hash = _bundle_op_hash(html, proxy)
    if not (base_vars and op_hash):
        return []
    cursors = _page_cursors(html) or [None]
    rows = []
    for cur in cursors[:max_pages]:
        v = json.loads(json.dumps(base_vars))            # deep copy per page
        ssr = v.get("staysSearchRequest")
        if isinstance(ssr, dict) and cur:
            ssr["cursor"] = cur
        ext = {"persistedQuery": {"version": 1, "sha256Hash": op_hash}}
        api = ("https://www.airbnb.com/api/v3/StaysSearch?operationName=StaysSearch"
               "&locale=en&currency=USD"
               "&variables=" + quote(json.dumps(v, separators=(",", ":")))
               + "&extensions=" + quote(json.dumps(ext, separators=(",", ":"))))
        try:
            data = _curl(api, proxy, headers={"X-Airbnb-Api-Key": API_KEY}).json()
        except Exception:
            break
        page_rows = _api_listing_rows(data, query)
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None) -> list[dict]:
    url = _search_url(query)
    # only fetch as many pages as needed for the limit (capped at Airbnb's ~15-page ceiling)
    max_pages = min(PAGE_CAP, (limit // PER_PAGE) + 2) if limit else PAGE_CAP
    rows = _via_api(url, query, max_pages)
    out, seen = [], set()
    for r in rows:
        key = r["listing_id"] or r["url"] or r["name"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


async def search(query: str, limit: int | None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in AIRBNB_SEARCH_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    """Background task: scrape each search's listings via the StaysSearch API and store the rows."""
    from .db import jobs, airbnb_search_results
    total = 0
    try:
        mode = "airbnb-api-paid-proxy" if settings.PROXY_URL.strip() else "airbnb-api-no-proxy"
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": mode}})

        for q in queries:
            rows = await search(q, limit)
            if not rows and settings.PROXY_URL.strip():   # transient API hiccup — retry once
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await airbnb_search_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = (
                "Airbnb Search is API-only — it calls Airbnb's StaysSearch GraphQL API, which "
                "requires a paid residential PROXY_URL (the free pool can't reach it). Set PROXY_URL "
                "in .env. No real IP was used.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
