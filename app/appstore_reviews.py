"""AppStore Reviews Scraper — customer reviews for an App Store app.

Uses Apple's public iTunes "customerreviews" RSS JSON feed (no key) THROUGH A PROXY (real IP never
used). Apple serves an empty feed to some IPs, so each page is retried across rotating proxy IPs. Up to
~50 reviews per page, 10 pages (500 max). Input is an apps.apple.com URL or an `id<digits>` / numeric id.
One row per review.
"""
import asyncio
import re
from datetime import datetime

from . import yp_us
from .config import settings

AR_COLUMNS = ["query", "app_id", "author", "rating", "title", "review", "version", "updated"]

_RSS = ("https://itunes.apple.com/{cc}/rss/customerreviews/id={app}/sortby={sort}/page={page}/json")


def _app_id(q: str) -> str:
    m = re.search(r"id(\d{4,})", q or "")
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,})", q or "")
    return m.group(1) if m else ""


def _dig(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def _page(app: str, page: int, sort: str, cc: str = "us") -> list:
    """One page of reviews, retried across rotating proxy IPs (Apple returns empty on some IPs)."""
    url = _RSS.format(cc=cc, app=app, sort=sort, page=page)
    for _ in range(8 if page == 1 else 4):
        try:
            r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
        except Exception:
            continue
        if r is None or r.status_code != 200:
            continue
        try:
            entries = r.json().get("feed", {}).get("entry")
        except ValueError:
            continue
        if isinstance(entries, dict):
            entries = [entries]
        revs = [e for e in (entries or []) if isinstance(e, dict) and e.get("im:rating")]
        if revs:
            return revs
    return []


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    app = _app_id(query)
    if not app:
        return []
    cap = limit if (limit and limit > 0) else 500
    out: list[dict] = []
    for page in range(1, 11):
        revs = _page(app, page, "mostrecent")
        if not revs:
            break
        for e in revs:
            out.append({
                "query": query,
                "app_id": app,
                "author": _dig(e, "author", "name", "label") or "",
                "rating": _dig(e, "im:rating", "label") or "",
                "title": _dig(e, "title", "label") or "",
                "review": _dig(e, "content", "label") or "",
                "version": _dig(e, "im:version", "label") or "",
                "updated": _dig(e, "updated", "label") or "",
            })
            if len(out) >= cap:
                return out
    return out


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, appstore_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await appstore_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("0 reviews — Apple served an empty feed on the free proxy (or the app has "
                            "no reviews in this store); retry.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
