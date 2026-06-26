"""Walmart Products Scraper — walmart.com product pages.

Same design as the other live scrapers: every request goes through a proxy IP (a paid PROXY_URL
if set, otherwise the rotating free pool — NEVER the real IP). Walmart is bot-protected
(PerimeterX/Akamai), so on the free pool it is often blocked and returns 0 until a paid PROXY_URL
is set (same behaviour as the BBB / Glassdoor scrapers).

A query is a Walmart product URL, e.g. https://www.walmart.com/ip/Homfa-Sofa-Bed/625493716
(a /ip/ URL yields one product). Each query yields up to `limit` rows.
"""
import asyncio
import json
import re
import threading
from datetime import datetime

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

BASE = "https://www.walmart.com"
_WM_TIMEOUT = 15
_GOOD_PROXY = None          # last proxy that passed walmart.com — reused before re-rotating
_PIN_LOCK = threading.Lock()
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

_ITEM_IN_URL = re.compile(r"/ip/(?:[^/]+/)?(\d{5,})")

# one row per product
WALMART_PRODUCT_COLUMNS = [
    "query", "item_id", "name", "brand", "price", "currency", "rating", "review_count",
    "availability", "seller", "category", "description", "image", "url", "position",
]


def _blank_row():
    return {c: "" for c in WALMART_PRODUCT_COLUMNS}


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    """A real walmart.com page (not a Cloudflare / PerimeterX bot block). The block/challenge page is a
    small interstitial WITHOUT Walmart's app data; a real product/search page always ships
    `__NEXT_DATA__` or product JSON-LD. Keying on that data (instead of a generic word blocklist) avoids
    false-rejecting real pages that merely contain a word like "blocked" somewhere in their 500 KB."""
    if r is None or r.status_code != 200:
        return False
    low = (r.text or "").lower()
    return "__next_data__" in low or '"@type":"product"' in low or '"__typename"' in low


def _try(url: str, px: str):
    try:
        r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                     timeout=_WM_TIMEOUT, verify=False, allow_redirects=True)
        return r if _ok(r) else None
    except Exception:
        return None


def _proxied_get(url: str):
    """Fetch through a free proxy (NEVER the real IP): reuse the last known-good proxy, else
    rotate the pool until one passes and pin it. Raises if none pass."""
    global _GOOD_PROXY
    pinned = _GOOD_PROXY
    if pinned:
        r = _try(url, pinned)
        if r is not None:
            return r
    from . import yp_us
    yp_us.ensure_pool(_SEED, 8)
    seen, candidates = {pinned}, []
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px not in seen:
            seen.add(px)
            candidates.append(px)
    for px in candidates[:15]:
        r = _try(url, px)
        if r is not None:
            with yp_us._LOCK:
                if px in yp_us._GOOD:
                    yp_us._GOOD.remove(px)
                yp_us._GOOD.insert(0, px)
            with _PIN_LOCK:
                _GOOD_PROXY = px
            return r
    raise RuntimeError("no free proxy passed walmart.com")


def _get_text(url: str) -> str | None:
    """Fetch one URL through a proxy. Priority: WALMART_PROXY_URL (Walmart-only) → PROXY_URL →
    free pool. Returns HTML on a real page, None if blocked/failed. NEVER the real IP."""
    proxy = settings.WALMART_PROXY_URL.strip() or settings.PROXY_URL.strip()
    if proxy:
        # Walmart's PerimeterX challenge is intermittent on a single proxy IP — retry a few times
        # before giving up so a one-off "verify you're human" page isn't fatal.
        for _ in range(4):
            try:
                r = cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                             timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
            except Exception:
                continue
            if _ok(r):
                return r.text
        return None
    try:
        return _proxied_get(url).text
    except Exception:
        return None


# ---------------- parsing ----------------

def _txt(node):
    return node.get_text(" ", strip=True) if node else ""


def _row_from_jsonld(j: dict, query: str) -> dict | None:
    if not isinstance(j, dict) or not j.get("name"):
        return None
    brand = j.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    offers = j.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    seller = offers.get("seller") or {}
    if isinstance(seller, dict):
        seller = seller.get("name")
    agg = j.get("aggregateRating") or {}
    img = j.get("image")
    if isinstance(img, list):
        img = img[0] if img else ""
    row = _blank_row()
    row["query"] = query
    row["item_id"] = str(j.get("sku") or j.get("productID") or "")
    row["name"] = j.get("name") or ""
    row["brand"] = brand or ""
    row["price"] = str(offers.get("price") or "")
    row["currency"] = offers.get("priceCurrency") or ""
    row["rating"] = str(agg.get("ratingValue") or "")
    row["review_count"] = str(agg.get("reviewCount") or agg.get("ratingCount") or "")
    av = offers.get("availability") or ""
    row["availability"] = av.split("/")[-1] if isinstance(av, str) else ""
    row["seller"] = seller or ""
    row["category"] = j.get("category") or ""
    row["description"] = re.sub(r"<[^>]+>", " ", j.get("description") or "")[:500].strip()
    row["image"] = img or ""
    row["url"] = j.get("url") or query
    return row


def _parse(html: str, query: str) -> list[dict]:
    """One+ product rows. JSON-LD Product first, then a best-effort __NEXT_DATA__ / HTML read."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()

    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict) and (obj.get("@type") == "Product"
                                          or (isinstance(obj.get("@type"), list) and "Product" in obj["@type"])):
                row = _row_from_jsonld(obj, query)
                if row and row["name"] not in seen:
                    seen.add(row["name"])
                    out.append(row)

    if not out:
        row = _parse_nextdata(html, query)
        if row:
            out.append(row)

    # ensure item_id from the URL when JSON-LD didn't carry it
    for row in out:
        if not row["item_id"]:
            m = _ITEM_IN_URL.search(row.get("url") or query)
            if m:
                row["item_id"] = m.group(1)
    return out


def _parse_nextdata(html: str, query: str) -> dict | None:
    """Pull the core fields from Walmart's __NEXT_DATA__ product JSON (best-effort)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None
    prod = _find_product(data)
    if not prod:
        return None
    row = _blank_row()
    row["query"] = query
    row["item_id"] = str(prod.get("usItemId") or prod.get("id") or "")
    row["name"] = prod.get("name") or ""
    row["brand"] = prod.get("brand") or ""
    pm = prod.get("priceInfo") or prod.get("price") or {}
    cur = (pm.get("currentPrice") or {}) if isinstance(pm, dict) else {}
    row["price"] = str(cur.get("price") or pm.get("price") or "") if isinstance(cur, dict) else ""
    row["currency"] = (cur.get("currencyUnit") if isinstance(cur, dict) else "") or "USD"
    rv = prod.get("averageRating") or (prod.get("rating") or {})
    row["rating"] = str(rv.get("averageRating") if isinstance(rv, dict) else rv or "")
    row["review_count"] = str((prod.get("numberOfReviews")
                               or (rv.get("numberOfReviews") if isinstance(rv, dict) else "")) or "")
    row["availability"] = prod.get("availabilityStatus") or ""
    row["seller"] = (prod.get("sellerName") or "")
    row["image"] = prod.get("imageUrl") or ((prod.get("imageInfo") or {}).get("thumbnailUrl") or "")
    row["url"] = query
    return row if row["name"] else None


def _find_product(obj):
    """Walk the __NEXT_DATA__ tree for the product node. Require `usItemId` (Walmart's canonical product
    id) so we don't match variant/option nodes like {"name":"Size","id":...}; fall back to a node that
    pairs a name with a priceInfo block."""
    def walk(pred):
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if pred(cur):
                    return cur
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
        return None
    return (walk(lambda c: c.get("name") and c.get("usItemId"))
            or walk(lambda c: c.get("name") and isinstance(c.get("priceInfo"), dict)))


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    html = _get_text(query)
    if html is None:
        return []
    rows = _parse(html, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in WALMART_PRODUCT_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    """Background task: scrape each Walmart product URL and store the rows."""
    from .db import jobs, walmart_products
    total = 0
    try:
        mode = "walmart-free-pool" if not settings.PROXY_URL.strip() else "walmart-paid-proxy"
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": mode}})

        for q in queries:
            rows = await search(q, limit)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await walmart_products.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
