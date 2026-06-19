"""Hotels Search Scraper — hotels from a hotels.com Hotel-Search URL.

hotels.com is Expedia Group and uses the same PerimeterX defense — it 429s every free/datacenter
proxy (and challenges automated browsers even on the real IP). So it CANNOT be scraped on the free
tier; it needs a paid residential PROXY_URL. PROXY-ONLY — the real IP is never used. The Hotel-Search
HTML is the same Expedia platform, so it reuses Expedia's hotel parser.
"""
import asyncio

from curl_cffi import requests as cffi

from .config import settings
from .expedia import _parse, _has_hotels


def _ok(r) -> bool:
    return r is not None and r.status_code == 200 and _has_hotels(r.text)


def _get(url: str):
    """Fetch through a proxy — NEVER the real IP. Paid PROXY_URL if set, else fail fast on the free
    pool (hotels.com 429s those → clear blocked error)."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                        timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 3)
    seen = set()
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px in seen:
            continue
        seen.add(px)
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                         timeout=7, verify=False, allow_redirects=True)
            if _ok(r):
                return r
        except Exception:
            pass
        if len(seen) >= 4:
            break
    raise RuntimeError("hotels.com blocks free proxies (429 / PerimeterX) — set a paid residential "
                       "PROXY_URL to scrape it. No real IP was used.")


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    rows = _parse(_get(query).text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from datetime import datetime
    from .db import jobs, hotels_results
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await hotels_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
