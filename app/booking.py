"""Booking Search Scraper — properties from a booking.com searchresults URL.

booking.com is PerimeterX/HUMAN-protected: it returns a 202 JS-challenge page to datacenter IPs and
to plain automated requests (confirmed even on a real IP), and loads results via a token-gated
GraphQL API. So it CANNOT be scraped on the free tier — every free/datacenter proxy is challenged.
ALL traffic is proxy-only (NEVER the real IP): a paid (ideally residential) PROXY_URL if set, else the
free pool (which Booking challenges → clear error).

Reads Booking's internal data (the embedded JSON-LD `ItemList`/`Hotel` objects the page ships) first,
then the rendered property cards as a fallback. The query is a booking.com searchresults URL; `limit`
caps the rows. Parser is best-effort — finalized once a real (residential-proxy) results page loads.
"""
import asyncio
import json
import re
from datetime import date, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

BOOKING_COLUMNS = ["query", "name", "stars", "rating", "review_label", "reviews", "location",
                   "distance", "room_type", "occupancy", "price", "original_price", "taxes",
                   "deal", "url", "image"]

_REVIEW_LABELS = ("Exceptional", "Wonderful", "Fabulous", "Superb", "Very Good", "Good",
                  "Pleasant", "Review score")
_BED_RE = re.compile(r"\s*\d+\s+(?:twin|queen|king|single|double|sofa|bunk|futon|full|extra-large|"
                     r"large|bed)", re.I)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _with_dates(url: str) -> str:
    """Booking only renders prices/availability when check-in/out dates are present — inject a default
    1-night stay (~2 weeks out) and English locale when the URL has none."""
    try:
        u = urlparse(url)
        q = parse_qs(u.query)
        if "checkin" not in q:
            ci = date.today() + timedelta(days=14)
            q["checkin"] = [ci.isoformat()]
            q["checkout"] = [(ci + timedelta(days=1)).isoformat()]
        q.setdefault("lang", ["en-us"])
        q.setdefault("group_adults", ["2"])
        return urlunparse(u._replace(query=urlencode({k: v[0] for k, v in q.items()})))
    except Exception:
        return url


_COUNT_JS = "document.querySelectorAll('[data-testid=\"property-card\"]').length"


def _headless_get_sync(url: str, proxy: str | None, want: int = 25) -> str | None:
    """Render Booking in headless Chrome THROUGH `proxy` so PerimeterX's JS challenge runs and clears
    (curl can't execute it). PROXY-ONLY: returns None if no proxy is given — the real IP is never
    used. Booking shows only ~25 cards on the first page, so after the challenge clears we scroll +
    click 'Load more results' until `want` cards are loaded (or no more appear)."""
    if not proxy:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    server = proxy if proxy.startswith("http") else "http://" + proxy
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy={"server": server},
                                        args=["--no-sandbox",
                                              "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(locale="en-US", user_agent=_UA)
            page = ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            for _ in range(8):                       # wait for the PerimeterX challenge to clear
                if _has_results(page.content()):
                    break
                page.wait_for_timeout(2500)
            # Booking loads ~25/page — scroll + click "Load more results" until `want` cards exist
            stale = 0
            for _ in range(40):
                try:
                    n = page.evaluate(_COUNT_JS)
                except Exception:
                    break
                if n >= want:
                    break
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1100)
                    btn = page.locator('button:has-text("Load more results")')
                    if btn.count() and btn.first.is_visible():
                        btn.first.click(timeout=4000)
                        page.wait_for_timeout(1800)
                except Exception:
                    pass
                try:
                    n2 = page.evaluate(_COUNT_JS)
                except Exception:
                    n2 = n
                stale = stale + 1 if n2 <= n else 0
                if stale >= 3:                       # no new cards for 3 rounds → results exhausted
                    break
            html = page.content()
            browser.close()
            return html if _has_results(html) else None
    except Exception:
        return None


def _has_results(text: str) -> bool:
    """A real Booking results page (not the 202 PerimeterX challenge)."""
    low = (text or "").lower()
    return ('data-testid="property-card"' in low or '"@type":"hotel"' in low
            or "b_hotel_id" in low or '"propertycards"' in low or "sr_property_block" in low)


def _ok(r) -> bool:
    return r is not None and r.status_code == 200 and _has_results(r.text)


def _get_html(url: str, want: int = 25) -> str:
    """Return a real Booking results page (PerimeterX cleared) with at least `want` property cards
    loaded, PROXY-ONLY — never the real IP. curl is 202-challenged, so the reliable path is a headless
    Chrome render THROUGH a proxy (it executes the challenge JS, clears it, and clicks 'Load more')."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        html = _headless_get_sync(url, proxy, want)    # render + paginate through the paid proxy
        if html:
            return html
        raise RuntimeError("Booking.com PerimeterX was not cleared even via the configured PROXY_URL.")
    # free pool: curl is always 202-challenged, so go straight to a headless render through each proxy
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 4)
    seen = set()
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px in seen:
            continue
        seen.add(px)
        html = _headless_get_sync(url, px, want)       # headless Chrome through the FREE proxy
        if html:
            return html
        if len(seen) >= 4:
            break
    raise RuntimeError("Booking.com PerimeterX challenge could not be cleared by the free proxies — "
                       "try again (it rotates proxies) or set a residential PROXY_URL. No real IP "
                       "was used.")


def _txt(node):
    return node.get_text(" ", strip=True) if node else None


def _from_jsonld(html: str, query: str) -> list[dict]:
    """PRIMARY: Booking ships its results as schema.org JSON-LD (ItemList of Hotel objects)."""
    out = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = []
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") == "ItemList":
                items += [el.get("item", el) for el in obj.get("itemListElement") or []]
            elif obj.get("@type") in ("Hotel", "LodgingBusiness"):
                items.append(obj)
        for it in items:
            if not isinstance(it, dict):
                continue
            agg = it.get("aggregateRating") or {}
            addr = it.get("address") or {}
            out.append({
                "query": query, "name": it.get("name") or "",
                "price": (it.get("priceRange") or ""),
                "rating": str(agg.get("ratingValue") or "") if isinstance(agg, dict) else "",
                "reviews": str(agg.get("reviewCount") or "") if isinstance(agg, dict) else "",
                "location": (addr.get("addressLocality") or addr.get("streetAddress") or "")
                            if isinstance(addr, dict) else "",
                "url": it.get("url") or "", "image": it.get("image") or "",
            })
    return [r for r in out if r["name"]]


def _from_cards(html: str, query: str) -> list[dict]:
    """Fallback: rendered property cards (data-testid attrs — finalize against a real response)."""
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for c in soup.select('[data-testid="property-card"]'):
        name = _txt(c.select_one('[data-testid="title"]'))
        if not name:
            continue
        a = c.select_one('a[data-testid="title-link"]') or c.select_one("a[href]")
        href = a.get("href") if a else None
        url = ("https://www.booking.com" + href) if href and href.startswith("/") else href
        img = c.select_one('img[data-testid="image"]') or c.select_one("img")
        # review block: "Scored 9.9 9.9 Exceptional 41 reviews"
        rev = c.select_one('[data-testid="review-score"]')
        rating, reviews, review_label = "", "", ""
        if rev:
            rtext = rev.get_text(" ", strip=True)
            sm = re.search(r"(\d(?:\.\d)?)", rtext)
            rating = sm.group(1) if sm else ""
            rm = re.search(r"([\d,]+)\s+reviews?", rtext, re.I)
            reviews = rm.group(1).replace(",", "") if rm else ""
            review_label = next((lb for lb in _REVIEW_LABELS if lb in rtext), "")
        # hotel star class (best-effort — count star icons / read the aria-label)
        stars = ""
        star_el = c.select_one('[data-testid="rating-stars"], [data-testid="rating-squares"]')
        if star_el:
            stars = str(len(star_el.select("span, svg")) or "")
        if not stars:
            al = c.select_one('[aria-label*="star"], [aria-label*="Star"]')
            sm2 = re.search(r"(\d)\s*(?:out of \d\s*)?star", al.get("aria-label", "")) if al else None
            stars = sm2.group(1) if sm2 else ""
        # price (current) + original (strikethrough)
        price = _txt(c.select_one('[data-testid="price-and-discounted-price"]'))
        rate = _txt(c.select_one('[data-testid="availability-rate-information"]')) or ""
        if not price:
            pm = re.search(r"(?:₹|€|£|\$|US\$|Rs\.?)\s?[\d,]+", c.get_text(" ", strip=True))
            price = pm.group(0) if pm else None
        om = re.search(r"Original price\s*((?:₹|€|£|\$|US\$|Rs\.?)\s?[\d,]+)", rate, re.I)
        original_price = om.group(1) if om else ""
        # room type (strip the bed/board details after the room name)
        ru = _txt(c.select_one('[data-testid="recommended-units"], [data-testid="availability-single"]'))
        room_type = _BED_RE.split(ru)[0].strip() if ru else ""
        out.append({
            "query": query, "name": name, "stars": stars,
            "rating": rating, "review_label": review_label, "reviews": reviews,
            "location": _txt(c.select_one('[data-testid="address-link"], [data-testid="address"]')),
            "distance": _txt(c.select_one('[data-testid="distance"]')),
            "room_type": room_type,
            "occupancy": _txt(c.select_one('[data-testid="price-for-x-nights"]')),
            "price": price, "original_price": original_price,
            "taxes": _txt(c.select_one('[data-testid="taxes-and-charges"]')),
            "deal": _txt(c.select_one('[data-testid="property-card-deal"]')),
            "url": url, "image": (img.get("src") or img.get("data-src")) if img else None,
        })
    return out


def _with_offset(url: str, offset: int) -> str:
    if offset <= 0:
        return url
    u = urlparse(url)
    q = parse_qs(u.query)
    q["offset"] = [str(offset)]
    return urlunparse(u._replace(query=urlencode({k: v[0] for k, v in q.items()})))


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """Booking shows 25 results/page and paginates by `&offset=25,50,…`. NOTE: on a free/datacenter
    session Booking *ignores* the offset (anti-bot — capped at 25, pagination disabled); a residential
    PROXY_URL re-enables it. So loop offsets and stop when a page brings nothing new."""
    base = _with_dates(query)
    rows, seen = [], set()
    want_total = limit or 1000
    for offset in range(0, 1000, 25):
        if len(rows) >= want_total:
            break
        html = _get_html(_with_offset(base, offset), 25)
        page_rows = _from_cards(html, query) or _from_jsonld(html, query)
        new = [r for r in page_rows if r["name"] not in seen]
        if not new:                                   # free session repeats page 1 → stop here
            break
        for r in new:
            seen.add(r["name"])
            rows.append(r)
            if len(rows) >= want_total:
                break
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from datetime import datetime
    from .db import jobs, booking_results
    total = 0
    last_err = ""
    try:
        for q in queries:
            try:
                rows = await search(q, limit)
            except Exception as qe:
                last_err = str(qe)
                rows = []
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await booking_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = last_err or ("Booking.com returned 0 — PerimeterX challenged the request. "
                                        "Set a paid residential PROXY_URL in .env (the real IP is "
                                        "never used).")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
