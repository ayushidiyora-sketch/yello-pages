"""Shared product-listing scraper for retailer sites (Vistaprint, Waxie, Otto, Newegg, BiggestBook).

Most retailers embed products as schema.org JSON-LD `Product` nodes in the page HTML; this pulls them
into flat rows. A per-site HTML fallback can be supplied for sites that don't use JSON-LD (e.g. Waxie).
All fetches go THROUGH A PROXY (real IP never used). Anti-bot sites (Otto/Newegg) return 403 on
datacenter IPs -> the row carries a clear "blocked (needs residential proxy)" status.
"""
import asyncio
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .config import settings

PRODUCT_COLUMNS = ["query", "name", "brand", "price", "currency", "sku", "availability",
                   "rating", "url", "image", "status"]

_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _str(v):
    if isinstance(v, dict):
        return v.get("name") or v.get("@id") or ""
    if isinstance(v, list):
        return _str(v[0]) if v else ""
    return v if isinstance(v, str) else (str(v) if v is not None else "")


def _offer(offers):
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return "", "", ""
    price = offers.get("price") or offers.get("lowPrice") or ""
    avail = (offers.get("availability") or "").split("/")[-1]  # schema.org/InStock -> InStock
    return str(price), offers.get("priceCurrency") or "", avail


def _product_row(node: dict, query: str) -> dict:
    price, currency, avail = _offer(node.get("offers"))
    rating = ""
    ar = node.get("aggregateRating")
    if isinstance(ar, dict):
        rating = str(ar.get("ratingValue") or "")
    img = node.get("image")
    if isinstance(img, list):
        img = img[0] if img else ""
    if isinstance(img, dict):
        img = img.get("url") or ""
    return {
        "query": query, "name": _str(node.get("name")), "brand": _str(node.get("brand")),
        "price": price, "currency": currency, "sku": _str(node.get("sku") or node.get("mpn")),
        "availability": avail, "rating": rating, "url": _str(node.get("url")),
        "image": img if isinstance(img, str) else "", "status": "ok",
    }


def _walk(node, out, query):
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x == "Product" for x in types) and node.get("name"):
            out.append(_product_row(node, query))
        for v in node.values():
            _walk(v, out, query)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, query)


def products_from_html(html: str, query: str) -> list:
    """All schema.org Product nodes in the page JSON-LD, as flat rows (deduped by name)."""
    out = []
    for m in _LD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except (ValueError, json.JSONDecodeError):
            continue
        _walk(data, out, query)
    seen, uniq = set(), []
    for r in out:
        if r["name"] and r["name"] not in seen:
            seen.add(r["name"])
            uniq.append(r)
    return uniq


def scrape(query: str, limit: int | None = None, html_fallback=None) -> list:
    """Fetch one product/category/search URL and return product rows. 403/429 -> a single row with a
    'blocked' status; JSON-LD Products first, then an optional site-specific html_fallback(soup, query)."""
    url = (query or "").strip()
    if not url:
        return []
    if not url.lower().startswith("http"):
        url = "https://" + url
    blank = {c: "" for c in PRODUCT_COLUMNS}
    blank.update(query=query, url=url)
    try:
        r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None:
        return [{**blank, "status": "no response (proxy)"}]
    if r.status_code in (403, 429):
        return [{**blank, "status": "blocked (needs residential proxy)"}]
    if r.status_code != 200 or not r.text:
        return [{**blank, "status": f"http {r.status_code}"}]
    rows = products_from_html(r.text, query)
    if not rows and html_fallback is not None:
        rows = html_fallback(BeautifulSoup(r.text, "lxml"), query, url)
    if not rows:
        return [{**blank, "status": "no product data found"}]
    return rows[:limit] if limit else rows


async def run(job_id: str, queries: list, limit, coll, html_fallback=None) -> None:
    """Shared per-site run_job: scrape each query URL, store rows, mark the job done. `coll` is the
    site's Mongo collection; `html_fallback(soup, query, url)` is an optional site-specific parser."""
    from .db import jobs
    total = 0
    try:
        for q in queries:
            rows = await asyncio.to_thread(scrape, q, limit, html_fallback)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await coll.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
