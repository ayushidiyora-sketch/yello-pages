"""Newegg Products Scraper — product listings from newegg.com.

Newegg product pages carry a schema.org JSON-LD `Product`, but newegg.com is anti-bot protected and
returns 403 to datacenter IPs -> the row carries a clear "blocked (needs residential proxy)" status.
On a residential proxy the JSON-LD is parsed. Proxy-only. One row per product.
"""
import asyncio

from . import products_common as pc

NEWEGG_COLUMNS = pc.PRODUCT_COLUMNS


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    return pc.scrape(query, limit)


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list, limit: int | None = None) -> None:
    from .db import newegg_products
    await pc.run(job_id, queries, limit, newegg_products)
