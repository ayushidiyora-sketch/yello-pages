"""TikTok Search Scraper — search results from TikTok.

TikTok's search results are returned by its request-signed API and are NOT embedded in the search
page's HTML, so they can't be scraped without a signing service. This returns a clear status per query.
Proxy-only (real IP never used).
"""
import asyncio
from datetime import datetime
from urllib.parse import quote

from . import tiktok_common as tt

TS_COLUMNS = ["query", "status"]


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    _, status = tt.fetch_scope(f"https://www.tiktok.com/search?q={quote(q)}")
    return [{"query": q, "status": ("blocked / captcha" if status != 200 else tt.API_NOTE)}]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, tiktok_search
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await tiktok_search.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
