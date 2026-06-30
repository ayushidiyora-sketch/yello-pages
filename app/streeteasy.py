"""StreetEasy Scraper — real estate listings from streeteasy.com search pages.

A query is a StreetEasy search URL (…/for-sale/<area>/… or …/for-rent/<area>/…). Each page is fetched
through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the
real IP). One row per listing. `limit` caps listings per query.

StreetEasy embeds every listing on a search page as JSON-LD `Apartment`/`SingleFamilyResidence`/
`House` objects (price + building type live in `additionalProperty` PropertyValue pairs). The page
renders fine with a browser fingerprint, but StreetEasy 403s datacenter IPs by reputation — so the
free pool is blocked and live scraping needs a RESIDENTIAL proxy in PROXY_URL / PROXY_LIST. The parser
below reads the JSON-LD, so rows come back as soon as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

SE_COLUMNS = [
    "query", "name", "price", "building_type", "bedrooms", "bathrooms",
    "size_sqft", "address", "neighborhood", "region", "postcode",
    "latitude", "longitude", "url", "image",
]

_LISTING_TYPES = {
    "Apartment", "SingleFamilyResidence", "House", "Residence", "Accommodation",
}


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _props(d: dict) -> dict:
    """Flatten additionalProperty PropertyValue pairs into {name: value}."""
    out = {}
    ap = d.get("additionalProperty") or []
    if isinstance(ap, dict):
        ap = [ap]
    for p in ap:
        if isinstance(p, dict) and p.get("name"):
            out[p["name"]] = p.get("value", "")
    return out


def _img(v):
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        return v.get("contentUrl") or v.get("url") or ""
    return v or ""


def _row(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict) or not d.get("name"):
        return None
    addr = d.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        addr = {}
    geo = d.get("geo") or {}
    if not isinstance(geo, dict):
        geo = {}
    fs = d.get("floorSize") or {}
    size = fs.get("value") if isinstance(fs, dict) else fs
    p = _props(d)
    row = {c: "" for c in SE_COLUMNS}
    row.update({
        "query": query,
        "name": _u(d.get("name")),
        "price": _u(p.get("Price")),
        "building_type": _u(p.get("Building Type")),
        "bedrooms": str(d.get("numberOfBedrooms") if d.get("numberOfBedrooms") is not None else ""),
        "bathrooms": str(d.get("numberOfBathroomsTotal") if d.get("numberOfBathroomsTotal") is not None else ""),
        "size_sqft": str(size or ""),
        "address": _u(addr.get("streetAddress")),
        "neighborhood": _u(addr.get("addressLocality")),
        "region": _u(addr.get("addressRegion")),
        "postcode": _u(addr.get("postalCode")),
        "latitude": str(geo.get("latitude") or ""),
        "longitude": str(geo.get("longitude") or ""),
        "url": _u(d.get("url") or d.get("@id")),
        "image": _img(d.get("image")),
    })
    return row


def _parse(html_text: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    out, seen = [], set()
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
                if tset & _LISTING_TYPES and cur.get("name"):
                    row = _row(cur, query)
                    if row:
                        key = row["url"] or (row["name"], row["price"])
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
    return {c: doc.get(c, "") for c in SE_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each StreetEasy search URL and store one row per listing."""
    from .db import jobs, streeteasy_results
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
                await streeteasy_results.insert_many(rows)
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
