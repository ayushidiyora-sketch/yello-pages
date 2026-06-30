"""Reverse Geocoding — coordinates (lat, lon) -> a human-readable address.

Uses OpenStreetMap's free Nominatim reverse API (no key) THROUGH A PROXY (real IP never used). Input is
"lat,lon" per line (e.g. "37.427074,-122.1439166"). One row per coordinate pair.
"""
import asyncio
from datetime import datetime

from . import yp_us
from .config import settings

RG_COLUMNS = ["query", "lat", "lon", "address", "type", "class", "osm_type", "osm_id"]

_API = "https://nominatim.openstreetmap.org/reverse?format=json&addressdetails=1&lat={lat}&lon={lon}"


def _parse_latlon(q: str):
    parts = [p.strip() for p in (q or "").replace(";", ",").split(",") if p.strip()]
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One 'lat,lon' input -> one row with the reverse-geocoded address."""
    q = (query or "").strip()
    if not q:
        return []
    row = {c: "" for c in RG_COLUMNS}
    row["query"] = q
    ll = _parse_latlon(q)
    if ll is None:
        return [row]
    row["lat"], row["lon"] = ll
    try:
        r = yp_us.pooled_get(_API.format(lat=ll[0], lon=ll[1]), timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None or r.status_code != 200:
        return [row]
    try:
        d = r.json()
    except ValueError:
        return [row]
    if not isinstance(d, dict) or d.get("error"):
        return [row]
    row.update({
        "address": d.get("display_name") or "",
        "type": d.get("type") or "",
        "class": d.get("class") or d.get("category") or "",
        "osm_type": d.get("osm_type") or "",
        "osm_id": d.get("osm_id") or "",
    })
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, reverse_geocoding
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await reverse_geocoding.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
