"""OfferUp Scraper — marketplace listings from offerup.com.

A query is an OfferUp item-detail URL (offerup.com/item/detail/<id>), an explore URL
(offerup.com/explore/k/...), or a search URL (offerup.com/search?q=...). Each page is fetched
through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the
real IP). OfferUp is a Next.js app: listings live in the `__NEXT_DATA__` JSON; we walk it for listing
nodes (title + price + listingId). One row per listing. `limit` caps listings per query.

OfferUp is reachable on the free pool (not hard bot-walled), so this works without a paid proxy.
"""
import asyncio
import json
import re
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

BASE = "https://offerup.com"

OFFERUP_COLUMNS = [
    "query", "title", "price", "condition", "location", "firm_price", "miles",
    "flags", "image", "listing_url",
]


def _is_listing(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    t = str(d.get("__typename", ""))
    has_id = bool(d.get("listingId") or (d.get("id") and "Listing" in t))
    return has_id and bool(d.get("title")) and ("price" in d)


def _img_url(img) -> str:
    if isinstance(img, dict):
        return img.get("url") or img.get("uri") or ""
    if isinstance(img, list) and img:
        return _img_url(img[0])
    return img if isinstance(img, str) else ""


def _row(d: dict, query: str) -> dict | None:
    lid = d.get("listingId") or d.get("id") or ""
    price = d.get("price")
    row = {c: "" for c in OFFERUP_COLUMNS}
    row.update({
        "query": query,
        "title": d.get("title") or "",
        "price": str(price) if price not in (None, "") else "",
        "condition": d.get("conditionText") or d.get("condition") or "",
        "location": d.get("locationName") or d.get("location") or "",
        "firm_price": ("Yes" if d.get("isFirmPrice") else "No") if d.get("isFirmPrice") is not None else "",
        "miles": str(d.get("vehicleMiles") or "") if d.get("vehicleMiles") else "",
        "flags": ", ".join(d.get("flags") or []) if isinstance(d.get("flags"), list) else "",
        "image": _img_url(d.get("image") or d.get("photos")),
        "listing_url": f"{BASE}/item/detail/{lid}" if lid else "",
    })
    return row if row["title"] else None


def _parse(html: str, query: str) -> list[dict]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out, seen = [], set()
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if _is_listing(cur):
                row = _row(cur, query)
                if row:
                    key = cur.get("listingId") or (row["title"], row["price"], row["location"])
                    if key not in seen:
                        seen.add(key)
                        out.append(row)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
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
    return {c: doc.get(c, "") for c in OFFERUP_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each OfferUp search/item URL and store one row per listing."""
    from .db import jobs, offerup_results
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
                await offerup_results.insert_many(rows)
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
