"""AppStore Search Scraper — App Store / iTunes search results for a term.

Uses Apple's free iTunes Search API (`itunes.apple.com/search`, no key) THROUGH A PROXY (real IP never
used). Input is an `apps.apple.com/<cc>/search?term=…` URL (country + term are read from it) or a plain
keyword. Returns each result's name, artist/developer, kind, genre, price, rating, reviews and URL.
One row per result.
"""
import asyncio
from datetime import datetime
from urllib.parse import quote, urlparse, parse_qs

from . import yp_us
from .config import settings

ASS_COLUMNS = ["query", "name", "artist", "kind", "genre", "price", "rating", "reviews", "url"]

_API = "https://itunes.apple.com/search?term={term}&country={cc}&media={media}&limit={limit}"


def _parse(query: str) -> tuple:
    """(country, term) from an apps.apple.com search URL, else (us, keyword)."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        u = urlparse(q)
        parts = [p for p in u.path.split("/") if p]
        cc = parts[0] if parts and len(parts[0]) == 2 else "us"
        term = (parse_qs(u.query).get("term") or [""])[0]
        return cc.lower(), term
    return "us", q


def search_sync(query: str, limit: int | None = None, media: str = "all") -> list[dict]:
    cc, term = _parse(query)
    if not term:
        return []
    cap = min(limit or 100, 200)  # Apple caps at 200
    url = _API.format(term=quote(term), cc=cc, media=media or "all", limit=cap)
    r = None
    for _ in range(4):  # Apple is IP-flaky — retry across rotating proxy IPs
        try:
            r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
        except Exception:
            continue
        if r is not None and r.status_code == 200:
            break
    if r is None or r.status_code != 200:
        return []
    try:
        results = r.json().get("results") or []
    except ValueError:
        return []
    rows = []
    for x in results:
        rows.append({
            "query": query,
            "name": x.get("trackName") or x.get("collectionName") or "",
            "artist": x.get("artistName") or x.get("sellerName") or "",
            "kind": x.get("kind") or x.get("wrapperType") or "",
            "genre": x.get("primaryGenreName") or "",
            "price": x.get("formattedPrice") or (str(x.get("price")) if x.get("price") is not None else ""),
            "rating": x.get("averageUserRating") if x.get("averageUserRating") is not None else "",
            "reviews": x.get("userRatingCount") if x.get("userRatingCount") is not None else "",
            "url": x.get("trackViewUrl") or x.get("collectionViewUrl") or "",
        })
    return rows[:cap]


async def search(query: str, limit: int | None = None, media: str = "all") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, media)


async def run_job(job_id: str, queries: list, limit: int | None = None, media: str = "all") -> None:
    from .db import jobs, appstore_search
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, media)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await appstore_search.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = "0 results — Apple may have throttled the free proxy; retry."
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
