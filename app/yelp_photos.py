"""Y.E.L.P Photos Scraper — photos from a yelp.com business.

Reuses the Yelp proxy fetch (app/yelp.py — PROXY-ONLY, residential PROXY_URL needed; Yelp hard-blocks
free/datacenter IPs; the real IP is never used). A query is a yelp.com /biz/ URL, a bare business
slug, or a business id alias — it is fetched as the business's photo gallery (/biz_photos/<slug>) and
all Yelp-CDN `bphoto` image URLs are extracted. Best-effort — finalized against a real (residential-
proxy) page.
"""
import asyncio
import re
from datetime import datetime

from .yelp import _get_html, BASE
from .yelp_reviews import _biz_name

YELP_PHOTO_COLUMNS = ["query", "business", "photo_url", "caption"]

_BPHOTO = re.compile(r'https://s3-media\d?\.fl\.yelpcdn\.com/bphoto/[A-Za-z0-9_\-]+/[a-z0-9]+\.(?:jpg|png|webp)')


def _photos_url(query: str, start: int) -> str:
    """A query (/biz/ URL, slug, or id) -> the business photo-gallery URL, paginated by `start`."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        slug = q.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    else:
        slug = q.strip("/")
    url = f"{BASE}/biz_photos/{slug}"
    return f"{url}?start={start}" if start else url


def _parse_photos(html: str, query: str, business: str) -> list[dict]:
    out, seen = [], set()
    for u in _BPHOTO.findall(html or ""):
        u = re.sub(r"/[a-z0-9]+\.(jpg|png|webp)$", r"/o.\1", u)   # normalize to the original size
        if u in seen:
            continue
        seen.add(u)
        out.append({"query": query, "business": business, "photo_url": u, "caption": ""})
    return out


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    rows, seen, business = [], set(), ""
    for page in range(0, 30):
        html = _get_html(_photos_url(query, page * 30))
        if html is None:
            break
        if not business:
            business = _biz_name(html)
        new = [r for r in _parse_photos(html, query, business) if r["photo_url"] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r["photo_url"])
            rows.append(r)
            if limit and len(rows) >= limit:
                return rows
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from .db import jobs, yelp_photos
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await yelp_photos.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("Yelp returned 0 photos — it hard-blocks free/datacenter IPs (the real "
                            "IP is never used). Set a US residential PROXY_URL in .env to scrape it.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
