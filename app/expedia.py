"""Expedia Search Scraper — hotels from an expedia.com Hotel-Search URL.

Expedia is PerimeterX/HUMAN-protected: it 429s datacenter IPs and shows a captcha to headless
browsers, and it loads hotels via a token-gated GraphQL API. So it CANNOT be scraped on the free
tier — every free proxy is 429'd. ALL traffic is proxy-only (NEVER the real IP): a paid (ideally
residential) PROXY_URL if set, otherwise the free pool (which Expedia blocks → clear error).

The query is an Expedia Hotel-Search URL; `limit` caps the hotels. The parser is best-effort
(verified only once a real results page can be loaded — i.e. with a residential proxy).
"""
import asyncio
import re

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _has_hotels(text: str) -> bool:
    return ('data-stid="property-card"' in text or '"propertyName"' in text
            or "lodging-card" in text or 'data-stid="lodging-card-responsive"' in text)


def _ok(r) -> bool:
    return r is not None and r.status_code == 200 and _has_hotels(r.text)


def _get(url: str):
    """Fetch through a proxy — NEVER the real IP. Paid PROXY_URL if set, else rotate the free pool
    (Expedia 429s those, so this raises a clear 'blocked' error)."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                        timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
    # Expedia 429s every free/datacenter proxy, so a working one is essentially never found —
    # fail FAST: warm just a couple, try only a few with a short timeout, then report blocked.
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
    raise RuntimeError("Expedia blocks free proxies (429 / PerimeterX) — set a paid residential "
                       "PROXY_URL to scrape it. No real IP was used.")


def _txt(node):
    return node.get_text(" ", strip=True) if node else None


def _parse(html: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    cards = (soup.select('[data-stid="property-card"]')
             or soup.select('[data-stid="lodging-card-responsive"]')
             or soup.select('[data-stid*="lodging-card"]')
             or soup.select("div.uitk-card"))
    for c in cards:
        name = _txt(c.select_one('[data-stid="content-hotel-title"]')) or _txt(c.select_one("h3"))
        if not name:
            continue
        a = c.select_one("a[href]")
        href = a.get("href") if a else None
        url = ("https://www.expedia.com" + href) if href and href.startswith("/") else href
        img = c.select_one("img")
        out.append({
            "query": query,
            "name": name,
            "price": _txt(c.select_one('[data-stid="price-summary"]'))
                     or _txt(c.select_one('[data-test-id="price-summary"]')),
            "rating": _txt(c.select_one('[data-stid="content-hotel-reviews-rating"]')),
            "reviews": _txt(c.select_one('[data-stid="content-hotel-reviews-count"]')),
            "location": _txt(c.select_one('[data-stid="content-hotel-neighborhood"]')),
            "deal": _txt(c.select_one('[data-stid="content-hotel-discount-badge"]')),
            "url": url,
            "image": (img.get("src") or img.get("data-src")) if img else None,
        })
    return out


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """Scrape hotels from an Expedia Hotel-Search URL (proxy-only). Raises a clear error when the
    free proxies are blocked."""
    rows = _parse(_get(query).text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from datetime import datetime
    from .db import jobs, expedia_results
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await expedia_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
