"""Eventbrite Scraper — event listings from eventbrite.com.

Input is an Eventbrite event URL or numeric event ID. The event detail page embeds a schema.org
JSON-LD `Event` node (fetched THROUGH A PROXY, real IP never used) which is parsed into a flat row.
One row per event.
"""
import asyncio
from datetime import datetime

from . import yp_us, events_common
from .config import settings

EB_COLUMNS = events_common.EVENT_COLUMNS


def _url(q: str) -> str:
    q = (q or "").strip()
    if q.lower().startswith("http"):
        return q
    if q.isdigit():
        return f"https://www.eventbrite.com/e/{q}"
    return q


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = _url(query)
    if not url:
        return []
    try:
        r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None or r.status_code != 200 or not r.text:
        return []
    rows = events_common.events_from_html(r.text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, eventbrite
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await eventbrite.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = "0 events — the event page may have been blocked on the free proxy; retry."
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
