"""BiggestBook Products Scraper — product listings from biggestbook.com (office supplies, Essendant).

biggestbook.com is a single-page app: the product data loads from Essendant's internal catalog API
(not in the page HTML), so a plain page fetch returns only the JS shell and the row carries a clear
"no product data (JS/API-driven)" status. When a page does embed a JSON-LD `Product` it is parsed.
Proxy-only. One row per product.
"""
import asyncio

from . import products_common as pc

BIGBOOK_COLUMNS = pc.PRODUCT_COLUMNS


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    return pc.scrape(query, limit)


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list, limit: int | None = None) -> None:
    from .db import biggestbook_products
    await pc.run(job_id, queries, limit, biggestbook_products)
