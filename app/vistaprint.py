"""Vistaprint Products Scraper — product listings from vistaprint.com.

Product pages embed a schema.org JSON-LD `Product`; category/search pages are JS/API-driven, so those
return a clear "no product data" status. Proxy-only (real IP never used). One row per product.
"""
import asyncio

from . import products_common as pc

VISTA_COLUMNS = pc.PRODUCT_COLUMNS


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    return pc.scrape(query, limit)


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list, limit: int | None = None) -> None:
    from .db import vistaprint_products
    await pc.run(job_id, queries, limit, vistaprint_products)
