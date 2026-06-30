"""Asos Products Scraper — product listings from asos.com.

Reads ASOS's own internal product-search API (`/api/product/search/v2`) — the JSON endpoint the site's
frontend calls — THROUGH A PROXY (real IP never used). Input is an ASOS search/category URL or a plain
keyword: a `cid=` category URL searches by categoryId, otherwise by keyword. Paginates via offset. One
row per product.
"""
import asyncio
import re
from datetime import datetime
from urllib.parse import quote, urlparse, parse_qs

from . import yp_us
from .config import settings

AS_COLUMNS = ["query", "product_id", "name", "brand", "price", "colour", "product_type", "url", "image"]

_API = ("https://www.asos.com/api/product/search/v2/{path}?{key}={val}&store=US&offset={offset}"
        "&limit={limit}&lang=en-US&currency=USD&sizeSchema=US&country=US")
_PAGE = 200  # max products per API call


def _params(query: str) -> tuple:
    """(path, key, value) -> search by categoryId for a cid= URL, else by keyword q."""
    q = (query or "").strip()
    if "cid=" in q or "/cat/" in q:
        qs = parse_qs(urlparse(q).query)
        cid = (qs.get("cid") or [""])[0]
        if not cid:
            m = re.search(r"cid=(\d+)", q)
            cid = m.group(1) if m else ""
        if cid:
            return ("", "categoryId", cid)
    if "?q=" in q or "&q=" in q:
        qs = parse_qs(urlparse(q).query)
        term = (qs.get("q") or [""])[0]
        return ("", "q", quote(term))
    return ("", "q", quote(q))  # bare keyword


def _row(p: dict, query: str) -> dict:
    url = p.get("url") or ""
    img = p.get("imageUrl") or ""
    ptype = p.get("productType")
    if isinstance(ptype, dict):
        ptype = ptype.get("name")
    return {
        "query": query,
        "product_id": p.get("id") or "",
        "name": p.get("name") or "",
        "brand": p.get("brandName") or "",
        "price": ((p.get("price") or {}).get("current") or {}).get("text") or "",
        "colour": p.get("colour") or "",
        "product_type": ptype or "",
        "url": ("https://www.asos.com/" + url.lstrip("/")) if url else "",
        "image": ("https://" + img.lstrip("/")) if img and not img.startswith("http") else img,
    }


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    path, key, val = _params(q)
    cap = limit if (limit and limit > 0) else 200
    out: list[dict] = []
    for offset in range(0, cap, _PAGE):
        url = _API.format(path=path, key=key, val=val, offset=offset, limit=min(_PAGE, cap - offset))
        # ASOS serves an error/empty on some pool IPs — retry across rotating IPs before giving up
        # (retry harder on the first page so one bad IP doesn't zero out the whole query).
        products = []
        for _ in range(5 if offset == 0 else 2):
            try:
                r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
            except Exception:
                continue
            if r is not None and r.status_code == 200:
                try:
                    products = r.json().get("products") or []
                except ValueError:
                    products = []
                if products:
                    break
        if not products:
            break
        out += [_row(p, query) for p in products]
        if len(out) >= cap:
            break
    return out[:cap]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, asos_products
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await asos_products.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = "0 products — ASOS may have been blocked on the free proxy; retry."
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
