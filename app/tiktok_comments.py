"""TikTok Comments Scraper — comments from a TikTok video.

Comments are loaded by TikTok's request-signed comment/list API and are NOT in the video page HTML, so
they can't be scraped without a signing service. The video's comment COUNT (from the embedded video
detail) is returned along with a clear status. One row per video. Proxy-only (real IP never used).
"""
import asyncio
from datetime import datetime

from . import tiktok_common as tt

TC_COLUMNS = ["query", "video_id", "comment_count", "status"]


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = tt.video_url(query)
    if not url:
        return []
    scope, status = tt.fetch_scope(url)
    item = ((scope.get("webapp.video-detail") or {}).get("itemInfo", {}) or {}).get("itemStruct", {})
    stats = item.get("statsV2") or item.get("stats") or {}
    return [{
        "query": query,
        "video_id": item.get("id") or "",
        "comment_count": stats.get("commentCount") or "",
        "status": ("blocked / captcha" if status != 200 else tt.API_NOTE),
    }]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, tiktok_comments
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await tiktok_comments.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
