"""Booking Reviews Scraper — guest reviews for a booking.com hotel (headless browser, proxy-only).

Booking is PerimeterX-protected: curl gets a 202 challenge (no JS), so — exactly like this project's
Booking Search scraper — we render the hotel page in a headless Chrome that runs the challenge JS and
generates the PerimeterX cookies itself, then parse the review cards out of the rendered DOM
(`[data-testid="review-card"]`, with the featured-review cards + JSON-LD as fallbacks).

PROXY-ONLY: the browser is launched THROUGH a proxy (a paid PROXY_URL if set, else the free US pool —
`yp_us` only supplies the proxy list; the fetch is the browser). The real IP is never used. Free
datacenter proxies are often still challenged, so we rotate through several until one renders a real
page. Input per line: a booking.com/hotel/<cc>/<slug>.html URL (preferred) or a bare hotel slug.
"""
import asyncio
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

BOOKING_REVIEW_COLUMNS = ["query", "reviewer_name", "reviewer_country", "score", "review_title",
                          "liked", "disliked", "date", "stay_date", "room_type", "traveler_type"]

# UI sort -> the option label inside Booking's reviews "Sort" menu (best-effort; "" = default)
SORT = {
    "": "", "most_relevant": "", "newest": "Most recent", "oldest": "Oldest first",
    "highest": "Highest score", "lowest": "Lowest score",
}


def _params_from_url(q: str):
    """A booking.com/hotel/<cc>/<slug>.html URL as-is (+ required params); a bare slug -> a URL."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        base = q.split("#")[0]
    else:
        slug = q.strip("/").replace(".html", "")
        base = f"https://www.booking.com/hotel/us/{slug}.html"
    return base


def _is_review_page(html: str | None) -> bool:
    if not html or len(html) < 40000:
        return False
    low = html.lower()
    if any(s in low for s in ("px-captcha", "/sorry/", "are you a human", "access denied")):
        return False
    return ('data-testid="review-card"' in html or 'data-testid="featuredreviewcard-text"' in html
            or 'data-testid="featuredreview-text"' in html or '"reviewbody"' in low)


def _headless_get_sync(url: str, proxy: str | None, want_limit: int | None = None,
                       stopped=None) -> str | None:
    """Render the hotel page in headless Chrome (through `proxy`; None = real IP). Opens the
    all-reviews panel and pages through it, collecting every review card's HTML (the modal swaps
    cards per page, so we grab them after each page). Returns a synthetic HTML of all collected
    review cards (or the page HTML for the featured-review fallback); None if the challenge wasn't
    cleared. `want_limit` stops paging once enough reviews are collected."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed — needed for the Booking headless browser.")
    launch_proxy = {"server": proxy if proxy.startswith("http") else "http://" + proxy} if proxy else None
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, proxy=launch_proxy,
                                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="en-US", user_agent=_UA, viewport={"width": 1400, "height": 1000})
        pg = ctx.new_page()
        collected: dict = {}

        def grab():
            try:
                for h in pg.eval_on_selector_all('[data-testid="review-card"]',
                                                 "els => els.map(e => e.outerHTML)"):
                    collected[h[:160]] = h
            except Exception:
                pass

        try:
            pg.goto(url, timeout=45000, wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)                          # let the PerimeterX JS settle
            for _ in range(6):                                 # scroll the reviews block into view
                if stopped and stopped():
                    break
                pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(500)
            try:                                               # wait for the featured reviews to render
                pg.wait_for_selector('[data-testid="featuredreviewcard-text"], '
                                     '[data-testid="featuredreview-text"]', timeout=8000)
            except Exception:
                pass
            page_html = pg.content()                           # featured reviews baseline (BEFORE the
            #                                                    modal, which can hide them if it fails)
            # open the all-reviews panel (JS-click reliably triggers the modal) and wait for the
            # review list to load. On a residential IP the modal's review-list GraphQL returns the
            # full scored cards; on free/datacenter proxies it's gated -> stays empty (featured only).
            opened = False
            for sel in ('[data-testid="fr-read-all-reviews"]',
                        '[data-testid="review-score-read-all-actionable"]',
                        '[data-testid="review-score-read-all"]',
                        '[data-testid="Property-Header-Nav-Tab-Trigger-reviews"]'):
                try:
                    pg.eval_on_selector(sel, "el => el.click()")
                except Exception:
                    continue
                try:                                           # wait for the scored review cards
                    pg.wait_for_selector('[data-testid="review-card"]', timeout=15000)
                    opened = True
                    break
                except Exception:
                    if pg.query_selector('[role="dialog"]'):   # modal opened but list gated -> stop trying
                        break
            grab()                                             # grab whatever cards rendered
            if opened:
                stale = 0
                for _ in range(60):                            # page through the modal
                    if stopped and stopped():
                        break
                    if want_limit and len(collected) >= want_limit:
                        break
                    before = len(collected)
                    moved = False
                    for nx in ('[data-testid="review-list-pagination-button"]',
                               'button[aria-label*="Next"]', 'button[aria-label*="next"]'):
                        try:
                            if pg.query_selector(nx):
                                pg.eval_on_selector(nx, "el => el.click()")
                                pg.wait_for_timeout(1600)
                                moved = True
                                break
                        except Exception:
                            continue
                    if not moved:                              # no pager -> try lazy-scroll the panel
                        pg.mouse.wheel(0, 3000)
                        pg.wait_for_timeout(1200)
                    grab()
                    stale = stale + 1 if len(collected) == before else 0
                    if stale >= 2:                             # nothing new twice in a row -> done
                        break
            # prefer the full modal cards; else the pre-modal page (featured); else the final state
            candidates = []
            if collected:
                candidates.append("<html><body>" + "".join(collected.values()) + "</body></html>")
            candidates += [page_html, pg.content()]
            html = next((h for h in candidates if _is_review_page(h)), None)
        except Exception:
            html = None
        finally:
            browser.close()
    return html


def _render(url: str, want_limit: int | None = None, stopped=None) -> str | None:
    """Render via the paid PROXY_URL, else rotate the free pool until a proxy clears the challenge."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return _headless_get_sync(url, proxy, want_limit, stopped)
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
            html = _headless_get_sync(url, px, want_limit, stopped)
        except Exception:
            html = None
        if html:
            with yp_us._LOCK:                                  # pin the working proxy to the front
                if px in yp_us._GOOD:
                    yp_us._GOOD.remove(px)
                yp_us._GOOD.insert(0, px)
            return html
        if len(seen) >= 6:                                     # free datacenter IPs rarely pass — cap it
            break
    return None


def _txt(card, *selectors) -> str:
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t.strip("“”\"")
    return ""


def _name_country(av) -> tuple[str, str]:
    """Reviewer name + country from an avatar block. Drops the single-letter avatar initial that
    Booking renders for reviewers without a photo (e.g. ['N','Neeta','United States'] -> Neeta)."""
    if not av:
        return "", ""
    parts = [s for s in av.stripped_strings if len(s.strip()) > 1]
    name = parts[0] if parts else ""
    country = parts[-1] if len(parts) > 1 else ""
    return name, country


def _parse(html: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    rows, seen = [], set()

    def add(row):
        key = (row["reviewer_name"], row["review_title"][:40], row["liked"][:40], row["disliked"][:40])
        if (row["liked"] or row["disliked"] or row["review_title"]) and key not in seen:
            seen.add(key)
            rows.append(row)

    # 1) full review cards (the all-reviews modal/list)
    for c in soup.select('[data-testid="review-card"]'):
        name, country = _name_country(c.select_one('[data-testid="review-avatar"]'))
        score_raw = _txt(c, '[data-testid="review-score"]')          # e.g. "Scored 10 10"
        m = re.search(r"\d+(?:\.\d+)?", score_raw)
        add({
            "query": query,
            "reviewer_name": name,
            "reviewer_country": country,
            "score": m.group(0) if m else "",
            "review_title": _txt(c, '[data-testid="review-title"]'),
            "liked": _txt(c, '[data-testid="review-positive-text"]'),
            "disliked": _txt(c, '[data-testid="review-negative-text"]'),
            "date": _txt(c, '[data-testid="review-date"]'),
            "stay_date": _txt(c, '[data-testid="review-stay-date"]'),
            "room_type": _txt(c, '[data-testid="review-room-name"]'),
            "traveler_type": _txt(c, '[data-testid="review-traveler-type"]'),
        })

    # 2) fallback: featured-review cards rendered on the hotel page (name + country + text)
    if not rows:
        for c in soup.select('[data-testid="featuredreviewcard-text"], [data-testid="featuredreview-text"]'):
            text = c.get_text(" ", strip=True).strip("“”\"")
            block = c                                          # walk up to the card container
            for _ in range(5):
                if block is None or block.select_one('[data-testid*="avatar"]'):
                    break
                block = block.parent
            av = block.select_one('[data-testid*="avatar"]') if block else None
            name, country = _name_country(av)
            if text:
                add({**dict.fromkeys(BOOKING_REVIEW_COLUMNS, ""), "query": query,
                     "reviewer_name": name, "reviewer_country": country, "liked": text})
    return rows


def search_sync(query: str, limit: int | None = None, sort: str = "",
                job_id: str | None = None) -> list[dict]:
    url = _params_from_url(query)
    if not url:
        return []
    stopped = (lambda: bool(job_id and job_id in STOP_REQUESTS))
    html = _render(url, limit, stopped)
    if html is None:
        if settings.PROXY_URL.strip():
            raise RuntimeError("Booking.com did not render through the proxy (PerimeterX / timeout). "
                               "Try a residential PROXY_URL. The real IP is never used.")
        raise RuntimeError("Booking.com blocked every free proxy (PerimeterX challenge). Set a "
                           "residential PROXY_URL for reliable results — the real IP is never used.")
    rows = _parse(html, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "",
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "") -> None:
    from .db import jobs, booking_reviews_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, sort, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await booking_reviews_results.insert_many(rows)
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
