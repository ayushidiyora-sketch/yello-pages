"""Craigslist Scraper — listings from craigslist.org search and listing pages.

A query is a Craigslist search URL (…/search/<cat>?query=…) or a single listing URL (…/d/<slug>.html).
Each page is fetched through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating
free pool — NEVER the real IP). Listings are embedded as JSON-LD `Product` objects (name + offers
price + image); we parse those. One row per listing. `limit` caps listings per query.

Craigslist is reachable on the free pool (not hard bot-walled), so this works without a paid proxy.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

CL_COLUMNS = [
    "query", "title", "price", "currency", "condition", "location", "url", "image", "description",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _row(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict) or not d.get("name"):
        return None
    offers = d.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    cond = offers.get("itemCondition") or d.get("itemCondition") or ""
    if isinstance(cond, str):
        cond = cond.rsplit("/", 1)[-1].replace("Condition", "")
    area = offers.get("availableAtOrFrom") or offers.get("areaServed") or {}
    loc = ""
    if isinstance(area, dict):
        addr = area.get("address") or area
        if isinstance(addr, dict):
            loc = addr.get("addressLocality") or addr.get("name") or ""
        elif isinstance(addr, str):
            loc = addr
    elif isinstance(area, str):
        loc = area
    row = {c: "" for c in CL_COLUMNS}
    row.update({
        "query": query,
        "title": _u(d.get("name")),
        "price": str(offers.get("price") or ""),
        "currency": offers.get("priceCurrency") or "",
        "condition": cond,
        "location": _u(loc),
        "url": offers.get("url") or d.get("url") or "",
        "image": _first(d.get("image")),
        "description": _u(re.sub(r"<[^>]+>", " ", str(d.get("description") or "")))[:500].strip(),
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
                if (t == "Product" or (isinstance(t, list) and "Product" in t)) and cur.get("name"):
                    row = _row(cur, query)
                    if row:
                        key = (row["title"], row["price"], row["url"] or row["image"])
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
    return {c: doc.get(c, "") for c in CL_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Craigslist search/listing URL and store one row per listing."""
    from .db import jobs, craigslist_results
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
                await craigslist_results.insert_many(rows)
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
