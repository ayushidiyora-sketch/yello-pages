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
    "asin", "product_name", "review_id", "title", "rating", "review_text",
    "review_date", "review_location", "author", "verified_purchase",
    "helpful_count", "variant", "images", "review_url", "position", "query",
]

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_IN_URL = re.compile(r"/(?:dp|gp/product|gp/aw/d|product|product-reviews)/([A-Z0-9]{10})")


def _blank_row() -> dict:
    r = {c: "" for c in REVIEW_COLUMNS}
    r["images"] = []
    r["verified_purchase"] = False
    return r


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


def parse_product_name(soup) -> str:
    el = (soup.select_one('a[data-hook="product-link"]')
          or soup.select_one(".product-title")
          or soup.select_one("#productTitle"))
    return amazon._txt(el) or ""


def parse_reviews(html: str, asin: str, domain: str) -> tuple[list[dict], str]:
    """Return (review_rows, product_name) parsed from a product-reviews page."""
    soup = BeautifulSoup(html, "lxml")
    base = f"https://www.{domain}"
    product_name = parse_product_name(soup)
    out = []
    for b in soup.select('div[data-hook="review"], li[data-hook="review"]'):
        row = _blank_row()
        row["asin"] = asin
        row["product_name"] = product_name
        row["review_id"] = b.get("id") or ""

        title_el = b.select_one('[data-hook="review-title"]')
        if title_el:
            spans = title_el.select("span")
            # the visible title is the last span (earlier spans hold the star rating)
            row["title"] = (spans[-1].get_text(strip=True) if spans
                            else title_el.get_text(" ", strip=True))
            href = title_el.get("href") or (title_el.find_parent("a") or {}).get("href")
            if href:
                row["review_url"] = urljoin(base, href)

        ri = b.select_one('[data-hook="review-star-rating"] .a-icon-alt, '
                          '[data-hook="cmps-review-star-rating"] .a-icon-alt, '
                          'i[data-hook="review-star-rating"] span')
        if ri:
            m = re.search(r"([\d.]+)\s+out of 5", ri.get_text())
            if m:
                row["rating"] = m.group(1)

        row["author"] = amazon._txt(b.select_one(".a-profile-name")) or ""
        row["review_text"] = amazon._txt(b.select_one('[data-hook="review-body"]')) or ""
        row["verified_purchase"] = bool(b.select_one('[data-hook="avp-badge"]'))
        row["variant"] = amazon._txt(b.select_one('[data-hook="format-strip"]')) or ""

        date_raw = amazon._txt(b.select_one('[data-hook="review-date"]')) or ""
        row["review_date"] = date_raw
        # "Reviewed in the United States on June 1, 2024" -> location + date
        dm = re.match(r"Reviewed in (.+?) on (.+)$", date_raw)
        if dm:
            row["review_location"] = dm.group(1).strip()
            row["review_date"] = dm.group(2).strip()

        hv = amazon._txt(b.select_one('[data-hook="helpful-vote-statement"]'))
        if hv:
            m = re.search(r"[\d,]+", hv)
            row["helpful_count"] = m.group(0).replace(",", "") if m else ("1" if "One" in hv else "")

        imgs = []
        for im in b.select('[data-hook="review-image-tile"], .review-image-tile'):
            src = im.get("src") or im.get("data-src")
            if src and src not in imgs:
                imgs.append(src)
        row["images"] = imgs

        if row["title"] or row["review_text"]:
            out.append(row)
    return out, product_name


# ---------------- persistence ----------------

async def _save(job_id: str, items: list[dict], seen: set, query: str, position_start: int) -> int:
    added = 0
    for it in items:
        key = it.get("review_id") or (it.get("author"), it.get("title"), it.get("review_date"))
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

async def run_reviews_scrape(job_id: str, queries: list[str], domain: str):
    """Background task: for each ASIN/URL, scrape its reviews through a proxy IP, live."""
    from .scraper import STOP_REQUESTS

    total = 0
    seen: set = set()
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

        for asin, dom in specs:
            if stopped():
                await finish("stopped"); return

            # the product page embeds a "top reviews" widget — try it first (the dedicated
            # /product-reviews/ page is increasingly JS/login-gated and returns no static reviews)
            dp_html = await amazon._fetch(f"https://www.{dom}/dp/{asin}", None, None, dom, stopped)
            if dp_html:
                rows, _ = parse_reviews(dp_html, asin, dom)
                total += await _save(job_id, rows, seen, query=asin, position_start=total)
                await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

            page = 1
            while page <= MAX_REVIEW_PAGES:
                if stopped():
                    await finish("stopped"); return
                html = await amazon._fetch(_reviews_url(dom, asin, page), None, None, dom, stopped)
                if stopped():
                    await finish("stopped"); return
                if not html:
                    break
                rows, _ = parse_reviews(html, asin, dom)
                if not rows:
                    break
                add = await _save(job_id, rows, seen, query=asin, position_start=total)
                total += add
                page += 1
                if add == 0:        # only duplicates -> no new pages worth fetching
                    break
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await finish("stopped" if stopped() else "done")
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow(),
        }})
