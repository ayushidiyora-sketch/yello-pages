"""BBB Business Reviews Scraper — customer reviews from a bbb.org business.

Reviews are embedded in `__PRELOADED_STATE__.businessProfile.customerReviews.items` on a business's
`/customer-reviews` page. Proxy-only — reuses `bbb._proxied_get` (rotates free proxies until one
passes; NEVER the real IP). A query is a bbb.org reviews URL or a profile URL (we append
`/customer-reviews`). Returns: business, reviewer, rating, date, review, business_response.
"""
import asyncio
import json
import re
from html import unescape

from .bbb import _proxied_get, _clean

MAX_PAGES = 30


def _reviews_url(query: str, page: int) -> str:
    q = (query or "").strip().split("?")[0].rstrip("/")
    if "/customer-reviews" not in q.lower():
        q = q + "/customer-reviews"
    return q + (f"?page={page}" if page > 1 else "")


def _fmt_date(d) -> str | None:
    if isinstance(d, dict) and d.get("month") and d.get("day") and d.get("year"):
        return f"{d['month']}/{d['day']}/{d['year']}"   # MM/DD/YYYY (matches bbb.org)
    return None


def _parse_reviews(html: str, query: str):
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});", html or "", re.S)
    if not m:
        return [], 0
    try:
        bp = (json.loads(m.group(1)).get("businessProfile") or {})
    except (ValueError, json.JSONDecodeError):
        return [], 0
    cr = bp.get("customerReviews") or {}
    business = _clean((bp.get("names") or {}).get("primary"))
    rows = []
    for it in cr.get("items") or []:
        if not isinstance(it, dict):
            continue
        rows.append({
            "query": query,
            "business": business,
            "reviewer": _clean(it.get("displayName")),
            "rating": it.get("reviewStarRating"),
            "date": _fmt_date(it.get("date")),
            "review": unescape((it.get("text") or "").strip()) or None,
            "business_response": unescape((it.get("businessResponseText") or "").strip()) or None,
            "review_id": it.get("id"),
        })
    return rows, cr.get("totalPages") or 0


def _date_key(d: str):
    """'MM/DD/YYYY' -> (year, month, day) for sorting; missing dates sort oldest."""
    m = re.match(r"(\d+)/(\d+)/(\d+)", d or "")
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)


def search_sync(query: str, limit: int | None = None, sort: str = "recent") -> list[dict]:
    """Scrape a business's BBB customer reviews. `limit` caps the rows (blank/None = all).
    `sort`: 'recent' (newest first), 'highest' or 'lowest' (by star rating)."""
    # rating sorts need ALL reviews before trimming; 'recent' can stop at the limit (BBB's default
    # order is already newest-first).
    fetch_limit = None if sort in ("highest", "lowest") else limit
    rows, page, last = [], 1, MAX_PAGES
    while page <= last:
        try:
            html = _proxied_get(_reviews_url(query, page)).text
        except Exception:
            break   # this proxy round failed — finish quietly; the pool already rotated proxies
        page_rows, pages = _parse_reviews(html, query)
        if not page_rows:
            break
        last = min(MAX_PAGES, pages or 1)
        rows += page_rows
        if fetch_limit and len(rows) >= fetch_limit:
            break
        page += 1
    if sort == "highest":
        rows.sort(key=lambda r: r.get("rating") or 0, reverse=True)
    elif sort == "lowest":
        rows.sort(key=lambda r: r.get("rating") or 0)
    else:  # recent
        rows.sort(key=lambda r: _date_key(r.get("date")), reverse=True)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "recent") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "recent") -> None:
    from datetime import datetime
    from .db import jobs, bbbreviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, sort)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit, sort)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await bbbreviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
