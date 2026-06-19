"""Walmart Reviews Scraper — walmart.com product reviews.

Same design as the other live scrapers: every request goes through a proxy IP (paid PROXY_URL
if set, else the rotating free pool — NEVER the real IP). Walmart is bot-protected
(PerimeterX/Akamai), so on the free pool it is often blocked and returns 0 until a paid PROXY_URL
is set. The proxy fetch is shared with app/walmart.py.

A query is a Walmart product URL (/ip/<slug>/<id>); reviews are read from
/reviews/product/<id>. Each query yields up to `limit` reviews. `sort` maps to Walmart's order.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .config import settings
from . import walmart

MAX_PAGES = 20

# dropdown value -> Walmart ?sort= value ("" = Walmart's default ordering)
SORT_PARAM = {
    "": "", "most_relevant": "relevancy", "top_reviews": "helpful",
    "newest": "submission-desc", "oldest": "submission-asc",
    "high_rating": "rating-desc", "low_rating": "rating-asc",
}

# one row per review
WALMART_REVIEW_COLUMNS = [
    "query", "product_name", "item_id", "reviewer", "rating", "title", "text",
    "date", "verified_purchase", "helpful_positive", "helpful_negative", "review_url", "position",
]


def _blank_row():
    return {c: "" for c in WALMART_REVIEW_COLUMNS}


def _reviews_url(query: str, page: int, sort: str):
    m = walmart._ITEM_IN_URL.search(query)
    item_id = m.group(1) if m else (query.rstrip("/").split("/")[-1] if query else "")
    base = f"https://www.walmart.com/reviews/product/{item_id}"
    params = {}
    val = SORT_PARAM.get(sort or "", "")
    if val:
        params["sort"] = val
    if page > 1:
        params["page"] = str(page)
    return base + ("?" + urlencode(params) if params else ""), item_id


# ---------------- parsing ----------------

def _review_from_jsonld(j: dict, product: str, item_id: str, query: str) -> dict | None:
    if not isinstance(j, dict):
        return None
    author = j.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    rating = ""
    rr = j.get("reviewRating")
    if isinstance(rr, dict):
        rating = str(rr.get("ratingValue") or "")
    row = _blank_row()
    row["query"] = query
    row["product_name"] = product
    row["item_id"] = item_id
    row["reviewer"] = author or ""
    row["rating"] = rating
    row["title"] = j.get("name") or ""
    row["text"] = re.sub(r"<[^>]+>", " ", j.get("reviewBody") or "")[:2000].strip()
    row["date"] = j.get("datePublished") or ""
    return row if (row["title"] or row["text"]) else None


def _reviews_from_nextdata(html: str):
    """Pull review objects + product name from Walmart's __NEXT_DATA__ JSON (best-effort)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return [], ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return [], ""
    product, found = "", []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            # a customer review node
            if ("reviewText" in cur or "reviewTitle" in cur) and ("rating" in cur or "reviewRating" in cur):
                found.append(cur)
            if not product and cur.get("name") and (cur.get("usItemId") or cur.get("productId")):
                product = cur.get("name")
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found, product


def _row_from_nextdata(rv: dict, product: str, item_id: str, query: str) -> dict | None:
    row = _blank_row()
    row["query"] = query
    row["product_name"] = product
    row["item_id"] = item_id
    row["reviewer"] = rv.get("userNickname") or rv.get("reviewerId") or ""
    row["rating"] = str(rv.get("rating") or "")
    row["title"] = rv.get("reviewTitle") or ""
    row["text"] = (rv.get("reviewText") or "").strip()
    row["date"] = rv.get("reviewSubmissionTime") or rv.get("submissionTime") or ""
    row["verified_purchase"] = "Yes" if rv.get("verifiedPurchaser") or rv.get("badges") else ""
    row["helpful_positive"] = str(rv.get("positiveFeedback") or "")
    row["helpful_negative"] = str(rv.get("negativeFeedback") or "")
    return row if (row["title"] or row["text"]) else None


def _parse(html: str, query: str, item_id: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()

    # product name (page <h1> as a fallback)
    product = ""
    h1 = soup.select_one("h1")
    if h1:
        product = h1.get_text(" ", strip=True)

    # 1) __NEXT_DATA__ review objects (Walmart's primary source)
    nd_reviews, nd_product = _reviews_from_nextdata(html)
    product = nd_product or product
    for rv in nd_reviews:
        row = _row_from_nextdata(rv, product, item_id, query)
        if row:
            key = (row["reviewer"], row["title"], row["date"], (row["text"] or "")[:40])
            if key not in seen:
                seen.add(key)
                out.append(row)

    # 2) JSON-LD Review fallback
    if not out:
        for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(sc.string or sc.get_text() or "")
            except Exception:
                continue
            for obj in (data if isinstance(data, list) else [data]):
                if not isinstance(obj, dict):
                    continue
                revs = [obj] if obj.get("@type") == "Review" else (obj.get("review") or [])
                for j in (revs if isinstance(revs, list) else [revs]):
                    row = _review_from_jsonld(j, product, item_id, query)
                    if row:
                        key = (row["reviewer"], row["title"], row["date"], (row["text"] or "")[:40])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None, sort: str) -> list[dict]:
    rows, page, item_id = [], 1, ""
    while page <= MAX_PAGES:
        url, item_id = _reviews_url(query, page, sort)
        html = walmart._get_text(url)
        if html is None:
            break   # blocked / no proxy passed — finish quietly (pool already rotated proxies)
        page_rows = _parse(html, query, item_id)
        if not page_rows:
            break
        rows += page_rows
        if limit and len(rows) >= limit:
            break
        page += 1
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None, sort: str) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in WALMART_REVIEW_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "") -> None:
    """Background task: scrape each product's Walmart reviews and store the rows."""
    from .db import jobs, walmart_reviews
    total = 0
    try:
        mode = "walmart-free-pool" if not settings.PROXY_URL.strip() else "walmart-paid-proxy"
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": mode}})

        for q in queries:
            rows = await search(q, limit, sort)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit, sort)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await walmart_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
