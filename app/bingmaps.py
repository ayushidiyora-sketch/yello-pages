"""Bing Maps Scraper — local business listings from Bing Maps.

A query is a free-text local search (category + city/zip/country), e.g. "bars, NY, USA" or
"restaurants near New York, NY 10001, United States". Results come from Bing Maps' local overlay
feed (bing.com/maps/overlaybfpr), fetched through the proxy pool (paid PROXY_URL / PROXY_LIST if set,
else the rotating free pool — NEVER the real IP). One row per business. `limit` caps businesses per
query.

Bing serves this feed without aggressive bot protection, so it works on the free pool — no
paid/residential proxy required.
"""
import asyncio
import html
import json
import re
from urllib.parse import quote
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

BM_COLUMNS = [
    "query", "name", "category", "style", "phone", "website", "address",
    "rating", "reviews", "latitude", "longitude", "image", "ypid",
]

_OVERLAY = "https://www.bing.com/maps/overlaybfpr?q={q}&count={n}&first={first}&mapVer=7.0"
_PAGE = 20


def _u(v):
    return html.unescape(str(v)) if v else ""


def _rating_from_infobox(box: str):
    """Pull a star rating + review count out of the infoboxHtml if present."""
    rating = reviews = ""
    if not box:
        return rating, reviews
    m = re.search(r'(\d(?:\.\d)?)\s*(?:star|/\s*5|out of 5)', box, re.I)
    if m:
        rating = m.group(1)
    m = re.search(r'([\d,]+)\s*(?:review|rating)', box, re.I)
    if m:
        reviews = m.group(1).replace(",", "")
    return rating, reviews


def _row(d: dict, query: str) -> dict | None:
    ent = d.get("entity") if isinstance(d, dict) else None
    if not isinstance(ent, dict) or not ent.get("title"):
        return None
    geo = d.get("geometry") or {}
    if not isinstance(geo, dict):
        geo = {}
    rating, reviews = _rating_from_infobox(ent.get("infoboxHtml") or "")
    ypid = ent.get("id") or ""
    if isinstance(ypid, str) and ypid.startswith("ypid:"):
        ypid = ypid[5:]
    row = {c: "" for c in BM_COLUMNS}
    row.update({
        "query": query,
        "name": _u(ent.get("title")),
        "category": _u(ent.get("primaryCategoryName")),
        "style": _u(ent.get("primaryStyleCategory")),
        "phone": _u(ent.get("phone")),
        "website": _u(ent.get("website")),
        "address": _u(ent.get("address")),
        "rating": rating,
        "reviews": reviews,
        "latitude": str(geo.get("y") or ""),
        "longitude": str(geo.get("x") or ""),
        "image": _u(ent.get("imageUrl")),
        "ypid": _u(ypid),
    })
    return row


def _parse(html_text: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    out, seen = [], set()
    for el in soup.find_all(attrs={"data-entity": True}):
        try:
            d = json.loads(el.get("data-entity"))
        except Exception:
            continue
        row = _row(d, query)
        if row:
            key = row["ypid"] or (row["name"], row["address"])
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    target = limit if (limit and limit > 0) else _PAGE
    out, seen = [], set()
    first = 0
    while True:
        url = _OVERLAY.format(q=quote(q), n=_PAGE, first=first)
        try:
            r = yp_us.pooled_get(url, {}, timeout=25)
        except Exception:
            break
        if r is None or r.status_code != 200:
            break
        rows = _parse(r.text, query)
        if not rows:
            break
        new = 0
        for row in rows:
            key = row["ypid"] or (row["name"], row["address"])
            if key not in seen:
                seen.add(key)
                out.append(row)
                new += 1
        if limit and len(out) >= limit:
            return out[:limit]
        if new == 0 or len(rows) < _PAGE:
            break
        first += _PAGE
        if first > 250:                # Bing local overlay caps out; safety stop
            break
    return out[:limit] if limit else out


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in BM_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape Bing Maps for each query and store one row per business."""
    from .db import jobs, bingmaps_results
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
                await bingmaps_results.insert_many(rows)
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
