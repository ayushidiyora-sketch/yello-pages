"""Expedia Reviews Scraper — guest reviews from an expedia.com hotel URL.

Expedia is Expedia Group / PerimeterX (the same defense as hotels.com): it 429s every free/datacenter
proxy, so it CANNOT be scraped on the free tier — it needs a paid residential PROXY_URL. PROXY-ONLY,
the real IP is never used. Same Expedia-platform review structure as hotels.com, so it reuses the
Hotels Reviews parser. Sort: relevant | recent | highest | lowest.
"""
import asyncio
import re

from curl_cffi import requests as cffi

from .config import settings
from .hotels_reviews import _parse, _is_hotel_page  # same Expedia Group review structure

_SORT_KEY = {"relevant": "", "recent": "NEWEST_TO_OLDEST", "highest": "HIGHEST_RATED",
             "lowest": "LOWEST_RATED"}


def _get(url: str):
    """Proxy-only fetch (never the real IP). Paid PROXY_URL if set, else fail fast on the free pool
    (expedia.com 429s those -> clear blocked error)."""
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
            if r is not None and r.status_code == 200 and _is_hotel_page(r.text):
                return r
        except Exception:
            pass
        if len(seen) >= 4:
            break
    raise RuntimeError("expedia.com blocks free proxies (PerimeterX) — set a paid residential "
                       "PROXY_URL to scrape it. No real IP was used.")


def _to_url(q: str, sort: str = "relevant") -> str:
    """A full expedia.com hotel URL as-is; a bare hotel id (e.g. 41313) -> a Hotel-Information URL.
    Adds the review sort param when not 'relevant'."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        url = q
    elif re.fullmatch(r"h?\d+", q):
        hid = q.lstrip("hH")
        url = f"https://www.expedia.com/h{hid}.Hotel-Information"
    else:
        url = q
    sk = _SORT_KEY.get((sort or "relevant").lower(), "")
    if sk and "sortType" not in url:
        url += ("&" if "?" in url else "?") + f"sortType={sk}"
    return url


def search_sync(query: str, limit: int | None = None, sort: str = "relevant") -> list[dict]:
    rows = _parse(_get(_to_url(query, sort)).text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "relevant") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "relevant") -> None:
    from datetime import datetime
    from .db import jobs, expedia_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, sort)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await expedia_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
