"""G2 Reviews Scraper — g2.com product reviews.

Same design as the other live scrapers: every request goes through a proxy IP (a paid
PROXY_URL if set, otherwise the rotating free US pool — NEVER the real IP). g2.com sits behind
Cloudflare, so on the free pool it is frequently challenged/403'd and returns 0/blocked until a
paid PROXY_URL is set (same behaviour as the BBB scraper).

A query line may be a g2.com product-reviews URL, a product URL, or a bare product slug
(e.g. "outscraper"). Each yields up to `limit` reviews. `sort` maps to G2's review ordering
(?order=...): "" (G2 default) | most_recent | most_helpful | highest_rated | lowest_rated.
"""
import asyncio
import json
import re
import threading
from datetime import datetime
from urllib.parse import urlparse, urljoin, urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

BASE = "https://www.g2.com"
MAX_PAGES = 10
_G2_TIMEOUT = 15
_GOOD_PROXY = None          # last proxy that passed g2.com — reused before re-rotating
_PIN_LOCK = threading.Lock()

# free-pool seed (any small query works just to warm the pool)
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

# dropdown value -> G2 ?order= value ("" = G2's own default ordering)
SORT_PARAM = {
    "": "", "most_recent": "most_recent", "most_helpful": "most_helpful",
    "highest_rated": "highest_rated", "lowest_rated": "lowest_rated",
}

# one row per review
G2_COLUMNS = [
    "product_name", "product_url", "review_title", "rating", "review_text",
    "pros", "cons", "reviewer_name", "reviewer_title", "company_size",
    "date", "helpful_count", "verified", "review_link", "query", "position",
]


def _blank_row():
    return {c: "" for c in G2_COLUMNS}


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    """A real g2.com page (not a Cloudflare challenge / block)."""
    if r is None or r.status_code != 200:
        return False
    low = (r.text or "").lower()
    if any(s in low for s in ("just a moment", "cf-browser-verification",
                              "attention required", "cf-error-details",
                              "/cdn-cgi/challenge")):
        return False
    return "g2.com" in low or "g2crowd" in low or 'itemprop="review"' in low


def _try(url: str, px: str):
    try:
        r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                     timeout=_G2_TIMEOUT, verify=False, allow_redirects=True)
        return r if _ok(r) else None
    except Exception:
        return None


def _proxied_get(url: str):
    """Fetch through a free proxy (NEVER the real IP): reuse the last known-good proxy, else
    rotate the pool until one passes g2.com and pin it. Raises if none pass."""
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
    raise RuntimeError("no free proxy passed g2.com")


def _get_text(url: str) -> str | None:
    """Fetch one URL through a proxy with curl_cffi. Paid PROXY_URL if set, else rotate the free
    pool. Returns HTML on a real page, None if blocked/failed. NEVER the real IP."""
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


# ---------------- headless-Chrome fetch (better odds vs Cloudflare) ----------------

def _is_real_g2(html: str | None) -> bool:
    """True if the HTML is a real G2 reviews page (challenge cleared + reviews present)."""
    if not html:
        return False
    low = html.lower()
    if any(s in low for s in ("just a moment", "cf-browser-verification",
                              "attention required", "/cdn-cgi/challenge", "enable javascript and cookies")):
        return False
    return ('itemprop="review"' in html) or ('"@type":"review"' in low) or ('"reviewbody"' in low)


def _headless_get_sync(url: str, proxy: str | None) -> str | None:
    """Render the page in headless Chrome so Cloudflare's JS challenge can run and the reviews
    load. Routed through `proxy` when given (NEVER the real IP). Returns HTML or None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    launch_proxy = None
    if proxy:
        launch_proxy = {"server": proxy if proxy.startswith("http") else "http://" + proxy}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True, proxy=launch_proxy,
                                        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(locale="en-US",
                                      user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                  "Chrome/124.0 Safari/537.36"))
            page = ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            # Cloudflare "Just a moment" interstitial runs JS then redirects — wait for the real
            # review markup to appear (poll a few times so the challenge has time to clear).
            for _ in range(6):
                try:
                    if page.query_selector('[itemprop="review"]'):
                        break
                    if "just a moment" not in (page.title() or "").lower():
                        page.wait_for_selector('[itemprop="review"]', timeout=4000)
                        break
                except Exception:
                    pass
                page.wait_for_timeout(2500)
            # reviews lazy-load — scroll to trigger more
            try:
                for _ in range(3):
                    page.evaluate("window.scrollBy(0, document.body.scrollHeight/3)")
                    page.wait_for_timeout(600)
            except Exception:
                pass
            html = page.content()
            browser.close()
            return html
    except Exception:
        return None


def _warm_proxies(n: int = 4) -> list:
    from . import yp_us
    with yp_us._LOCK:
        warm = list(yp_us._GOOD)
    if len(warm) < 4:
        yp_us.ensure_pool(_SEED, 8)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
    return warm[:n]


async def _fetch_html(url: str, stopped=None) -> str | None:
    """Fetch a G2 page as HTML. Headless Chrome first (best chance against Cloudflare), then a
    plain curl fetch as fallback. Always through a proxy — NEVER the real IP."""
    if stopped and stopped():
        return None
    paid = settings.PROXY_URL.strip()
    if paid:
        html = await asyncio.to_thread(_headless_get_sync, url, paid)
        if _is_real_g2(html):
            return html
        return await asyncio.to_thread(_get_text, url)      # curl via paid proxy
    # free pool: try headless through a few warm proxies, then curl rotation
    for px in await asyncio.to_thread(_warm_proxies, 4):
        if stopped and stopped():
            return None
        html = await asyncio.to_thread(_headless_get_sync, url, px)
        if _is_real_g2(html):
            return html
    return await asyncio.to_thread(_get_text, url)


# ---------------- URL building ----------------

def _reviews_base(query: str) -> str:
    """A query may be a full g2.com URL (product or reviews) or a bare slug -> reviews URL."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        u = urlparse(q)
        path = u.path.rstrip("/")
        if not path.endswith("/reviews"):
            path = path + "/reviews"
        return f"{u.scheme}://{u.netloc}{path}"
    slug = q.strip("/").split("/")[-1]
    return f"{BASE}/products/{slug}/reviews"


def _page_url(base: str, page: int, sort: str) -> str:
    params = {}
    order = SORT_PARAM.get(sort or "", "")
    if order:
        params["order"] = order
    if page > 1:
        params["page"] = page
    return base + ("?" + urlencode(params) if params else "")


# ---------------- parsing ----------------

def _mtext(el) -> str:
    """schema.org microdata value: prefer a <meta content="...">, else the element text."""
    if not el:
        return ""
    if el.has_attr("content") and el["content"].strip():
        return el["content"].strip()
    return el.get_text(" ", strip=True)


def _review_from_jsonld(rv) -> dict | None:
    if not isinstance(rv, dict):
        return None
    author = rv.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    rating = ""
    rr = rv.get("reviewRating")
    if isinstance(rr, dict):
        rating = str(rr.get("ratingValue") or "")
    body = rv.get("reviewBody") or rv.get("description") or ""
    title = rv.get("name") or ""
    if not (title or body):
        return None
    return {
        "review_title": title,
        "review_text": body,
        "rating": rating,
        "reviewer_name": author or "",
        "date": rv.get("datePublished") or "",
    }


def _parse_jsonld(html: str):
    """Return (product_name, product_url, [review dicts]) from any ld+json on the page."""
    name = url = ""
    reviews = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types or "SoftwareApplication" in types:
                name = obj.get("name") or name
                url = obj.get("url") or url
                revs = obj.get("review") or obj.get("reviews") or []
                for rv in (revs if isinstance(revs, list) else [revs]):
                    r = _review_from_jsonld(rv)
                    if r:
                        reviews.append(r)
            elif "Review" in types:
                r = _review_from_jsonld(obj)
                if r:
                    reviews.append(r)
    return name, url, reviews


def _company_size(text: str) -> str:
    m = re.search(r"(Small-Business|Mid-Market|Enterprise)\s*\(([^)]*)\)", text)
    return m.group(0) if m else ""


def _helpful(text: str) -> str:
    m = re.search(r"([\d,]+)\s+people found this review helpful", text, re.I)
    return m.group(1).replace(",", "") if m else ""


def _parse(html: str, query: str, base_url: str):
    """Return (review_rows, product_name). Microdata cards first; JSON-LD as the fallback."""
    soup = BeautifulSoup(html, "lxml")
    product_name, product_url, ld_reviews = _parse_jsonld(html)
    if not product_name:
        h1 = soup.select_one("h1")
        product_name = h1.get_text(" ", strip=True) if h1 else ""
    if not product_url:
        product_url = base_url

    rows = []
    for c in soup.select('[itemprop="review"]'):
        row = _blank_row()
        row["product_name"] = product_name
        row["product_url"] = product_url
        row["review_title"] = _mtext(c.select_one('[itemprop="name"]'))
        rmeta = c.select_one('[itemprop="reviewRating"] [itemprop="ratingValue"], [itemprop="ratingValue"]')
        row["rating"] = _mtext(rmeta)
        row["review_text"] = _mtext(c.select_one('[itemprop="reviewBody"]'))
        row["reviewer_name"] = _mtext(c.select_one('[itemprop="author"]'))
        dmeta = c.select_one('[itemprop="datePublished"]')
        row["date"] = _mtext(dmeta)

        card_text = c.get_text(" ", strip=True)
        row["company_size"] = _company_size(card_text)
        row["helpful_count"] = _helpful(card_text)
        row["verified"] = "Yes" if ("Validated Reviewer" in card_text or "Verified" in card_text) else ""

        # pros / cons: G2 prompts each answer; capture the two main blocks when present
        pros, cons = _pros_cons(c)
        row["pros"], row["cons"] = pros, cons
        if not row["review_text"]:
            row["review_text"] = "\n".join(x for x in (pros, cons) if x)

        a = c.select_one('a[href*="#survey-response"], a[href*="/reviews/"]')
        if a and a.get("href"):
            row["review_link"] = urljoin(BASE, a["href"])

        if row["review_title"] or row["review_text"]:
            rows.append(row)

    if not rows and ld_reviews:
        for rv in ld_reviews:
            row = _blank_row()
            row["product_name"] = product_name
            row["product_url"] = product_url
            row.update(rv)
            rows.append(row)

    for r in rows:
        r["query"] = query
    return rows, product_name


def _pros_cons(card) -> tuple[str, str]:
    """Best-effort: G2 splits the answer into 'What do you like best' / 'What do you dislike'
    prompts. Pull the text that follows each prompt within this review card."""
    t = card.get_text("\n", strip=True)
    pros = _section(t, r"like best[^\n]*\n", r"(?:What do you dislike|Recommendations|What problems|Review collected)")
    cons = _section(t, r"dislike[^\n]*\n", r"(?:Recommendations|What problems|Review collected|$)")
    return pros, cons


def _section(text: str, start_re: str, end_re: str) -> str:
    m = re.search(start_re, text, re.I)
    if not m:
        return ""
    rest = text[m.end():]
    e = re.search(end_re, rest, re.I)
    chunk = rest[:e.start()] if e else rest
    return re.sub(r"\s+", " ", chunk).strip()[:2000]


# ---------------- scrape + run loop ----------------

async def scrape(query: str, limit: int | None, sort: str, stopped) -> list[dict]:
    base = _reviews_base(query)
    rows, seen, page = [], set(), 1
    while page <= MAX_PAGES and (not limit or len(rows) < limit):
        if stopped and stopped():
            break
        html = await _fetch_html(_page_url(base, page, sort), stopped)
        if html is None:
            if page == 1 and not settings.PROXY_URL.strip():
                raise RuntimeError(
                    "g2.com blocked every free proxy tried (Cloudflare) — retry, or set a paid "
                    "PROXY_URL in .env. No real IP was used.")
            break
        page_rows, _name = _parse(html, query, base)
        new = 0
        for r in page_rows:
            key = (r.get("reviewer_name"), r.get("review_title"), r.get("date"),
                   (r.get("review_text") or "")[:48])
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
            new += 1
            if limit and len(rows) >= limit:
                break
        if new == 0:
            break
        page += 1
    return rows[:limit] if limit else rows


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in G2_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "") -> None:
    """Background task: scrape each query's reviews through a proxy IP and store the rows."""
    from .scraper import STOP_REQUESTS
    from .db import jobs, g2reviews

    total = 0

    def stopped() -> bool:
        return job_id in STOP_REQUESTS

    try:
        if not settings.PROXY_URL.strip():
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "g2-free-pool"}})
        else:
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "g2-paid-proxy"}})

        for q in queries:
            if stopped():
                break
            rows = await scrape(q, limit, sort, stopped)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await g2reviews.insert_many(rows)
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
