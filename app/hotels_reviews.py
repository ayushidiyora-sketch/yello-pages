"""Hotels Reviews Scraper — guest reviews from a hotels.com hotel URL.

Same Expedia Group / PerimeterX block as the Hotels Search scraper: it 429s every free proxy and
challenges automated browsers, so it CANNOT be scraped on the free tier — proxy-only, real IP never
used, needs a paid residential PROXY_URL. Best-effort review parser (verifiable only with a
residential proxy). Sort: relevant | recent | highest | lowest.
"""
import asyncio
import json
import re

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

_SORT_KEY = {"relevant": "", "recent": "NEWEST_TO_OLDEST", "highest": "HIGHEST_RATED",
             "lowest": "LOWEST_RATED"}


def _is_hotel_page(html: str) -> bool:
    """A genuine hotels.com hotel page (not a PerimeterX challenge or a proxy error page)."""
    if not html:
        return False
    low = html.lower()
    if "px-captcha" in low or "access to this page has been denied" in low or "_pxhd" in low:
        return False
    # markers a real hotel-detail page carries; a generic block/error page won't
    return ('data-stid="property-' in html or '"reviewText"' in html
            or 'data-stid="reviews' in html or 'lodging-pdp' in low)


def _get(url: str):
    """Proxy-only fetch (never the real IP). Paid PROXY_URL if set, else fail fast on the free pool
    (hotels.com 429s those -> clear blocked error)."""
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
    raise RuntimeError("hotels.com blocks free proxies (429 / PerimeterX) — set a paid residential "
                       "PROXY_URL to scrape it. No real IP was used.")


def _txt(n):
    return n.get_text(" ", strip=True) if n else None


def _apollo_state(html: str) -> dict:
    """Decode the page's `window.__APOLLO_STATE__ = JSON.parse("…")` blob (or {})."""
    m = re.search(r'__APOLLO_STATE__\s*=\s*JSON\.parse\("((?:[^"\\]|\\.)*)"\)', html or "")
    if not m:
        return {}
    try:
        return json.loads(json.loads('"' + m.group(1) + '"'))
    except Exception:
        return {}


def _str(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, dict):
            t = v.get("value") or v.get("text") or v.get("primary") or v.get("longForm")
            if isinstance(t, str) and t.strip():
                return t.strip()
    return None


def _parse(html: str, query: str) -> list[dict]:
    """Best-effort: pull individual guest reviews from review cards or the Apollo/embedded data.

    (Verifiable only against a full hotel PDP fetched through a paid residential proxy — the free
    landing page hotels.com serves carries the rating summary but no individual reviews.)"""
    out = []
    soup = BeautifulSoup(html or "", "lxml")
    for c in soup.select('[data-stid*="review-card"], article[itemprop="review"]'):
        text = _txt(c.select_one('[data-stid*="review-text"]')) or _txt(c.select_one("blockquote, p"))
        if not text:
            continue
        out.append({
            "query": query,
            "reviewer": _txt(c.select_one('[data-stid*="reviewer-name"], .reviewer, [itemprop="author"]')),
            "rating": _txt(c.select_one('[data-stid*="rating"], [itemprop="ratingValue"]')),
            "date": _txt(c.select_one('[data-stid*="review-date"], time, [itemprop="datePublished"]')),
            "title": _txt(c.select_one('[data-stid*="review-title"], h3')),
            "review": text,
        })
    if out:
        return out
    # Apollo cache: collect nodes that look like a guest review (have body text + an author/date).
    for v in _apollo_state(html).values():
        if not isinstance(v, dict):
            continue
        tn = v.get("__typename", "")
        body = _str(v.get("text"), v.get("reviewText"), v.get("body"))
        if not body or ("Review" not in tn and "review" not in str(v.get("title", "")).lower()):
            continue
        out.append({
            "query": query,
            "reviewer": _str(v.get("reviewAuthorAttribution"), v.get("authorName"), v.get("author")),
            "rating": _str(v.get("ratingOverall"), v.get("rating"), v.get("reviewScoreWithDescription")),
            "date": _str(v.get("submissionTimeLocalized"), v.get("submissionTime"), v.get("date")),
            "title": _str(v.get("title")),
            "review": body,
        })
    if out:
        return out
    # last resort: embedded JSON review objects anywhere in the document
    for m in re.finditer(r'"(?:reviewText|text)":"((?:[^"\\]|\\.){15,})"', html or ""):
        out.append({"query": query, "reviewer": None, "rating": None, "date": None,
                    "title": None, "review": m.group(1)})
    return out


def search_sync(query: str, limit: int | None = None, sort: str = "relevant") -> list[dict]:
    url = query
    sk = _SORT_KEY.get((sort or "relevant").lower(), "")
    if sk and "sortType" not in url:
        url += ("&" if "?" in url else "?") + f"sortType={sk}"
    rows = _parse(_get(url).text, query)
    if not rows:
        raise RuntimeError(
            "hotels.com serves individual guest reviews through a protected dynamic API "
            "(PerimeterX) — the reachable page only carries the rating summary, not the reviews "
            "themselves. Scraping the reviews needs a paid residential PROXY_URL. No real IP was used.")
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "relevant") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "relevant") -> None:
    from datetime import datetime
    from .db import jobs, hotels_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, sort)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await hotels_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
