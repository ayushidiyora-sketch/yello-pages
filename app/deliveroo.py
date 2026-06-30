"""Deliveroo Scraper — restaurant data from deliveroo.co.uk menu and restaurant-list pages.

A query is a single Deliveroo restaurant/menu URL (…/menu/<city>/<area>/<id>-<slug>) or a
restaurant-list / search URL (…/restaurants/<city>/<area>?query=…). Each page is fetched through the
proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the real IP).
One row per restaurant — a menu URL yields one restaurant, a list URL yields all listed restaurants.
`limit` caps restaurants per query.

Deliveroo is protected by Cloudflare (JS challenge) — the same aggressive anti-bot tier as the
Deliveroo Reviews Scraper / Crunchbase / ZoomInfo. The datacenter free pool (and even a real IP) gets
a 403, so live scraping needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The parser below handles
both Deliveroo data shapes — JSON-LD `Restaurant`/`FoodEstablishment` and the embedded Apollo/Next
app-state JSON — so it returns rows as soon as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

DL_COLUMNS = [
    "query", "name", "cuisines", "rating", "reviews", "price_range",
    "address", "city", "postcode", "country", "phone", "url", "image",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _txt(v):
    if isinstance(v, dict):
        return _txt(v.get("value") or v.get("name") or v.get("text"))
    if isinstance(v, list):
        return ", ".join(_txt(x) for x in v if _txt(x))
    return str(v) if v not in (None, "") else ""


# ---------------- JSON-LD shape ----------------

def _row_from_ld(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict) or not d.get("name"):
        return None
    addr = d.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        addr = {}
    agg = d.get("aggregateRating") or {}
    if not isinstance(agg, dict):
        agg = {}
    row = {c: "" for c in DL_COLUMNS}
    row.update({
        "query": query,
        "name": _u(d.get("name")),
        "cuisines": _u(_txt(d.get("servesCuisine"))),
        "rating": str(agg.get("ratingValue") or ""),
        "reviews": str(agg.get("reviewCount") or agg.get("ratingCount") or ""),
        "price_range": _u(d.get("priceRange")),
        "address": _u(addr.get("streetAddress")),
        "city": _u(addr.get("addressLocality")),
        "postcode": _u(addr.get("postalCode")),
        "country": _u(addr.get("addressCountry")),
        "phone": _u(d.get("telephone")),
        "url": _u(d.get("url") if isinstance(d.get("url"), str) else query),
        "image": _first(d.get("image") if not isinstance(d.get("image"), dict) else d.get("image", {}).get("url")),
    })
    return row


# ---------------- embedded app-state shape ----------------

def _looks_like_restaurant(d: dict) -> bool:
    keys = set(d.keys())
    has_name = bool(keys & {"name", "restaurantName", "displayName"})
    has_attr = bool(keys & {"cuisines", "rating", "ratingValue", "menuId", "uname",
                            "neighborhood", "priceCategory", "deliveryFee"})
    return has_name and has_attr


def _row_from_obj(d: dict, query: str) -> dict | None:
    name = _txt(d.get("name") or d.get("restaurantName") or d.get("displayName"))
    if not name:
        return None
    addr = d.get("address") or d.get("location") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        addr = {}
    rating = d.get("rating") or d.get("ratingValue") or {}
    rv = rc = ""
    if isinstance(rating, dict):
        rv = rating.get("value") or rating.get("ratingValue") or ""
        rc = rating.get("count") or rating.get("reviewCount") or ""
    else:
        rv = rating
    uname = d.get("uname") or d.get("slug") or ""
    url = ("https://deliveroo.co.uk/menu/" + uname) if uname else query
    row = {c: "" for c in DL_COLUMNS}
    row.update({
        "query": query,
        "name": _u(name),
        "cuisines": _u(_txt(d.get("cuisines") or d.get("categories") or d.get("tags"))),
        "rating": str(rv or ""),
        "reviews": str(rc or d.get("numRatings") or ""),
        "price_range": _u(_txt(d.get("priceCategory") or d.get("priceRange"))),
        "address": _u(_txt(addr.get("street") or addr.get("streetAddress") or addr.get("address1"))),
        "city": _u(_txt(addr.get("city") or addr.get("addressLocality") or d.get("neighborhood"))),
        "postcode": _u(_txt(addr.get("postcode") or addr.get("postalCode"))),
        "country": _u(_txt(addr.get("country") or addr.get("addressCountry"))),
        "phone": _u(_txt(d.get("phone") or d.get("phoneNumber"))),
        "url": url,
        "image": _u(_txt(d.get("image") or d.get("imageUrl") or d.get("heroImage"))),
    })
    return row


def _balanced_json(text: str, start: int) -> str:
    """From the first '{' at/after `start`, return the balanced-brace JSON substring."""
    i = text.find("{", start)
    if i < 0:
        return ""
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def _parse_state(html_text: str, query: str) -> list[dict]:
    blobs = []
    for m in re.finditer(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S):
        blobs.append(m.group(1))
    for m in re.finditer(r'window\.__(?:APOLLO_STATE|INITIAL_STATE|PRELOADED_STATE|NEXT_DATA)__\s*=\s*', html_text):
        blob = _balanced_json(html_text, m.end())
        if blob:
            blobs.append(blob)
    out, seen = [], set()
    for raw in blobs:
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if _looks_like_restaurant(cur):
                    row = _row_from_obj(cur, query)
                    if row:
                        key = (row["name"], row["city"], row["url"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return out


def _parse(html_text: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    out, seen = [], set()
    types = {"Restaurant", "FoodEstablishment", "LocalBusiness", "FoodService"}
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                t = cur.get("@type")
                tset = set(t) if isinstance(t, list) else {t}
                if tset & types and cur.get("name"):
                    row = _row_from_ld(cur, query)
                    if row:
                        key = (row["name"], row["city"], row["url"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    for row in _parse_state(html_text, query):
        key = (row["name"], row["city"], row["url"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = (query or "").strip()
    if not url.lower().startswith("http"):
        return []
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return []
    if r is None or r.status_code != 200:
        return []
    rows = _parse(r.text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in DL_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Deliveroo URL and store one row per restaurant."""
    from .db import jobs, deliveroo_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await deliveroo_results.insert_many(rows)
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
