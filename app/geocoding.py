"""Geocoding — translate a human-readable address into coordinates (lat/lon) + place metadata.

Uses OpenStreetMap's free Nominatim API (no key) THROUGH A PROXY (real IP never used). Nominatim asks
for a descriptive User-Agent and ~1 req/sec; this runs one address at a time. One row per address.
"""
import asyncio
from datetime import datetime
from urllib.parse import quote

from . import yp_us
from .config import settings

GC_COLUMNS = ["address", "lat", "lon", "matched_address", "type", "class", "importance",
              "osm_type", "osm_id"]

_API = "https://nominatim.openstreetmap.org/search?format=json&addressdetails=1&limit=1&q="


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input address -> one row with coordinates + matched place (empty lat/lon if not found)."""
    addr = (query or "").strip()
    if not addr:
        return []
    row = {c: "" for c in GC_COLUMNS}
    row["address"] = addr
    try:
        r = yp_us.pooled_get(_API + quote(addr), timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None or r.status_code != 200:
        return [row]
    try:
        data = r.json()
    except ValueError:
        return [row]
    if not isinstance(data, list) or not data:
        return [row]
    d = data[0]
    row.update({
        "lat": d.get("lat") or "",
        "lon": d.get("lon") or "",
        "matched_address": d.get("display_name") or "",
        "type": d.get("type") or "",
        "class": d.get("class") or "",
        "importance": round(float(d["importance"]), 4) if d.get("importance") is not None else "",
        "osm_type": d.get("osm_type") or "",
        "osm_id": d.get("osm_id") or "",
    })
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, geocoding
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await geocoding.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
