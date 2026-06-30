"""TikTok Hashtags Scraper — data from a TikTok hashtag page.

A hashtag's video list loads via TikTok's request-signed API and is NOT in the page HTML, so the videos
can't be scraped without a signing service. When the page does embed challenge metadata
(`webapp.challenge-detail`) the hashtag's view/video counts are returned; otherwise a clear status. One
row per hashtag. Proxy-only (real IP never used).
"""
import asyncio
from datetime import datetime

from . import tiktok_common as tt

TH_COLUMNS = ["query", "hashtag", "views", "videos", "status"]


def _tag(q: str) -> str:
    q = (q or "").strip().lstrip("#")
    if q.lower().startswith("http"):
        return q.rstrip("/").split("/tag/")[-1]
    return q


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    tag = _tag(query)
    if not tag:
        return []
    scope, status = tt.fetch_scope(f"https://www.tiktok.com/tag/{tag}")
    row = {"query": query, "hashtag": f"#{tag}", "views": "", "videos": "", "status": ""}
    cd = (scope.get("webapp.challenge-detail") or {})
    info = (cd.get("challengeInfo", {}) or {})
    stats = info.get("statsV2") or info.get("stats") or {}
    if stats:
        row["views"] = stats.get("viewCount") or ""
        row["videos"] = stats.get("videoCount") or ""
        row["status"] = "ok (counts only — video list needs TikTok's signed API)"
    else:
        row["status"] = ("blocked / captcha" if status != 200 else tt.API_NOTE)
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, tiktok_hashtags
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await tiktok_hashtags.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
