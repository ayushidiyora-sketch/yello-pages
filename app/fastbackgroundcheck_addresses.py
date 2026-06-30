"""Fastbackgroundcheck Addresses Scraper — insights about an address and its residents.

Same shape as the Whitepages Addresses scraper: geocoded location (reliable, OpenStreetMap) plus a
best-effort residents lookup against fastbackgroundcheck.com THROUGH A PROXY (real IP never used).
fastbackgroundcheck.com is anti-bot protected (403 on datacenter IPs), so residents usually come back
empty on the free pool with a clear status — needs a residential proxy. One row per address.
"""
import asyncio
import re
from datetime import datetime

from . import geocoding, yp_us
from .config import settings

AR_COLUMNS = ["address", "lat", "lon", "matched_address", "residents", "status"]
_NAME_RE = re.compile(r'/people/[^"]*"[^>]*>([A-Z][a-zA-Z.\'-]+(?:\s+[A-Z][a-zA-Z.\'-]+){1,3})<', re.S)


def _slug(addr: str) -> str:
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if not parts:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", addr.lower()).strip("-")
    return f"https://www.fastbackgroundcheck.com/address/{slug}"


def _residents(addr: str) -> tuple:
    """(residents_str, status). Best-effort fastbackgroundcheck; empty on block (403)."""
    url = _slug(addr)
    if not url:
        return "", "no location parsed"
    try:
        r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None:
        return "", "no response (proxy)"
    if r.status_code in (403, 429):
        return "", "blocked (needs residential proxy)"
    if r.status_code == 404:
        return "", "no public residents"
    if r.status_code != 200:
        return "", f"http {r.status_code}"
    names = list(dict.fromkeys(_NAME_RE.findall(r.text)))[:10]
    return ("; ".join(names), "ok") if names else ("", "no public residents")


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    addr = (query or "").strip()
    if not addr:
        return []
    g = (geocoding.search_sync(addr) or [{}])[0]
    residents, status = _residents(addr)
    return [{
        "address": addr,
        "lat": g.get("lat", ""), "lon": g.get("lon", ""),
        "matched_address": g.get("matched_address", ""),
        "residents": residents, "status": status,
    }]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, fastbackgroundcheck_addresses
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await fastbackgroundcheck_addresses.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
