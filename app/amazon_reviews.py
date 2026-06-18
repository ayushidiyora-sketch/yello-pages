"""Amazon Reviews scraper.

Same design as the Amazon Products scraper (app/amazon.py): proxy-only curl_cffi fetches,
cooperative Stop, live save to Mongo. Input is the same kind of lines (ASIN / product URL /
product-reviews URL) plus file upload; the only filters are Domain + the input box.

For each product it pulls the reviews Amazon serves on /product-reviews/<asin> (a few pages,
best-effort — Amazon gates deep pagination behind login). One row per review.
"""
import asyncio
import json
import os
import re
from datetime import datetime
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from .config import settings
from .db import jobs, reviews
from . import amazon, yp_us

MAX_REVIEW_PAGES = 6          # pages fetched per product (best-effort)

# Fixed export schema (one row per review)
REVIEW_COLUMNS = [
    "id", "product_asin", "title", "body", "rating", "rating_text", "helpful",
    "comments", "date", "bage", "official_comment_banner", "url", "img_url",
    "variation", "total_reviews", "overall_rating", "autor_name", "autor_descriptor",
    "autor_url", "autor_profile_img", "product_name", "product_url",
]

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_IN_URL = re.compile(r"/(?:dp|gp/product|gp/aw/d|product|product-reviews)/([A-Z0-9]{10})")


def _blank_row() -> dict:
    return {c: "" for c in REVIEW_COLUMNS}


def _headless_get_sync(url: str, proxy: str | None) -> str | None:
    """Render a page in headless Chrome (so JS-loaded public reviews appear) and return the
    HTML. Routed through `proxy` when given (no real IP). Returns None on any failure."""
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
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            # reviews lazy-load — scroll down a few times to trigger the widget, then wait
            try:
                for _ in range(4):
                    page.evaluate("window.scrollBy(0, document.body.scrollHeight/4)")
                    page.wait_for_timeout(700)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_selector('[data-hook="review"]', timeout=8000)
            except Exception:
                pass
            html = page.content()
            browser.close()
            return html
    except Exception:
        return None


async def _rv_fetch(url: str, stop=None) -> str | None:
    """Fetch a review page via headless Chrome through the proxy pool (best-effort).
    Tries a few warm proxies; returns the first render that contains review blocks (else the
    last usable HTML). Falls back to the plain curl fetch if Playwright isn't available."""
    if stop and stop():
        return None
    paid = settings.PROXY_URL.strip()
    if paid:
        html = await asyncio.to_thread(_headless_get_sync, url, paid)
        return html or await amazon._fetch(url, None, None, "amazon.com", stop)

    with yp_us._LOCK:
        warm = list(yp_us._GOOD)
    if len(warm) < 4:
        await asyncio.to_thread(yp_us.ensure_pool, amazon.SEED_PARAMS, 8)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
    fallback = None
    for px in warm[:5]:
        if stop and stop():
            return None
        html = await asyncio.to_thread(_headless_get_sync, url, px)
        if html and 'data-hook="review"' in html:
            return html
        if html and fallback is None:
            fallback = html
    return fallback


def classify(line: str, domain: str):
    """A line -> (asin, effective_domain) or None. Accepts a bare ASIN, a product URL,
    or a /product-reviews/ URL. A full URL drives its own marketplace."""
    s = (line or "").strip()
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        m = _ASIN_IN_URL.search(s)
        if not m:
            return None
        return (m.group(1).upper(), amazon._domain_of(s) or domain)
    if _ASIN_RE.match(s.upper()) and any(c.isdigit() for c in s):
        return (s.upper(), domain)
    return None


def _reviews_url(domain: str, asin: str, page: int = 1) -> str:
    base = f"https://www.{domain}/product-reviews/{asin}/"
    return base + "?" + urlencode({
        "pageNumber": page, "sortBy": "recent", "reviewerType": "all_reviews",
    })


def parse_page_meta(soup) -> tuple[str, str, str]:
    """Page-level: (product_name, overall_rating, total_reviews) — same for every review."""
    name = amazon._txt(soup.select_one('a[data-hook="product-link"]')
                       or soup.select_one(".product-title")
                       or soup.select_one("#productTitle")) or ""
    overall = ""
    el = soup.select_one("#acrPopover")
    if el and el.get("title"):
        m = re.search(r"([\d.]+)", el["title"]); overall = m.group(1) if m else ""
    if not overall:
        alt = soup.select_one('[data-hook="rating-out-of-text"], #averageCustomerReviews .a-icon-alt')
        if alt:
            m = re.search(r"([\d.]+)", alt.get_text()); overall = m.group(1) if m else ""
    total = ""
    tel = soup.select_one('#acrCustomerReviewText, [data-hook="total-review-count"]')
    if tel:
        m = re.search(r"[\d,]+", tel.get_text()); total = m.group(0).replace(",", "") if m else ""
    return name, overall, total


def parse_reviews(html: str, asin: str, domain: str) -> tuple[list[dict], str]:
    """Return (review_rows, product_name) shaped to the fixed REVIEW_COLUMNS schema."""
    soup = BeautifulSoup(html, "lxml")
    base = f"https://www.{domain}"
    product_name, overall_rating, total_reviews = parse_page_meta(soup)
    product_url = f"{base}/dp/{asin}"
    out = []
    for b in soup.select('[data-hook="review"]'):
        row = _blank_row()
        row["id"] = b.get("id") or ""
        row["product_asin"] = asin
        row["product_name"] = product_name
        row["product_url"] = product_url
        row["total_reviews"] = total_reviews
        row["overall_rating"] = overall_rating

        # title (+ review url). New markup: data-hook="reviewTitle" (<h5> inside <a>).
        title_el = b.select_one('[data-hook="reviewTitle"], [data-hook="review-title"]')
        if title_el:
            t = re.sub(r"^\s*[\d.]+\s+out of 5 stars\s*", "", title_el.get_text(" ", strip=True))
            row["title"] = t.strip()
            a = title_el if title_el.name == "a" else title_el.find_parent("a")
            href = (a.get("href") if a else None) or title_el.get("href")
            if href:
                row["url"] = urljoin(base, href)

        ri = b.select_one('[data-hook="review-star-rating"] .a-icon-alt, '
                          '[data-hook="cmps-review-star-rating"] .a-icon-alt')
        if ri:
            row["rating_text"] = ri.get_text(strip=True)
            m = re.search(r"([\d.]+)\s+out of 5", row["rating_text"])
            if m:
                row["rating"] = m.group(1)

        body_el = (b.select_one('[data-hook="reviewRichContentContainer"]')
                   or b.select_one('[data-hook="reviewText"]')
                   or b.select_one('[data-hook="review-body"]'))
        row["body"] = amazon._txt(body_el) or ""

        # author name / profile url / avatar / descriptor
        row["autor_name"] = amazon._txt(b.select_one(".a-profile-name")) or ""
        prof = b.select_one("a.a-profile")
        if prof and prof.get("href"):
            row["autor_url"] = urljoin(base, prof["href"])
        av = b.select_one(".a-profile-avatar img")
        if av:
            row["autor_profile_img"] = av.get("src") or av.get("data-src") or ""
        row["autor_descriptor"] = amazon._txt(b.select_one(".a-profile-descriptor")) or ""

        date_raw = amazon._txt(b.select_one('[data-hook="review-date"]')) or ""
        dm = re.match(r"Reviewed in .+? on (.+)$", date_raw)
        row["date"] = dm.group(1).strip() if dm else date_raw

        row["bage"] = (amazon._txt(b.select_one('[data-hook="avp-badge"]'))
                       or amazon._txt(b.select_one('.c7y-badge-text, [data-hook="vine-badge"]')) or "")
        row["official_comment_banner"] = amazon._txt(
            b.select_one('[data-hook="review-comment-official"], .review-official-comment')) or ""
        row["variation"] = amazon._txt(b.select_one('[data-hook="format-strip"]')) or ""

        hv = amazon._txt(b.select_one('[data-hook="helpful-vote-statement"]'))
        if hv:
            m = re.search(r"[\d,]+", hv)
            row["helpful"] = m.group(0).replace(",", "") if m else ("1" if "One" in hv else "")
        cm = amazon._txt(b.select_one('[data-hook="review-comment-count"], .review-comments-count'))
        if cm:
            m = re.search(r"[\d,]+", cm)
            row["comments"] = m.group(0).replace(",", "") if m else ""

        imgs = []
        for im in b.select('[data-hook="review-image-tile"], .review-image-tile img, img[data-hook="review-image-tile"]'):
            src = im.get("src") or im.get("data-src")
            if src and src not in imgs:
                imgs.append(src)
        row["img_url"] = ", ".join(imgs)

        if row["title"] or row["body"]:
            out.append(row)
    return out, product_name


# ---------------- persistence ----------------

async def _save(job_id: str, items: list[dict], seen: set, query: str, position_start: int) -> int:
    added = 0
    for it in items:
        key = it.get("id") or (it.get("autor_name"), it.get("title"), it.get("date"))
        if not key or key in seen:
            continue
        seen.add(key)
        it["query"] = query
        it["position"] = position_start + added + 1
        it["job_id"] = job_id
        it["scraped_at"] = datetime.utcnow()
        try:
            await reviews.insert_one(it)
            added += 1
        except Exception:
            pass
    return added


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in REVIEW_COLUMNS}


async def export_reviews(job_id: str) -> str:
    os.makedirs("exports", exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("exports", f"amazon_reviews_{job_id[:8]}_{ts}.json")
    rows = [to_export(d) async for d in reviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    return path


# ---------------- run loop ----------------

async def run_reviews_scrape(job_id: str, queries: list[str], domain: str, limit: int = 20):
    """Background task: for each ASIN/URL, scrape up to `limit` reviews through a proxy IP, live."""
    from .scraper import STOP_REQUESTS

    total = 0
    seen: set = set()
    limit = max(1, limit or 20)
    domain = (domain or "amazon.com").strip().lstrip(".") or "amazon.com"

    def stopped() -> bool:
        return job_id in STOP_REQUESTS

    async def finish(status: str):
        STOP_REQUESTS.discard(job_id)
        path = await export_reviews(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": status, "total_scraped": total,
            "finished_at": datetime.utcnow(), "export_path": path,
        }})

    try:
        specs = [s for s in (classify(q, domain) for q in queries) if s]
        if not specs:
            await finish("done"); return

        if not settings.PROXY_URL.strip():
            await asyncio.to_thread(yp_us.ensure_pool, amazon.SEED_PARAMS, 8)
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "amazon-free-pool"}})
        else:
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "amazon-paid-proxy"}})

        async def scrape_product(asin: str, dom: str) -> int:
            """Scrape one product's reviews (dp widget + review pages), up to `limit`. Returns count."""
            added = 0
            # product page embeds a public "top reviews" widget (rendered via headless)
            dp_html = await _rv_fetch(f"https://www.{dom}/dp/{asin}", stopped)
            if dp_html and not stopped():
                rows, _ = parse_reviews(dp_html, asin, dom)
                added += await _save(job_id, rows[:limit - added], seen, query=asin, position_start=total + added)
            page = 1
            while added < limit and page <= MAX_REVIEW_PAGES and not stopped():
                html = await _rv_fetch(_reviews_url(dom, asin, page), stopped)
                if stopped() or not html:
                    break
                rows, _ = parse_reviews(html, asin, dom)
                if not rows:
                    break
                a = await _save(job_id, rows[:limit - added], seen, query=asin, position_start=total + added)
                added += a
                page += 1
                if a == 0:        # only duplicates -> no new pages worth fetching
                    break
            return added

        for asin, dom in specs:
            if stopped():
                await finish("stopped"); return
            add = await scrape_product(asin, dom)
            # free proxies are flaky — if a product yielded nothing, give it one more try
            if add == 0 and not stopped():
                add = await scrape_product(asin, dom)
            total += add
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await finish("stopped" if stopped() else "done")
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow(),
        }})
