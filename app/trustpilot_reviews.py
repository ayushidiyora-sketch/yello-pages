"""Trustpilot Reviews Summary — individual reviews from a trustpilot.com /review/<company> page.

PROXY-ONLY, COMPULSORY: the headless browser is ALWAYS routed through a proxy (free pool, paid
PROXY_URL first if set) and the real IP is NEVER used. Trustpilot is Cloudflare-protected and free
proxies are datacenter IPs, so the free pool is usually challenged → a clear "all proxies blocked"
error and 0 reviews; the pool rotates, so retries can pass. Reviews live in the page's
`__NEXT_DATA__` (`props.pageProps.reviews` + `businessUnit`).
"""
import asyncio
import json
import re

from . import trustpilot as tp
from .trustpilot_search import _free_proxies

# common Trustpilot review languages offered in the UI dropdown
LANGS = ["all", "en", "es", "fr", "de", "it", "nl", "pt", "da", "sv", "no", "fi", "pl"]


def _review_url(q: str, page: int, language: str = "all") -> str:
    """A trustpilot.com /review/ URL as-is; a bare domain/company id -> a /review/ URL. Adds the page
    and (optional) language filter."""
    q = (q or "").strip()
    base = q if q.lower().startswith("http") else f"https://www.trustpilot.com/review/{q}"
    base = base.split("?")[0]
    params = []
    if language and language.lower() != "all":
        params.append(f"languages={language.lower()}")
    if page > 1:
        params.append(f"page={page}")
    return base + ("?" + "&".join(params) if params else "")


def _render(url: str) -> str:
    """Render through a proxy — ALWAYS, never the real IP. Tries the free pool (paid first if set)."""
    def fn(browser):
        proxies = _free_proxies()
        if not proxies:
            raise RuntimeError("No proxy available — the free pool is empty and no PROXY_URL is set. "
                               "Trustpilot reviews use ONLY proxy IPs (real IP never used). Retry.")
        for px in proxies:
            ctx = browser.new_context(locale="en-US", user_agent=tp._UA,
                                      viewport={"width": 1366, "height": 900},
                                      proxy=tp._proxy_opts(px))   # px is never None -> no real IP
            try:
                pg = ctx.new_page()
                pg.goto(url, timeout=35000, wait_until="domcontentloaded")
                pg.wait_for_timeout(3000)
                html = pg.content()
                if "__NEXT_DATA__" in html and '"reviews"' in html:
                    return html
            except Exception:
                pass
            finally:
                ctx.close()
        raise RuntimeError("Trustpilot reviews used only proxy IPs (no real IP) — every free proxy "
                           "was blocked by Cloudflare or failed to connect. Retry (the free pool "
                           "rotates) or set a paid residential PROXY_URL.")
    return tp._run(fn)


def _dig(d, *path):
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        else:
            return None
    return d


def _parse(html: str, query: str):
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return [], 0
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return [], 0
    reviews = pp.get("reviews")
    if not isinstance(reviews, list):
        return [], 0
    bu = pp.get("businessUnit") or {}
    biz = bu.get("displayName")
    biz_rating = bu.get("trustScore") or _dig(bu, "score", "trustScore")
    biz_reviews = bu.get("numberOfReviews") or _dig(bu, "numberOfReviews", "total")
    pages = _dig(pp, "filters", "pagination", "totalPages") or 1
    if pages == 1 and isinstance(biz_reviews, (int, float)) and biz_reviews:
        pages = min(20, -(-int(biz_reviews) // 20))  # ceil
    out = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        out.append({
            "query": query,
            "business": biz,
            "business_rating": biz_rating,
            "business_reviews": biz_reviews,
            "reviewer": _dig(r, "consumer", "displayName") or r.get("reviewer"),
            "rating": r.get("rating") if r.get("rating") is not None else r.get("stars"),
            "date": _dig(r, "dates", "publishedDate") or _dig(r, "dates", "experiencedDate") or r.get("date"),
            "title": r.get("title"),
            "review": r.get("text"),
            "reply": _dig(r, "reply", "message"),
            "language": r.get("language"),
        })
    return out, pages


def search_sync(query: str, limit: int | None = None, language: str = "all") -> list[dict]:
    rows, page, last = [], 1, 20
    while page <= last:
        try:
            html = _render(_review_url(query, page, language))
        except Exception:
            if page == 1:
                raise
            break
        page_rows, pages = _parse(html, query)
        if not page_rows:
            break
        last = min(20, pages or 1)
        rows += page_rows
        if limit and len(rows) >= limit:
            break
        page += 1
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, language: str = "all") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, language)


async def run_job(job_id: str, queries: list[str], limit: int | None, language: str = "all") -> None:
    from datetime import datetime
    from .db import jobs, trustpilot_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await trustpilot_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
