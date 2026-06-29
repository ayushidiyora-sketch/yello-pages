"""Thuisbezorgd Reviews Scraper — restaurant reviews from thuisbezorgd.nl (Just Eat Takeaway, NL).

A query is a thuisbezorgd.nl restaurant menu URL (…/menu/<slug>) or a bare restaurant slug/ID. Reviews
are read from the restaurant's menu page. Every request goes through a proxy IP (paid PROXY_URL /
PROXY_LIST if set, else the rotating free pool — NEVER the real IP).

Thuisbezorgd is Cloudflare-protected ("Just a moment…"), so on free/datacenter IPs it is blocked and
returns 0 until a paid RESIDENTIAL PROXY_URL is set (same behaviour as the Trustpilot / Kununu
scrapers). It is a Next.js/React app: reviews live in embedded JSON (__NEXT_DATA__ / JSON-LD Review);
we extract them with a DOM fallback. `limit` caps reviews per restaurant.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

BASE = "https://www.thuisbezorgd.nl"
MAX_PAGES = 20
_TIMEOUT = 20
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

TB_REVIEW_COLUMNS = [
    "query", "restaurant", "reviewer", "rating", "title", "text", "date",
    "delivery", "food", "service",
]


def _blank_row():
    return {c: "" for c in TB_REVIEW_COLUMNS}


# ---------------- URL ----------------

def _menu_url(query: str) -> str:
    """A thuisbezorgd restaurant URL -> as-is; a bare slug/id -> /en/menu/<slug>."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q.split("?")[0].rstrip("/")
    slug = re.sub(r"[^a-z0-9-]+", "-", q.lower()).strip("-")
    return f"{BASE}/en/menu/{slug}"


def _page_url(base: str, page: int) -> str:
    return base + (f"?reviewsPage={page}" if page > 1 else "")


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    """A real thuisbezorgd page (not a Cloudflare 'Just a moment' challenge)."""
    if r is None or r.status_code != 200:
        return False
    low = (r.text or "").lower()
    if any(s in low for s in ("just a moment", "/cdn-cgi/challenge", "captcha-delivery",
                              "access denied", "attention required")):
        return False
    return "__next_data__" in low or "thuisbezorgd" in low or "takeaway" in low


def _get_text(url: str) -> str | None:
    """Fetch one URL through a proxy. Paid PROXY_URL if set, else rotate the free/list pool.
    Returns HTML on a real page, None if blocked/failed. NEVER the real IP."""
    headers = {"Accept-Language": "en-US,en;q=0.9,nl;q=0.8"}
    proxy = settings.PROXY_URL.strip()
    if proxy:
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                         timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True,
                         headers=headers)
        except Exception:
            return None
        return r.text if _ok(r) else None
    try:
        yp_us.ensure_pool(_SEED, 6)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        candidates = warm + yp_us._fetch_candidates()
    except Exception:
        candidates = []
    for px in candidates[:8]:
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                         timeout=_TIMEOUT, verify=False, allow_redirects=True, headers=headers)
        except Exception:
            continue
        if _ok(r):
            return r.text
    return None


# ---------------- parsing ----------------

def _restaurant_name(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return re.split(r"\s*[|–-]\s*", og["content"])[0].strip()
    h1 = soup.select_one("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def _is_review_node(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    has_text = any(k in d for k in ("text", "comment", "reviewText", "body", "title"))
    has_score = any(k in d for k in ("rating", "score", "ratingValue", "total", "stars"))
    return has_text and has_score


def _num(v):
    if isinstance(v, dict):
        v = v.get("rating") or v.get("score") or v.get("value") or v.get("ratingValue")
    try:
        return str(round(float(v), 2)) if v is not None and str(v).strip() != "" else ""
    except Exception:
        return str(v or "")


def _row_from_node(d: dict, restaurant: str, query: str) -> dict | None:
    author = d.get("author") or d.get("reviewer") or d.get("name") or d.get("consumerName")
    if isinstance(author, dict):
        author = author.get("name") or author.get("displayName") or ""
    txt = d.get("text") or d.get("comment") or d.get("reviewText") or d.get("body") or ""
    row = _blank_row()
    row["query"] = query
    row["restaurant"] = restaurant
    row["reviewer"] = author or ""
    row["rating"] = _num(d.get("rating") or d.get("score") or d.get("ratingValue") or d.get("total"))
    row["title"] = d.get("title") or d.get("headline") or ""
    row["text"] = re.sub(r"<[^>]+>", " ", str(txt))[:2000].strip()
    row["date"] = (d.get("date") or d.get("createdAt") or d.get("datePublished")
                   or d.get("orderDate") or d.get("time") or "")
    # Thuisbezorgd shows delivery / food / service sub-scores on many reviews
    scores = d.get("scores") or d.get("ratings") or {}
    if isinstance(scores, dict):
        row["delivery"] = _num(scores.get("delivery") or scores.get("deliveryTime"))
        row["food"] = _num(scores.get("food") or scores.get("quality"))
        row["service"] = _num(scores.get("service"))
    return row if (row["text"] or row["title"] or row["rating"]) else None


def _reviews_from_nextdata(html: str):
    """Pull review objects + restaurant name from __NEXT_DATA__ JSON (best-effort tree walk)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return [], ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return [], ""
    restaurant, found = "", []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if _is_review_node(cur):
                found.append(cur)
            if not restaurant and cur.get("name") and (cur.get("restaurantId") or cur.get("slug")
                                                        or cur.get("primarySlug")):
                restaurant = cur.get("name")
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found, restaurant


def _reviews_from_jsonld(soup: BeautifulSoup, restaurant: str, query: str) -> list[dict]:
    out = []
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            revs = [obj] if obj.get("@type") == "Review" else (obj.get("review") or [])
            for j in (revs if isinstance(revs, list) else [revs]):
                if isinstance(j, dict):
                    j2 = dict(j)
                    rr = j.get("reviewRating")
                    if isinstance(rr, dict):
                        j2["rating"] = rr.get("ratingValue")
                    row = _row_from_node(j2, restaurant, query)
                    if row:
                        out.append(row)
    return out


def _parse(html: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    restaurant = _restaurant_name(soup)
    out, seen = [], set()

    nd_reviews, nd_name = _reviews_from_nextdata(html)
    restaurant = nd_name or restaurant
    for rv in nd_reviews:
        row = _row_from_node(rv, restaurant, query)
        if row:
            key = (row["reviewer"], (row["text"] or "")[:60], row["date"])
            if key not in seen:
                seen.add(key)
                out.append(row)

    if not out:
        for row in _reviews_from_jsonld(soup, restaurant, query):
            key = (row["reviewer"], (row["text"] or "")[:60], row["date"])
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    base = _menu_url(query)
    rows, page = [], 1
    while page <= MAX_PAGES:
        html = _get_text(_page_url(base, page))
        if html is None:                          # blocked / no proxy passed — finish quietly
            break
        page_rows = _parse(html, query)
        if not page_rows:
            break
        rows += page_rows
        if limit and len(rows) >= limit:
            break
        page += 1
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in TB_REVIEW_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each restaurant's thuisbezorgd reviews and store the rows."""
    from .db import jobs, thuisbezorgd_reviews
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # proxies flaky — retry once
                rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await thuisbezorgd_reviews.insert_many(rows)
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
