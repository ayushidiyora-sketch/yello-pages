"""Gemplers Products Scraper — product listings from gemplers.com.

Uses the shared products_common engine: fetch the product/category/URL query THROUGH A PROXY (real IP
never used), extract schema.org JSON-LD `Product` nodes, else fall back to the page name + price/image.
Anti-bot pages (403) return a clear "blocked (residential-only)" status. One row per product.
"""
import asyncio

from . import products_common as pc

GEMPLERS_COLUMNS = pc.PRODUCT_COLUMNS


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    return pc.scrape(query, limit)


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list, limit: int | None = None) -> None:
    from .db import gemplers_products
    await pc.run(job_id, queries, limit, gemplers_products)
