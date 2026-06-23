"""Google Search Images Scraper — image results for a query via Bing's image search (proxy-only).

Free, no key: fetches Bing's image-search async endpoint through the proxy pool (`yp_us.pooled_get`
— paid PROXY_URL if set, else a free-pool proxy; the REAL IP is never used) and reads the JSON
embedded in each result cell (the `m="{…}"` attribute). DuckDuckGo's image API now hard-403s, and
Google Images is anti-bot heavy; Bing is the reliable free source. Each query → image rows: title,
full image URL, thumbnail, source page + host, width/height.
"""
import asyncio
import html
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from . import yp_us
from .scraper import STOP_REQUESTS

BING = "https://www.bing.com/images/async"

GIMG_COLUMNS = ["query", "title", "image", "thumbnail", "source", "link", "width", "height"]

_IUSC = re.compile(r'class="iusc"[^>]*\sm="([^"]+)"')


def _host(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").replace("www.", "")
    except Exception:
        return ""


def search_sync(query: str, limit: int | None = None, country: str = "us",
                language: str = "en", job_id: str | None = None) -> list[dict]:
    headers = {"Accept-Language": f"{(language or 'en')}-{(country or 'us').upper()},"
                                  f"{(language or 'en')};q=0.9"}
    rows: list[dict] = []
    seen, first = set(), 1
    for _page in range(25):                       # ~35 images/page, hard cap
        if job_id and job_id in STOP_REQUESTS:    # Stop button pressed mid-pagination
            break
        params = {"q": query, "first": str(first), "count": "35", "mmasync": "1"}
        r = yp_us.pooled_get(BING, params, timeout=20, headers=headers)
        if r is None or r.status_code != 200:
            if rows:
                break
            raise RuntimeError("Could not reach Bing image search through a proxy (set a PROXY_URL, "
                               "or wait for the free pool to warm up). The real IP is never used.")
        cells = _IUSC.findall(r.text)
        if not cells:
            break
        added = 0
        for c in cells:
            try:
                m = json.loads(html.unescape(c))
            except Exception:
                continue
            img = m.get("murl") or ""
            if not img or img in seen:
                continue
            seen.add(img)
            added += 1
            rows.append({
                "query": query,
                "title": m.get("t") or "",
                "image": img,
                "thumbnail": m.get("turl") or "",
                "source": _host(m.get("purl") or img),
                "link": m.get("purl") or "",
                "width": str(m.get("mw") or ""),
                "height": str(m.get("mh") or ""),
            })
            if limit and len(rows) >= limit:
                return rows
        if not added:
            break
        first += 35
    return rows


async def search(query: str, limit: int | None = None, country: str = "us",
                 language: str = "en", job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, country, language, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None, country: str,
                  language: str) -> None:
    from .db import jobs, gimages_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:               # Stop button pressed
                break
            rows = await search(q, limit, country, language, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gimages_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "stopped" if stopped else "done", "total_scraped": total,
            "finished_at": datetime.utcnow()}})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
