"""Feedback Company Scraper — company profiles from feedbackcompany.com.

A query is a Feedback Company reviews URL (https://www.feedbackcompany.com/<locale>/reviews/<slug>).
Each page is fetched through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating
free pool — NEVER the real IP). One row per company with its aggregate profile (name, average score,
review count, website, phone, address). `limit` caps companies per query (one company per URL).

The company profile is server-rendered as a JSON-LD `Organization` with an `aggregateRating`, so this
works on the free pool — no paid/residential proxy required. (For individual reviews, see the
separate Feedback Company Reviews Scraper.)
"""
import asyncio
import html
import json
import re
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

FCC_COLUMNS = [
    "query", "company", "company_id", "rating", "best_rating", "review_count",
    "website", "telephone", "address", "city", "postcode", "country",
]


def _u(v):
    return html.unescape(str(v)) if v else ""


def _org_with_rating(html_text: str) -> dict | None:
    """Return the first JSON-LD Organization that carries an aggregateRating."""
    for s in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html_text, re.S):
        try:
            data = json.loads(s)
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                t = cur.get("@type")
                tset = set(t) if isinstance(t, list) else {t}
                if tset & {"Organization", "LocalBusiness", "Store"} and cur.get("aggregateRating"):
                    return cur
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return None


def _row(html_text: str, query: str) -> dict | None:
    org = _org_with_rating(html_text)
    if not org:
        return None
    agg = org.get("aggregateRating") or {}
    if not isinstance(agg, dict):
        agg = {}
    addr = org.get("address") or {}
    if not isinstance(addr, dict):
        addr = {}
    cid = ""
    m = re.search(r"customers/(\d+)/reviews", html_text)
    if m:
        cid = m.group(1)
    row = {c: "" for c in FCC_COLUMNS}
    row.update({
        "query": query,
        "company": _u(org.get("name")),
        "company_id": cid,
        "rating": str(agg.get("ratingValue") or ""),
        "best_rating": str(agg.get("bestRating") or ""),
        "review_count": str(agg.get("reviewCount") or agg.get("ratingCount") or ""),
        "website": _u(org.get("url")),
        "telephone": _u(org.get("telephone")),
        "address": _u(addr.get("streetAddress")),
        "city": _u(addr.get("addressLocality")),
        "postcode": _u(addr.get("postalCode")),
        "country": _u(addr.get("addressCountry")),
    })
    return row


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = (query or "").strip()
    if not url.lower().startswith("http"):
        return []
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return []
    if r is None or r.status_code != 200:
        return []
    row = _row(r.text, query)
    return [row] if row else []


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in FCC_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Feedback Company URL and store one row per company profile."""
    from .db import jobs, feedbackcompany_companies
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await feedbackcompany_companies.insert_many(rows)
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
