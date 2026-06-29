"""Feefo Reviews Scraper — merchant service reviews from feefo.com.

A query is a Feefo company reviews URL (feefo.com/en-GB/reviews/<merchant>) or a bare merchant
identifier. The feefo.com page itself is Cloudflare-protected, but Feefo exposes a free public
reviews JSON API (api.feefo.com/api/10/reviews/all) keyed by `merchant_identifier` — we use that, so
this works on the proxy pool without a residential proxy. Every request goes through a proxy IP
(paid PROXY_URL / PROXY_LIST if set, else the free pool — NEVER the real IP).

`sort` orders the reviews (Newest | Oldest | Most Helpful); `limit` caps reviews per merchant.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlencode

from . import yp_us
from .scraper import STOP_REQUESTS

API = "https://api.feefo.com/api/10/reviews/all"
PAGE_SIZE = 100
MAX_PAGES = 50

# UI sort label -> Feefo API sort value (we also re-sort client-side to guarantee the order)
SORT_PARAM = {"": "", "Newest": "created_desc", "Oldest": "created_asc", "Most Helpful": "helpfulness"}

FEEFO_COLUMNS = [
    "query", "merchant", "customer", "location", "rating", "title", "review",
    "date", "helpful_votes", "review_url",
]


def _merchant_id(query: str) -> str:
    q = (query or "").strip()
    if q.lower().startswith("http"):
        m = re.search(r"/reviews/([^/?#]+)", q)
        if m:
            return m.group(1)
        return q.rstrip("/").split("/")[-1].split("?")[0]
    return q


def _api_url(merchant: str, page: int, sort: str) -> str:
    params = {"merchant_identifier": merchant, "page_size": str(PAGE_SIZE), "page": str(page)}
    val = SORT_PARAM.get(sort or "", "")
    if val:
        params["sort"] = val
    return API + "?" + urlencode(params)


def _row(rv: dict, merchant: str, query: str) -> dict | None:
    if not isinstance(rv, dict):
        return None
    cust = rv.get("customer") or {}
    svc = rv.get("service") or {}
    rating = svc.get("rating") or {}
    if isinstance(rating, dict):
        rating = rating.get("rating")
    row = {c: "" for c in FEEFO_COLUMNS}
    row.update({
        "query": query,
        "merchant": (rv.get("merchant") or {}).get("identifier") or merchant,
        "customer": cust.get("display_name") or "",
        "location": cust.get("display_location") or "",
        "rating": str(rating) if rating is not None else "",
        "title": svc.get("title") or "",
        "review": (svc.get("review") or "").strip(),
        "date": svc.get("created_at") or rv.get("last_updated_date") or "",
        "helpful_votes": str(svc.get("helpful_votes") or ""),
        "review_url": rv.get("url") or "",
    })
    return row if (row["title"] or row["review"] or row["rating"]) else None


def _sort_rows(rows: list[dict], sort: str) -> list[dict]:
    s = sort or ""
    if s == "Oldest":
        return sorted(rows, key=lambda r: r.get("date") or "")
    if s == "Most Helpful":
        return sorted(rows, key=lambda r: int(r.get("helpful_votes") or 0), reverse=True)
    # Newest (default) — Feefo created_at is ISO, sorts lexicographically
    return sorted(rows, key=lambda r: r.get("date") or "", reverse=True)


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    merchant = _merchant_id(query)
    if not merchant:
        return []
    rows, seen, page = [], set(), 1
    while page <= MAX_PAGES:
        try:
            r = yp_us.pooled_get(_api_url(merchant, page, sort), {}, timeout=25)
        except Exception:
            break
        if r is None or r.status_code != 200:
            break
        try:
            data = json.loads(r.text)
        except Exception:
            break
        revs = data.get("reviews") or []
        if not revs:
            break
        added = 0
        for rv in revs:
            row = _row(rv, merchant, query)
            if not row:
                continue
            key = (row["customer"], row["title"], (row["review"] or "")[:48], row["date"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            added += 1
        if not added or len(revs) < PAGE_SIZE:
            break
        if limit and len(rows) >= limit:
            break
        page += 1
    rows = _sort_rows(rows, sort)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in FEEFO_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None, sort: str = "") -> None:
    """Background task: scrape each merchant's Feefo reviews (public API) and store the rows."""
    from .db import jobs, feefo_reviews
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, sort)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit, sort)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await feefo_reviews.insert_many(rows)
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
