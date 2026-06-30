"""Trustpilot Scraper — companies from a trustpilot.com category/profile URL or company ID.

Trustpilot is Cloudflare-protected: curl gets 403, so it needs a real browser. Company data is
embedded in `__NEXT_DATA__` (`props.pageProps.businessUnits.businesses` for a category page, or
`businessUnit` for a /review/ profile). PROXY-ONLY — the browser is routed through a proxy and the
real IP is NEVER used: the dedicated TRUSTPILOT_PROXY_URL (else the global PROXY_URL). Cloudflare
blocks datacenter IPs, so a residential proxy is needed for data; with no proxy (or a blocked one) the
scraper raises a clear "did not load / blocked" error instead of ever touching the real IP.
"""
import asyncio
import json
import queue
import re
import threading
from urllib.parse import urlparse

from .config import settings

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---- single Playwright worker thread (sync API is thread-affine) ----
_jobs: "queue.Queue" = queue.Queue()
_started = False
_start_lock = threading.Lock()


def _worker():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True,
                                 args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    while True:
        fn, holder, done = _jobs.get()
        try:
            holder.append(fn(browser))
        except Exception as e:
            holder.append(e)
        finally:
            done.set()


def _run(fn):
    global _started
    with _start_lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
    holder, done = [], threading.Event()
    _jobs.put((fn, holder, done))
    done.wait()
    res = holder[0]
    if isinstance(res, Exception):
        raise res
    return res


def _proxy_opts(px: str) -> dict:
    u = urlparse(px if "://" in px else "http://" + px)
    o = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        o["username"] = u.username
    if u.password:
        o["password"] = u.password
    return o


def _proxies() -> list[str]:
    """Proxies to route the browser through — PROXY-ONLY, the real IP is NEVER used. The dedicated
    TRUSTPILOT_PROXY_URL (else global PROXY_URL) is tried first, then a few rotating proxies.txt IPs as
    fallback so a slow/blocked IP rotates. Trustpilot's Cloudflare clears on datacenter IPs given enough
    time (the render polls ~24s), so datacenter works. Empty list -> render raises 'blocked', never direct."""
    from . import yp_us
    pin = settings.TRUSTPILOT_PROXY_URL.strip() or settings.PROXY_URL.strip()
    rotating = [p for p in yp_us._plist_candidates(4) if p != pin]
    return ([pin] if pin else []) + rotating


def _render(url: str) -> str:
    """Render `url` through the configured proxy (never the real IP). Returns the HTML once the Next.js
    data loaded; raises if it didn't (or if no proxy is configured)."""
    return _run(lambda browser: _render_through(browser, url, _proxies()))


def _render_through(browser, url: str, proxies: list[str]) -> str:
    attempts = proxies   # PROXY-ONLY: no real-IP fallback — empty list -> raises below, never direct
    for px in attempts:
        kw = {"locale": "en-US", "user_agent": _UA, "viewport": {"width": 1366, "height": 900}}
        if px:
            kw["proxy"] = _proxy_opts(px)
        ctx = browser.new_context(**kw)
        try:
            pg = ctx.new_page()
            pg.goto(url, timeout=35000, wait_until="domcontentloaded")
            # Cloudflare's JS challenge takes ~6-12s to clear (even on datacenter IPs), so poll instead of
            # checking once — give it up to ~24s before moving to the next proxy.
            for _ in range(8):
                pg.wait_for_timeout(3000)
                html = pg.content()
                if "__NEXT_DATA__" in html and "businessUnit" in html:
                    return html
        except Exception:
            pass
        finally:
            ctx.close()
    raise RuntimeError("Trustpilot did not load (Cloudflare challenge / network) — please retry.")


def _query_url(q: str, page: int) -> str:
    """A trustpilot.com URL (category/review) as-is; a bare domain/company ID -> a /review/ URL."""
    q = (q or "").strip()
    base = q.split("?")[0] if q.lower().startswith("http") else f"https://www.trustpilot.com/review/{q}"
    return base + (f"?page={page}" if page > 1 else "")


def _parse(html: str, query: str):
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return [], 0
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return [], 0
    bu = pp.get("businessUnits")
    if isinstance(bu, dict) and isinstance(bu.get("businesses"), list):
        items, pages = bu["businesses"], (bu.get("totalPages") or 1)
    elif isinstance(pp.get("businessUnit"), dict):
        items, pages = [pp["businessUnit"]], 1
    else:
        return [], 0
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
            html = _render(_query_url(query, page))
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
    from .db import jobs, trustpilot_results
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await trustpilot_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
