"""Waxie Products Scraper — product listings from shop.waxie.com (janitorial supplies).

Waxie item pages don't use JSON-LD, so a site-specific HTML fallback pulls the product name (page
title) and price. Input is a shop.waxie.com itemDetail URL. Proxy-only. One row per product.
"""
import asyncio
import re

from . import products_common as pc

WAXIE_COLUMNS = pc.PRODUCT_COLUMNS


def _fallback(soup, query, url):
    t = soup.find("title")
    name = t.get_text(" ", strip=True).split("|")[0].strip() if t else ""
    if not name:
        return []
    m = re.search(r"\$\s?([\d,]+\.\d{2})", soup.get_text(" ", strip=True))
    row = {c: "" for c in WAXIE_COLUMNS}
    row.update(query=query, name=name, price=("$" + m.group(1)) if m else "", url=url, status="ok")
    return [row]


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    return pc.scrape(query, limit, _fallback)


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list, limit: int | None = None) -> None:
    from .db import waxie_products
    await pc.run(job_id, queries, limit, waxie_products, _fallback)
