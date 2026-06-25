"""YouTube Transcripts Scraper — full-text transcripts from YouTube videos.

Uses the maintained `youtube-transcript-api` library, routed through a proxy IP (paid PROXY_URL if
set, else the rotating free pool — the real IP is never used). A query is a video id or any YouTube
URL (watch?v=, youtu.be/, /shorts/, /embed/). Returns one row per video with the joined transcript
text, language, and segment count.
"""
import asyncio
import re
from datetime import datetime

from .config import settings

YT_COLUMNS = ["query", "video_id", "language", "segments", "transcript"]

_VID_RE = re.compile(r"(?:v=|youtu\.be/|/watch/|/shorts/|/embed/|/v/)([A-Za-z0-9_-]{11})")


def _video_id(query: str) -> str:
    q = (query or "").strip()
    m = _VID_RE.search(q)
    if m:
        return m.group(1)
    return q if re.fullmatch(r"[A-Za-z0-9_-]{11}", q) else ""


def _fetch(vid: str, proxy: str | None) -> dict:
    from youtube_transcript_api import YouTubeTranscriptApi
    cfg = None
    if proxy:
        from youtube_transcript_api.proxies import GenericProxyConfig
        cfg = GenericProxyConfig(http_url=proxy, https_url=proxy)
    ytt = YouTubeTranscriptApi(proxy_config=cfg)
    t = ytt.fetch(vid)
    segs = list(t)
    text = " ".join((s.text if hasattr(s, "text") else s["text"]) for s in segs).strip()
    return {"transcript": text, "language": getattr(t, "language_code", "") or "",
            "segments": len(segs)}


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    vid = _video_id(query)
    if not vid:
        raise RuntimeError(f"Could not read a YouTube video id from '{query}' (use an id or a "
                           "watch?v= / youtu.be/ URL).")
    paid = settings.PROXY_URL.strip()
    if paid:
        proxies = [paid]
    else:
        from . import yp_us
        yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "New York, NY", "page": "1"}, 8)
        with yp_us._LOCK:
            proxies = list(yp_us._GOOD)[:8]
    last = ""
    for px in proxies:
        try:
            d = _fetch(vid, px)
            if d["transcript"] or d["segments"]:
                return [{"query": query, "video_id": vid, **d}]
        except Exception as e:
            last = str(e)
            continue
    if last:
        raise RuntimeError(last)
    return []


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, yt_transcripts
    total = 0
    last_err = ""
    try:
        for q in queries:
            try:
                rows = await search(q)
            except Exception as qe:
                last_err = str(qe)
                rows = []
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await yt_transcripts.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = last_err or ("No transcripts — the video may have captions disabled, or "
                                        "the proxy was blocked (the real IP is never used). Try again "
                                        "or set a PROXY_URL.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
