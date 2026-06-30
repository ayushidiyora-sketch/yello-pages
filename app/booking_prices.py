"""Booking Prices Scraper — room types + nightly prices for a booking.com hotel (headless, proxy-only).

Same design as the Booking Reviews scraper: Booking is PerimeterX-protected (curl gets a 202
challenge), so we render the hotel page in a headless Chrome — launched THROUGH a proxy (paid
PROXY_URL if set, else the free US pool; `yp_us` only supplies the proxy list, the fetch is the
browser). The real IP is never used. Prices/availability need check-in/check-out dates, which we add
to the URL if missing (~14 days out, 1 night, 2 adults), then read the room rows from the rendered
DOM (room name + price), with the page's embedded Apollo JSON as a fallback.

NOTE: like reviews, Booking gates the live availability/price API for datacenter/free IPs — room names
may render but prices populate reliably only on a residential PROXY_URL. On the free pool you may get
room names with empty prices. Input per line: a booking.com/hotel/<cc>/<slug>.html URL or a bare slug.
"""
import asyncio
import re
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode

from bs4 import BeautifulSoup

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

BOOKING_PRICE_COLUMNS = ["query", "hotel_name", "room_type", "price", "occupancy",
                         "board", "cancellation"]

_PRICE_RE = re.compile(r"(?:US\$|₹|\$|€|£|Rs\.?|INR|USD|EUR|GBP)\s?[\d][\d,]*")


def _to_url(q: str) -> str:
    """A booking.com/hotel URL with check-in/out + occupancy params (added if missing); a bare slug
    becomes a /hotel/us/<slug>.html URL. Dates are required for prices to render."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        u = urlparse(q.split("#")[0])
        base = f"{u.scheme}://{u.netloc}{u.path}"
        params = dict(parse_qsl(u.query))
    else:
        base = f"https://www.booking.com/hotel/us/{q.strip('/').replace('.html', '')}.html"
        params = {}
    if "checkin" not in params:
        ci = datetime.utcnow().date() + timedelta(days=14)
        params["checkin"] = ci.isoformat()
        params["checkout"] = (ci + timedelta(days=1)).isoformat()
    params.setdefault("group_adults", "2")
    params.setdefault("lang", "en-us")
    return base + "?" + urlencode(params)


def _headless_get_sync(url: str, proxy: str | None, stopped=None) -> str | None:
    """Render the hotel page through `proxy` so the PerimeterX JS clears, scroll to the rooms table,
    and return the page HTML (or None if the challenge wasn't cleared)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed — needed for the Booking headless browser.")
    launch_proxy = {"server": proxy if proxy.startswith("http") else "http://" + proxy} if proxy else None
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, proxy=launch_proxy,
                                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="en-US", user_agent=_UA, viewport={"width": 1400, "height": 1200})
        pg = ctx.new_page()
        try:
            pg.goto(url, timeout=50000, wait_until="domcontentloaded")
            pg.wait_for_timeout(3500)
            for _ in range(10):                                # scroll to load the rooms/price table
                if stopped and stopped():
                    break
                pg.mouse.wheel(0, 3000)
                pg.wait_for_timeout(500)
            try:                                               # wait for the rooms table to appear
                pg.wait_for_selector('[data-testid="rt-name-link"], #hprt-table', timeout=12000)
            except Exception:
                pass
            html = pg.content()
        except Exception:
            html = None
        finally:
            browser.close()
    if not html or len(html) < 40000:
        return None
    low = html.lower()
    if any(s in low for s in ("px-captcha", "/sorry/", "are you a human", "access denied")):
        return None
    return html


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


def _price_near(el) -> str:
    """Find a currency-formatted price within a room row by walking up from the room-name element."""
    node = el
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        m = _PRICE_RE.search(node.get_text(" ", strip=True))
        if m:
            return m.group(0)
    return ""


def _text_near(el, *classes_or_words) -> str:
    node = el
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        txt = node.get_text(" ", strip=True)
        for w in classes_or_words:
            if w.lower() in txt.lower():
                return w
    return ""


def _parse(html: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    h = soup.select_one('h2[data-testid="title"]') or soup.select_one("h1") or soup.select_one("title")
    hotel = h.get_text(" ", strip=True).replace(" – Reviews", "").strip() if h else ""

    rows, seen = [], set()
    names = soup.select('[data-testid="rt-name-link"], a.hprt-roomtype-icon-link, .hprt-roomtype-link')
    for nm in names:
        room = nm.get_text(" ", strip=True)
        if not room or room in seen:
            continue
        seen.add(room)
        rows.append({
            "query": query, "hotel_name": hotel, "room_type": room,
            "price": _price_near(nm),
            "occupancy": _text_near(nm, "max", "guests") and "",  # best-effort, often in icons
            "board": _text_near(nm, "Breakfast included", "breakfast"),
            "cancellation": _text_near(nm, "Free cancellation", "Non-refundable"),
        })

    # fallback: pull price blocks from the page's embedded Apollo/JSON state
    if not rows or all(not r["price"] for r in rows):
        for m in re.finditer(r'"grossAmount"\s*:\s*\{[^}]*"amountRounded"\s*:\s*"([^"]+)"', html or ""):
            rows.append({"query": query, "hotel_name": hotel, "room_type": "", "price": m.group(1),
                         "occupancy": "", "board": "", "cancellation": ""})
            if len(rows) >= 40:
                break
    return rows


def search_sync(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    url = _to_url(query)
    stopped = (lambda: bool(job_id and job_id in STOP_REQUESTS))
    html = _render(url, stopped)
    if html is None:
        if settings.PROXY_URL.strip():
            raise RuntimeError("Booking.com did not render through the proxy (PerimeterX/timeout). "
                               "Try a residential PROXY_URL. The real IP is never used.")
        raise RuntimeError("Booking.com blocked every free proxy (PerimeterX). Set a residential "
                           "PROXY_URL for reliable results — the real IP is never used.")
    rows = _parse(html, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, booking_prices_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await booking_prices_results.insert_many(rows)
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
