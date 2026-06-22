"""Airbnb Reviews Scraper — airbnb.com listing reviews.

Same design as the other live scrapers: every request goes through a proxy IP (a paid PROXY_URL
if set, otherwise the rotating free pool — NEVER the real IP). Airbnb room pages embed the
listing's reviews in a `data-deferred-state` JSON blob, which we extract (no headless browser).
Airbnb is bot-protected, so on the free pool it is often blocked and returns 0 until a paid
PROXY_URL is set (same behaviour as the other protected sites).

A query is an Airbnb room URL (/rooms/<id>) or a bare listing id. Each query yields up to
`limit` reviews. `sort` orders them: most_recent | highest | lowest.
"""
import asyncio
import base64
import json
import re
import threading
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

BASE = "https://www.airbnb.com"
_AB_TIMEOUT = 15
_GOOD_PROXY = None          # last proxy that passed airbnb.com — reused before re-rotating
_PIN_LOCK = threading.Lock()
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

# Airbnb's public web API key (embedded in every page) + the StaysPdpReviewsQuery operation,
# captured once from a real-IP browser render. The reviews API is free-callable through the proxy
# pool; only the persisted-query hash + variables come from the page, so we capture them once.
_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
_OP = {"hash": None, "variables": None, "locale": "en", "currency": "USD"}
_OP_LOCK = threading.Lock()

_ID_IN_URL = re.compile(r"/rooms/(?:plus/)?(\d+)")

SORT_PARAM = {"": "most_recent", "most_recent": "most_recent", "highest": "highest", "lowest": "lowest"}

# one row per review
AIRBNB_REVIEW_COLUMNS = [
    "query", "listing_id", "listing_name", "reviewer", "reviewer_location", "rating",
    "date", "comment", "host_response", "language", "position",
]


def _blank_row():
    return {c: "" for c in AIRBNB_REVIEW_COLUMNS}


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    if r is None or r.status_code != 200:
        return False
    low = (r.text or "").lower()
    if any(s in low for s in ("px-captcha", "/cdn-cgi/challenge", "please verify", "access to this page has been denied")):
        return False
    return "data-deferred-state" in low or "airbnb" in low


def _try(url: str, px: str):
    try:
        r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                     timeout=_AB_TIMEOUT, verify=False, allow_redirects=True)
        return r if _ok(r) else None
    except Exception:
        return None


def _proxied_get(url: str):
    global _GOOD_PROXY
    pinned = _GOOD_PROXY
    if pinned:
        r = _try(url, pinned)
        if r is not None:
            return r
    from . import yp_us
    yp_us.ensure_pool(_SEED, 8)
    seen, candidates = {pinned}, []
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px not in seen:
            seen.add(px)
            candidates.append(px)
    for px in candidates[:15]:
        r = _try(url, px)
        if r is not None:
            with yp_us._LOCK:
                if px in yp_us._GOOD:
                    yp_us._GOOD.remove(px)
                yp_us._GOOD.insert(0, px)
            with _PIN_LOCK:
                _GOOD_PROXY = px
            return r
    raise RuntimeError("no free proxy passed airbnb.com")


def _get_text(url: str) -> str | None:
    proxy = settings.PROXY_URL.strip()
    if proxy:
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                         timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
        except Exception:
            return None
        return r.text if _ok(r) else None
    try:
        return _proxied_get(url).text
    except Exception:
        return None


# ---------------- parsing ----------------

def _listing_id(query: str) -> str:
    m = _ID_IN_URL.search(query or "")
    if m:
        return m.group(1)
    q = (query or "").strip().rstrip("/").split("/")[-1]
    return q if q.isdigit() else ""


def _room_url(query: str) -> str:
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q
    lid = _listing_id(q)
    return f"{BASE}/rooms/{lid}" if lid else f"{BASE}/rooms/{q}"


def _iter_deferred_json(html: str):
    """Yield parsed JSON from each <script ... data-deferred-state...> / application/json block."""
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html or "", re.S):
        body = m.group(1).strip()
        if '"comments"' in body or '"reviewer"' in body or '"localizedReview"' in body:
            try:
                yield json.loads(body)
            except Exception:
                continue


def _find_reviews(obj):
    """Walk the JSON for Airbnb review objects (have a comment + reviewer/rating)."""
    found = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            has_text = isinstance(cur.get("comments"), str) or isinstance(cur.get("localizedReview"), dict)
            if has_text and ("reviewer" in cur or "rating" in cur or "createdAt" in cur):
                found.append(cur)
            else:
                stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _listing_name(html: str) -> str:
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', html)
    return (m.group(1).split(" - ")[0].strip() if m else "")


def _row(rv: dict, listing_id: str, listing_name: str, query: str) -> dict | None:
    reviewer = rv.get("reviewer") or {}
    if isinstance(reviewer, dict):
        rname = reviewer.get("firstName") or reviewer.get("smartName") or reviewer.get("name") or ""
        rloc = reviewer.get("location") or ""
    else:
        rname, rloc = str(reviewer or ""), ""
    loc_rev = rv.get("localizedReview") or {}
    comment = (rv.get("comments") or (loc_rev.get("comments") if isinstance(loc_rev, dict) else "") or "").strip()
    resp = rv.get("response") or (loc_rev.get("response") if isinstance(loc_rev, dict) else "") or ""
    row = _blank_row()
    row["query"] = query
    row["listing_id"] = listing_id
    row["listing_name"] = listing_name
    row["reviewer"] = rname
    row["reviewer_location"] = rloc or rv.get("reviewerLocation") or ""
    row["rating"] = str(rv.get("rating") or "")
    row["date"] = rv.get("createdAt") or rv.get("localizedDate") or rv.get("createdAtDate") or ""
    row["comment"] = comment
    row["host_response"] = (resp or "").strip()
    row["language"] = rv.get("language") or (loc_rev.get("language") if isinstance(loc_rev, dict) else "") or ""
    return row if row["comment"] else None


def _parse(html: str, query: str) -> list[dict]:
    listing_id = _listing_id(query) or _listing_id(_room_url(query))
    listing_name = _listing_name(html)
    out, seen = [], set()
    for data in _iter_deferred_json(html):
        for rv in _find_reviews(data):
            row = _row(rv, listing_id, listing_name, query)
            if row:
                key = (row["reviewer"], row["date"], row["comment"][:48])
                if key not in seen:
                    seen.add(key)
                    out.append(row)
    return out


# ---------------- headless render (reviews load via JS, not in the static HTML) ----------------

def _headless_get_sync(url: str, proxy: str | None) -> str | None:
    """Render the room page in headless Chrome so Airbnb's JS loads the reviews, then return the
    rendered HTML. Routed through `proxy` when given (NEVER the real IP)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    launch_proxy = None
    if proxy:
        launch_proxy = {"server": proxy if proxy.startswith("http") else "http://" + proxy}
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(channel="chrome", headless=True, proxy=launch_proxy,
                                   args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            pg = br.new_context(locale="en-US").new_page()
            pg.goto(url, timeout=45000, wait_until="domcontentloaded")
            for _ in range(8):                       # scroll to trigger the reviews section
                pg.evaluate("window.scrollBy(0, document.body.scrollHeight/6)")
                pg.wait_for_timeout(900)
            try:
                pg.wait_for_selector('[id^="review_"]', timeout=5000)
            except Exception:
                pass
            html = pg.content()
            br.close()
            return html
    except Exception:
        return None


def _rendered_html(url: str) -> str | None:
    """Headless render through a proxy. Paid PROXY_URL first; else a few warm free proxies."""
    paid = settings.PROXY_URL.strip()
    if paid:
        return _headless_get_sync(url, paid)
    from . import yp_us
    with yp_us._LOCK:
        warm = list(yp_us._GOOD)
    if len(warm) < 3:
        yp_us.ensure_pool(_SEED, 8)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
    for px in warm[:3]:
        html = _headless_get_sync(url, px)
        if html and ('id="review_' in html or "on Airbnb" in html):
            return html
    return None


def _parse_rendered(html: str, query: str) -> list[dict]:
    """Parse reviews from the rendered DOM. Each review has an id="review_<id>_title" block
    (reviewer h3 + 'X years on Airbnb'); the rating/date/comment follow in the same card."""
    soup = BeautifulSoup(html, "lxml")
    listing_id = _listing_id(query) or _listing_id(_room_url(query))
    listing_name = _listing_name(html)
    out, seen = [], set()
    for title in soup.find_all(id=re.compile(r"^review_\d+_title$")):
        rid = re.match(r"review_(\d+)_title", title.get("id")).group(1)
        h = title.find(["h2", "h3"])
        reviewer = h.get_text(strip=True) if h else ""

        def _comment_of(text):
            best = ""
            for line in text.split("\n"):
                ll = line.strip()
                low = ll.lower()
                if (ll and ll != reviewer and "on airbnb" not in low and "ago" not in low
                        and "out of 5" not in low and "show more" not in low
                        and "stars" not in low and not low.startswith("rating")
                        and not re.fullmatch(r"[\d.\s★☆·,/-]+", ll) and len(ll) > len(best)):
                    best = ll
            return best

        # climb up until the card actually contains this review's comment (a real sentence)
        card, ctext, comment = title, "", ""
        for _ in range(6):
            card = card.parent
            if card is None:
                break
            ctext = card.get_text("\n", strip=True)
            comment = _comment_of(ctext)
            if len(comment) >= 40:
                break
        if not comment:
            continue
        dm = re.search(r"(\d+\s+(?:hour|day|week|month|year)s?\s+ago|[A-Z][a-z]{2,8}\s+\d{4})", ctext)
        date = dm.group(1) if dm else ""
        rm = re.search(r"Rating[, ]+(\d)", ctext) or re.search(r"(\d)\s+out of\s+5", ctext)
        rating = rm.group(1) if rm else ""
        row = _blank_row()
        row.update(query=query, listing_id=listing_id, listing_name=listing_name,
                   reviewer=reviewer, rating=rating, date=date, comment=comment)
        key = (rid, comment[:40])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------- internal reviews API (StaysPdpReviewsQuery) ----------------

def _capture_op(url: str) -> dict | None:
    """Render the room page on the REAL IP (headless Chrome) and intercept the page's own
    StaysPdpReviewsQuery request — its URL carries the live persisted-query hash + variables. We do
    this ONCE; afterwards reviews are fetched via the free-proxy API. Returns {hash, variables,
    locale, currency, listing_name} or None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    cap: dict = {}
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(channel="chrome", headless=True,
                                   args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            pg = br.new_context(locale="en-US").new_page()

            def on_req(req):
                u = req.url
                if "StaysPdpReviewsQuery" in u and "hash" not in cap:
                    m = re.search(r"/StaysPdpReviewsQuery/([a-f0-9]{64})", u)
                    qs = parse_qs(urlparse(u).query)
                    if m and qs.get("variables"):
                        try:
                            cap["hash"] = m.group(1)
                            cap["variables"] = json.loads(qs["variables"][0])
                            cap["locale"] = (qs.get("locale") or ["en"])[0]
                            cap["currency"] = (qs.get("currency") or ["USD"])[0]
                        except Exception:
                            cap.clear()

            pg.on("request", on_req)
            pg.goto(url, timeout=45000, wait_until="domcontentloaded")
            for _ in range(10):
                if "hash" in cap:
                    break
                pg.evaluate("window.scrollBy(0, document.body.scrollHeight/5)")
                pg.wait_for_timeout(1000)
            cap["listing_name"] = (pg.title() or "").split(" - ")[0].strip()
            br.close()
    except Exception:   
        return None
    return cap if cap.get("hash") else None


def _api_url(listing_id: str, offset: int, limit: int) -> str | None:
    with _OP_LOCK:
        if not _OP.get("hash"):
            return None
        hsh, variables = _OP["hash"], json.loads(json.dumps(_OP["variables"]))
        locale, currency = _OP.get("locale", "en"), _OP.get("currency", "USD")
    variables["id"] = base64.b64encode(f"StayListing:{listing_id}".encode()).decode()
    pr = variables.get("pdpReviewsRequest") or {}
    pr.update({"offset": str(offset), "limit": limit, "first": limit})
    variables["pdpReviewsRequest"] = pr
    ext = {"persistedQuery": {"version": 1, "sha256Hash": hsh}}
    qs = urlencode({"operationName": "StaysPdpReviewsQuery", "locale": locale, "currency": currency,
                    "variables": json.dumps(variables, separators=(",", ":")),
                    "extensions": json.dumps(ext, separators=(",", ":"))})
    return f"{BASE}/api/v3/StaysPdpReviewsQuery/{hsh}?{qs}"


def _api_json(listing_id: str, offset: int, limit: int):
    """Fetch one page of reviews from the internal API through the free pool (NEVER the real IP).
    Returns the parsed JSON, or None if blocked / the hash went stale (forces a re-capture)."""
    global _GOOD_PROXY
    url = _api_url(listing_id, offset, limit)
    if not url:
        return None
    from . import yp_us
    hdr = {"X-Airbnb-API-Key": _API_KEY, "accept": "application/json"}
    yp_us.ensure_pool(_SEED, 6)
    pinned = _GOOD_PROXY
    cands = ([pinned] if pinned else []) + [
        p for p in (list(yp_us._GOOD) + yp_us._fetch_candidates()) if p != pinned]
    for px in cands[:12]:
        try:
            r = cffi.get(url, impersonate="chrome", headers=hdr,
                         proxies={"http": px, "https": px}, timeout=_AB_TIMEOUT, verify=False)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        if "persistedquery" in r.text[:300].lower():       # stale hash → re-capture next time
            with _OP_LOCK:
                _OP["hash"] = None
            return None
        try:
            data = r.json()
        except Exception:
            continue
        with _PIN_LOCK:
            _GOOD_PROXY = px
        return data
    return None


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None, sort: str) -> list[dict]:
    url = _room_url(query)
    listing_id = _listing_id(query) or _listing_id(url)
    rows: list[dict] = []
    listing_name = ""

    # 1) Internal reviews API (free proxy). Capture the op (hash+variables) ONCE via a real-IP
    #    browser render, then page through reviews via the free pool — no browser per call.
    if listing_id:
        if not _OP.get("hash"):
            cap = _capture_op(url)
            if cap:
                with _OP_LOCK:
                    _OP.update(cap)
                listing_name = cap.get("listing_name") or ""
        if _OP.get("hash"):
            seen, offset, page = set(), 0, 24
            while True:
                data = _api_json(listing_id, offset, page)
                if not data:
                    break
                batch = [r for r in (_row(rv, listing_id, listing_name, query)
                                     for rv in _find_reviews(data)) if r]
                fresh = [b for b in batch
                         if (b["reviewer"], b["date"], b["comment"][:48]) not in seen]
                for b in fresh:
                    seen.add((b["reviewer"], b["date"], b["comment"][:48]))
                rows += fresh
                if not fresh or (limit and len(rows) >= limit) or len(batch) < page:
                    break
                offset += page

    # 2) Fallback: rendered DOM / embedded JSON (paid proxy, or a lucky free proxy)
    if not rows:
        html = _rendered_html(url)
        rows = _parse_rendered(html, query) if html else []
        if not rows:
            plain = html or _get_text(url)
            if plain:
                rows = _parse(plain, query)
    if not rows:
        return []

    s = SORT_PARAM.get(sort or "", "most_recent")
    if s == "highest":
        rows.sort(key=lambda r: float(r.get("rating") or 0), reverse=True)
    elif s == "lowest":
        rows.sort(key=lambda r: float(r.get("rating") or 0))
    else:  # most_recent — Airbnb dates sort lexicographically (ISO) well enough
        rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None, sort: str) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in AIRBNB_REVIEW_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "") -> None:
    """Background task: scrape each listing's reviews and store the rows."""
    from .db import jobs, airbnb_reviews
    total = 0
    try:
        mode = "airbnb-free-pool" if not settings.PROXY_URL.strip() else "airbnb-paid-proxy"
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": mode}})

        for q in queries:
            rows = await search(q, limit, sort)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit, sort)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await airbnb_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
