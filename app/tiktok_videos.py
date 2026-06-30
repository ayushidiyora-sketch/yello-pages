"""TikTok Videos Scraper — data for a TikTok video URL (or numeric id).

A TikTok video page embeds the full video detail in its `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON
(`webapp.video-detail`), so this works from the page HTML THROUGH A PROXY (real IP never used) — no
signed API needed. One row per video.
"""
import asyncio
from datetime import datetime

from . import tiktok_common as tt

TV_COLUMNS = ["query", "video_id", "author", "description", "create_time", "likes", "comments",
              "shares", "plays", "url"]


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = tt.video_url(query)
    if not url:
        return []
    scope, status = tt.fetch_scope(url)
    vd = (scope.get("webapp.video-detail") or {})
    item = (vd.get("itemInfo", {}) or {}).get("itemStruct", {})
    if not item:
        return [{c: "" for c in TV_COLUMNS} | {"query": query, "url": url,
                "description": ("blocked / not found (TikTok served a captcha or removed video)"
                                if status != 200 else "no data")}]
    stats = item.get("statsV2") or item.get("stats") or {}
    author = item.get("author", {}) or {}
    return [{
        "query": query,
        "video_id": item.get("id") or "",
        "author": author.get("uniqueId") or "",
        "description": item.get("desc") or "",
        "create_time": item.get("createTime") or "",
        "likes": stats.get("diggCount") or "",
        "comments": stats.get("commentCount") or "",
        "shares": stats.get("shareCount") or "",
        "plays": stats.get("playCount") or "",
        "url": url,
    }]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, tiktok_videos
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await tiktok_videos.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
