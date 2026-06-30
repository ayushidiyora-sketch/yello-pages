"""Feedback Company Reviews Scraper — customer reviews from feedbackcompany.com.

A query is a Feedback Company customer id (e.g. 15626) or a reviews URL
(https://www.feedbackcompany.com/en-en/reviews/<slug>). Reviews come from the site's public JSON
feed `https://www.feedbackcompany.com/nuxt/api/customers/<id>/reviews` (paginated, 15 per page),
fetched through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool —
NEVER the real IP). One row per review.

`sort` orders the reviews (Newest review | Oldest review | Highest stars | Lowest stars); `limit`
caps reviews per company. feedbackcompany.com serves this feed without bot protection, so it works
on the free pool — no paid/residential proxy required.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

FC_COLUMNS = [
    "query", "company_id", "author", "score", "max_score", "recommendation",
    "review_text", "published_at", "response", "response_date",
]

# UI sort label -> Feedback Company order_by/order_direction params
SORT_PARAM = {
    "newest": ("published_at", "desc"),
    "oldest": ("published_at", "asc"),
    "highest": ("score", "desc"),
    "lowest": ("score", "asc"),
}

_API = "https://www.feedbackcompany.com/nuxt/api/customers/{id}/reviews"


def _u(v):
    return html.unescape(str(v)) if v else ""


def _resolve_id(query: str) -> str:
    """A query may be a bare numeric customer id or a reviews URL — return the customer id."""
    q = (query or "").strip()
    if q.isdigit():
        return q
    # try to read the id straight from the page (SEO links + payload both carry customers/<id>/reviews)
    if q.lower().startswith("http"):
        try:
            r = yp_us.pooled_get(q, {}, timeout=25)
        except Exception:
            r = None
        if r is not None and r.status_code == 200:
            m = re.search(r"customers/(\d+)/reviews", r.text)
            if m:
                return m.group(1)
    return ""


def _api_url(cid: str, page: int, sort: str) -> str:
    url = _API.format(id=cid)
    params = []
    ob = SORT_PARAM.get((sort or "").lower())
    if ob:
        params.append(f"order_by={ob[0]}")
        params.append(f"order_direction={ob[1]}")
    params.append(f"page={page}")
    return url + "?" + "&".join(params)


def _row(rev: dict, query: str, cid: str) -> dict | None:
    if not isinstance(rev, dict):
        return None
    resp = rev.get("response") or {}
    if not isinstance(resp, dict):
        resp = {}
    row = {c: "" for c in FC_COLUMNS}
    row.update({
        "query": query,
        "company_id": cid,
        "author": _u(rev.get("respondent")),
        "score": str(rev.get("score") if rev.get("score") is not None else ""),
        "max_score": "10",
        "recommendation": "yes" if rev.get("recommendation") else ("no" if rev.get("recommendation") is False else ""),
        "review_text": _u(rev.get("reviewText")),
        "published_at": _u(rev.get("publishedAt")),
        "response": _u(resp.get("responseText")),
        "response_date": _u(resp.get("responseDate")),
    })
    return row


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    cid = _resolve_id(query)
    if not cid:
        return []
    out, seen = [], set()
    page = 1
    while True:
        try:
            r = yp_us.pooled_get(_api_url(cid, page, sort), {}, timeout=25)
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
        revs = data.get("data") if isinstance(data, dict) else None
        if not isinstance(revs, list) or not revs:
            break
        for rev in revs:
            row = _row(rev, query, cid)
            if row:
                key = row.get("review_text", "") + "|" + str(rev.get("id") or "")
                if key not in seen:
                    seen.add(key)
                    out.append(row)
        if limit and len(out) >= limit:
            return out[:limit]
        meta = data.get("meta") or {}
        last = meta.get("last_page") or page
        if page >= last:
            break
        page += 1
    return out[:limit] if limit else out


async def search(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in FC_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None, sort: str = "") -> None:
    """Background task: scrape each Feedback Company customer's reviews, one row per review."""
    from .db import jobs, feedbackcompany_reviews
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
                await feedbackcompany_reviews.insert_many(rows)
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
