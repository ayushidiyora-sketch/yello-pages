"""Deliveroo Reviews Scraper — restaurant reviews from deliveroo.co.uk.

A query is a Deliveroo store/menu URL (https://deliveroo.co.uk/menu/<city>/<area>/<id>-<slug>) or a
bare restaurant id / UUID. Reviews come from Deliveroo's consumer reviews API
(api.deliveroo.com/consumer/restaurant-reviews/v1/<id>), fetched through the proxy pool (paid
PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the real IP). One row per review.

`sort` orders the reviews (Highest rated | Lowest rated | Newest | Oldest); `limit` caps reviews per
store. Deliveroo is protected by Cloudflare (JS challenge) — the same aggressive anti-bot tier as
Crunchbase / ZoomInfo. The datacenter free pool (and even a real IP) gets a 403, so live scraping
needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The parser below reads Deliveroo's reviews-API
JSON (and an embedded JSON-LD fallback), so rows come back as soon as a residential proxy is set.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

DR_COLUMNS = [
    "query", "restaurant_id", "author", "rating", "max_rating",
    "review_text", "date", "order_again",
]

# UI sort label -> Deliveroo API sort_by value
SORT_PARAM = {
    "highest": "rating_descending",
    "lowest": "rating_ascending",
    "newest": "time_descending",
    "oldest": "time_ascending",
}

_API = "https://api.deliveroo.com/consumer/restaurant-reviews/v1/{id}"
_PAGE = 20


def _u(v):
    return html.unescape(str(v)) if v else ""


def _store_id(query: str) -> str:
    """A query may be a menu URL, a numeric id, or a UUID — return the restaurant id."""
    q = (query or "").strip()
    if not q:
        return ""
    # UUID
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", q, re.I)
    if m:
        return m.group(0)
    # menu URL .../<id>-<slug>  (id is the digits right after the last slash)
    m = re.search(r"/(\d+)-[^/]+/?$", q)
    if m:
        return m.group(1)
    if q.isdigit():
        return q
    # any trailing numeric token
    m = re.search(r"(\d{3,})", q)
    return m.group(1) if m else ""


def _api_url(sid: str, sort: str, offset: int) -> str:
    val = SORT_PARAM.get((sort or "").lower(), "time_descending")
    return f"{_API.format(id=sid)}?sort_by={val}&offset={offset}&limit={_PAGE}"


def _row(rev: dict, query: str, sid: str) -> dict | None:
    if not isinstance(rev, dict):
        return None
    author = rev.get("consumer_name") or rev.get("customer_name") or rev.get("author") or rev.get("name") or ""
    if isinstance(author, dict):
        author = author.get("name") or author.get("value") or ""
    rating = rev.get("rating") or rev.get("stars") or rev.get("score") or ""
    if isinstance(rating, dict):
        rating = rating.get("value") or rating.get("rating") or ""
    text = rev.get("text") or rev.get("review") or rev.get("comment") or rev.get("body") or rev.get("content") or ""
    date = rev.get("created_at") or rev.get("date") or rev.get("time") or rev.get("published_at") or ""
    again = rev.get("would_order_again")
    if again is None:
        again = rev.get("order_again")
    row = {c: "" for c in DR_COLUMNS}
    row.update({
        "query": query,
        "restaurant_id": sid,
        "author": _u(author),
        "rating": str(rating if rating not in (None, "") else ""),
        "max_rating": "5",
        "review_text": _u(re.sub(r"<[^>]+>", " ", str(text))).strip()[:1000],
        "date": _u(str(date)[:25]),
        "order_again": "yes" if again is True else ("no" if again is False else ""),
    })
    return row


def _extract_reviews(data):
    """Pull the reviews list out of whatever the API returns."""
    if isinstance(data, dict):
        for k in ("reviews", "data", "items", "results"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = v.get("reviews") or v.get("data") or v.get("items")
                if isinstance(inner, list):
                    return inner
        # search one level deeper
        for v in data.values():
            if isinstance(v, dict):
                r = _extract_reviews(v)
                if r:
                    return r
    elif isinstance(data, list):
        return data
    return []


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    sid = _store_id(query)
    if not sid:
        return []
    out, seen = [], set()
    offset = 0
    while True:
        try:
            r = yp_us.pooled_get(_api_url(sid, sort, offset), {"Accept": "application/json"}, timeout=25)
        except Exception:
            break
        if r is None or r.status_code != 200:
            break
        try:
            data = r.json()
        except Exception:
            try:
                data = json.loads(r.text)
            except Exception:
                break
        revs = _extract_reviews(data)
        if not revs:
            break
        new = 0
        for rev in revs:
            row = _row(rev, query, sid)
            if row:
                key = (row["author"], row["date"], row["review_text"][:40])
                if key not in seen:
                    seen.add(key)
                    out.append(row)
                    new += 1
        if limit and len(out) >= limit:
            return out[:limit]
        if new == 0 or len(revs) < _PAGE:
            break
        offset += _PAGE
    return out[:limit] if limit else out


async def search(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in DR_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None, sort: str = "") -> None:
    """Background task: scrape each Deliveroo store's reviews, one row per review."""
    from .db import jobs, deliveroo_reviews
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, sort)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit, sort)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await deliveroo_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "stopped" if stopped else "done", "total_scraped": total,
            "finished_at": datetime.utcnow()}})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
