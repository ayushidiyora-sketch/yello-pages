"""Meetup Scraper — event listings from meetup.com.

Input is a Meetup event URL, a /find/ search URL, or a plain keyword. The page embeds schema.org
JSON-LD `Event` nodes (a find page lists several; an event page has one), fetched THROUGH A PROXY (real
IP never used). One row per event.
"""
import asyncio
from datetime import datetime
from urllib.parse import quote

from . import yp_us, events_common
from .config import settings

MU_COLUMNS = events_common.EVENT_COLUMNS


def _url(q: str) -> str:
    q = (q or "").strip()
    if q.lower().startswith("http"):
        return q
    return f"https://www.meetup.com/find/?keywords={quote(q)}&source=EVENTS"


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
    from .db import jobs, meetup
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await meetup.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = "0 events — Meetup may have been blocked on the free proxy; retry."
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
