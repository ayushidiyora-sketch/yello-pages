"""Trustpilot Search Scraper — companies matching a keyword search on trustpilot.com.

PROXY-ONLY, COMPULSORY: the headless browser is ALWAYS routed through a proxy and the real IP is
NEVER used — a paid PROXY_URL first if one is set, otherwise the free rotating pool (yp_us). Because
Trustpilot is Cloudflare-protected and free proxies are datacenter IPs, the free pool is often
challenged → a clear "all proxies blocked" error and 0 results; the pool rotates, so retries can get
through. Input is a plain search keyword (e.g. "real estate"); results come from /search?query=…
`__NEXT_DATA__` (`props.pageProps.businessUnits` list + `pagination`).
"""
import asyncio
import json
import re
from urllib.parse import quote

from .config import settings
from . import trustpilot as tp

# the proxy that last rendered a page successfully — tried first so pages 2..N don't re-probe.
_pinned: str | None = None
_MAX_TRY = 8  # proxies attempted per render before giving up


def _free_proxies() -> list[str]:
    """Ordered, de-duped proxies to route the browser through — NEVER the real IP. Paid PROXY_URL
    first if set, then the last-good pin, then the warm free pool, then fresh free candidates."""
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "Trustpilot", "geo_location_terms": "US", "page": "1"}, 4)
    ordered: list[str] = []
    paid = settings.PROXY_URL.strip()
    if paid:
        ordered.append(paid)
    if _pinned:
        ordered.append(_pinned)
    with yp_us._LOCK:
        ordered += list(yp_us._GOOD)
    ordered += yp_us._fetch_candidates()
    seen, out = set(), []
    for px in ordered:
        if px and px not in seen:
            seen.add(px)
            out.append(px)
    return out[:_MAX_TRY]


def _search_url(q: str, page: int) -> str:
    """A trustpilot.com search URL as-is; otherwise a keyword -> /search?query=<keyword>."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        base = q
    else:
        base = f"https://www.trustpilot.com/search?query={quote(q)}"
    sep = "&" if "?" in base else "?"
    return base + (f"{sep}page={page}" if page > 1 else "")


def _render(url: str) -> str:
    """Render the search page through a proxy — ALWAYS, never the real IP. Tries each proxy from the
    free pool (paid PROXY_URL first if set); pins the one that works for the next pages."""
    global _pinned

    def fn(browser):
        global _pinned
        proxies = _free_proxies()
        if not proxies:
            raise RuntimeError("No proxy available — the free pool is empty and no PROXY_URL is set. "
                               "Trustpilot search uses ONLY proxy IPs (real IP never used). Retry.")
        for px in proxies:
            kw = {"locale": "en-US", "user_agent": tp._UA,
                  "viewport": {"width": 1366, "height": 900},
                  "proxy": tp._proxy_opts(px)}   # px is never None -> real IP is never used
            ctx = browser.new_context(**kw)
            try:
                pg = ctx.new_page()
                pg.goto(url, timeout=35000, wait_until="domcontentloaded")
                pg.wait_for_timeout(3000)
                html = pg.content()
                if "__NEXT_DATA__" in html and '"businessUnits"' in html:
                    _pinned = px
                    return html
            except Exception:
                pass
            finally:
                ctx.close()
            if px == _pinned:   # the pinned proxy just failed — stop trusting it
                _pinned = None
        raise RuntimeError("Trustpilot search used only proxy IPs (no real IP) — every free proxy "
                           "was blocked by Cloudflare or failed to connect. Retry (the free pool "
                           "rotates) or set a paid residential PROXY_URL.")
    return tp._run(fn)


def _parse(html: str, query: str):
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return [], 0
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return [], 0
    items = pp.get("businessUnits")
    if not isinstance(items, list):
        return [], 0
    pages = (pp.get("pagination") or {}).get("totalPages") or 1
    out = []
    for b in items:
        if not isinstance(b, dict) or not b.get("displayName"):
            continue
        loc = b.get("location") or {}
        contact = b.get("contact") or {}
        cats = b.get("categories") or []
        out.append({
            "query": query,
            "name": b.get("displayName"),
            "domain": b.get("identifyingName"),
            "rating": b.get("trustScore"),
            "stars": b.get("stars"),
            "reviews": b.get("numberOfReviews"),
            "category": cats[0].get("displayName") if cats and isinstance(cats[0], dict) else None,
            "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None,
            "website": contact.get("website"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "url": f"https://www.trustpilot.com/review/{b['identifyingName']}" if b.get("identifyingName") else None,
        })
    return out, pages


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    rows, page, last = [], 1, 20
    while page <= last:
        try:
            html = _render(_search_url(query, page))
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


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from datetime import datetime
    from .db import jobs, trustpilot_search_results
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await trustpilot_search_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
