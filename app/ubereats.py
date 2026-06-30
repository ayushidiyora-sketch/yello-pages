"""Uber Eats Scraper — restaurant/store data from ubereats.com.

A query is a single Uber Eats store URL (…/store/<slug>/<id>) or a listing URL — find-near-me,
category, or city page (…/find-near-me/…, …/category/…, …/city/…). Each page is fetched through the
proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the real IP).
One row per store: a store URL yields that store, a listing URL yields every store linked on the page
(each store page is fetched for its full profile). `limit` caps stores per query.

Uber Eats serves store pages with a clean JSON-LD `Restaurant` block and is reachable on the free
pool with a browser fingerprint, so this works without a residential proxy.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

UE_COLUMNS = [
    "query", "name", "cuisines", "rating", "reviews", "price_range",
    "address", "city", "region", "postcode", "country", "phone", "url", "image",
]

_BASE = "https://www.ubereats.com"
_STORE_RE = re.compile(r"/store/[A-Za-z0-9%._\-]+/[A-Za-z0-9_\-]{10,}")


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _txt(v):
    if isinstance(v, dict):
        return _txt(v.get("value") or v.get("name"))
    if isinstance(v, list):
        return ", ".join(_txt(x) for x in v if _txt(x))
    return str(v) if v not in (None, "") else ""


def _restaurant_ld(html_text: str) -> dict | None:
    soup = BeautifulSoup(html_text, "lxml")
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
                if tset & {"Restaurant", "FoodEstablishment", "LocalBusiness"} and cur.get("name"):
                    return cur
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return None


def _row_from_ld(d: dict, query: str, url: str) -> dict | None:
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
    row = {c: "" for c in UE_COLUMNS}
    row.update({
        "query": query,
        "name": _u(d.get("name")),
        "cuisines": _u(_txt(d.get("servesCuisine"))),
        "rating": str(agg.get("ratingValue") or ""),
        "reviews": str(agg.get("reviewCount") or agg.get("ratingCount") or ""),
        "price_range": _u(d.get("priceRange")),
        "address": _u(addr.get("streetAddress")),
        "city": _u(addr.get("addressLocality")),
        "region": _u(addr.get("addressRegion")),
        "postcode": _u(addr.get("postalCode")),
        "country": _u(addr.get("addressCountry")),
        "phone": _u(d.get("telephone")),
        "url": url,
        "image": _first(d.get("image")),
    })
    return row


def _store_links(html_text: str) -> list[str]:
    seen, out = set(), []
    for m in _STORE_RE.findall(html_text):
        if m not in seen:
            seen.add(m)
            out.append(_BASE + m)
    return out


def _fetch(url: str):
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return None
    if r is None or r.status_code != 200:
        return None
    return r.text


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = (query or "").strip()
    if not url.lower().startswith("http"):
        return []
    html_text = _fetch(url)
    if not html_text:
        return []
    # single store page
    if "/store/" in url:
        d = _restaurant_ld(html_text)
        row = _row_from_ld(d, query, url) if d else None
        return [row] if row else []
    # listing page (find-near-me / category / city): follow each store link
    links = _store_links(html_text)
    if limit:
        links = links[:limit]
    out, seen = [], set()
    for link in links:
        page = _fetch(link)
        if not page:
            continue
        d = _restaurant_ld(page)
        row = _row_from_ld(d, query, link) if d else None
        if row:
            key = (row["name"], row["url"])
            if key not in seen:
                seen.add(key)
                out.append(row)
        if limit and len(out) >= limit:
            break
    return out


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in UE_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Uber Eats URL and store one row per restaurant."""
    from .db import jobs, ubereats_results
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
                await ubereats_results.insert_many(rows)
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
