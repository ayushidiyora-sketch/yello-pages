"""Upwork Jobs Scraper — job listings from an upwork.com search URL (headless, proxy-only).

Upwork is Cloudflare-protected (curl + its RSS feed are 403/410), so — like the G2 scraper — we render
the job-search page in a headless Chrome launched THROUGH a proxy, let Cloudflare's JS challenge clear,
and read the job tiles from the rendered DOM (`article[data-test="JobTile"]`, with an embedded-JSON
fallback). The real IP is never used (`yp_us` only supplies the proxy list; the browser does the fetch).

Input per line: an upwork.com/nx/search/jobs/?… search URL. `sort` maps Relevance / Most recent.
NOTE: Cloudflare's managed challenge clears reliably only on a residential PROXY_URL — on free/datacenter
proxies it stays on "Just a moment" and returns 0. Tile selectors are best-effort and may need tuning.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urljoin

from bs4 import BeautifulSoup

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

UPWORK_COLUMNS = ["query", "title", "url", "posted", "budget", "job_type", "experience_level",
                  "proposals", "skills", "description"]

# UI sort -> Upwork `sort` query value
SORT = {"": "", "relevance": "relevance", "recency": "recency", "most_recent": "recency"}


def _with_sort(url: str, sort: str, page: int) -> str:
    u = urlparse(url)
    params = dict(parse_qsl(u.query))
    sv = SORT.get((sort or "").lower(), "")
    if sv:
        params["sort"] = sv
    if page > 1:
        params["page"] = str(page)
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(params)}"


def _t(card, *selectors) -> str:
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return ""


def _headless_get_sync(url: str, proxy: str | None, stopped=None) -> str | None:
    """Render the Upwork search page through `proxy` so Cloudflare's JS clears; return the HTML (or
    None if the challenge wasn't cleared)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed — needed for the Upwork headless browser.")
    launch_proxy = {"server": proxy if proxy.startswith("http") else "http://" + proxy} if proxy else None
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, proxy=launch_proxy,
                                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="en-US", user_agent=_UA, viewport={"width": 1400, "height": 1000})
        pg = ctx.new_page()
        try:
            pg.goto(url, timeout=50000, wait_until="domcontentloaded")
            for _ in range(6):                                 # wait for Cloudflare to auto-clear
                if stopped and stopped():
                    break
                if "just a moment" not in (pg.title() or "").lower() and pg.query_selector(
                        'article[data-test="JobTile"], [data-test="job-tile-list"] article'):
                    break
                pg.wait_for_timeout(2500)
            for _ in range(4):
                pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(600)
            html = pg.content()
        except Exception:
            html = None
        finally:
            browser.close()
    if not html:
        return None
    low = html.lower()
    if "just a moment" in low or "cf-browser-verification" in low or "/cdn-cgi/challenge" in low:
        return None
    return html if ('data-test="jobtile"' in low or 'data-test="job-tile' in low or "/jobs/" in html) else None


def _render(url: str, stopped=None) -> str | None:
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return _headless_get_sync(url, proxy, stopped)
    try:
        yp_us.ensure_pool(_SEED, 6)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        candidates = warm + yp_us._fetch_candidates()
    except Exception:
        candidates = []
    seen = set()
    for px in candidates:
        if px in seen:
            continue
        seen.add(px)
        if stopped and stopped():
            return None
        try:
            html = _headless_get_sync(url, px, stopped)
        except Exception:
            html = None
        if html:
            with yp_us._LOCK:
                if px in yp_us._GOOD:
                    yp_us._GOOD.remove(px)
                yp_us._GOOD.insert(0, px)
            return html
        if len(seen) >= 6:
            break
    return None


def _parse(html: str, base: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    rows, seen = [], set()
    tiles = (soup.select('article[data-test="JobTile"]')
             or soup.select('[data-test="job-tile-list"] article')
             or soup.select("article.job-tile"))
    for c in tiles:
        a = (c.select_one('[data-test="job-tile-title-link"]')
             or c.select_one('a[data-test="UpLink"]') or c.select_one("h2 a, h3 a"))
        title = a.get_text(" ", strip=True) if a else ""
        href = a["href"] if a and a.has_attr("href") else ""
        url = urljoin(base, href) if href else ""
        if not title or url in seen:
            continue
        seen.add(url)
        skills = " · ".join(s.get_text(" ", strip=True) for s in
                            c.select('[data-test="token"], [data-test="attr-item"]')[:12])
        rows.append({
            "query": query, "title": title, "url": url,
            "posted": _t(c, '[data-test="job-pubilshed-date"]', '[data-test="posted-on"]', "small"),
            "budget": _t(c, '[data-test="budget"]', '[data-test="is-fixed-price"]', '[data-test="job-type-label"]'),
            "job_type": _t(c, '[data-test="job-type-label"]', '[data-test="job-type"]'),
            "experience_level": _t(c, '[data-test="experience-level"]', '[data-test="contractor-tier"]'),
            "proposals": _t(c, '[data-test="proposals"]', '[data-test="proposals-tier"]'),
            "skills": skills,
            "description": _t(c, '[data-test="job-description-text"]',
                              '[data-test="UpCLineClamp JobDescription"]', "p"),
        })
    return rows


def search_sync(query: str, limit: int | None = None, sort: str = "",
                job_id: str | None = None) -> list[dict]:
    u = urlparse(query)
    base = f"{u.scheme}://{u.netloc}" if u.scheme else "https://www.upwork.com"
    rows, seen = [], set()
    for page in range(1, 21):
        if job_id and job_id in STOP_REQUESTS:
            break
        html = _render(_with_sort(query, sort, page), (lambda: bool(job_id and job_id in STOP_REQUESTS)))
        if html is None:
            if page == 1:
                if settings.PROXY_URL.strip():
                    raise RuntimeError("Upwork did not render through the proxy (Cloudflare challenge). "
                                       "Try a residential PROXY_URL. The real IP is never used.")
                raise RuntimeError("Upwork blocked every free proxy (Cloudflare 'Just a moment'). Set a "
                                   "residential PROXY_URL for reliable results — the real IP is never used.")
            break
        page_rows = _parse(html, base, query)
        new = 0
        for r in page_rows:
            if r["url"] not in seen:
                seen.add(r["url"])
                rows.append(r)
                new += 1
        if not new:
            break
        if limit and len(rows) >= limit:
            break
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "",
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "") -> None:
    from .db import jobs, upwork_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, sort, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await upwork_results.insert_many(rows)
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
