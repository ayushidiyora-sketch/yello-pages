"""IPInfo Scraper — geolocation + network info for an IP address.

Uses the free ip-api.com JSON endpoint (no key) THROUGH A PROXY (real IP never used). Input is one IP
per line; returns country/region/city/zip, coordinates, timezone, ISP/org and the AS number. One row
per IP.
"""
import asyncio
from datetime import datetime

from . import yp_us
from .config import settings

IPINFO_COLUMNS = ["query", "ip", "city", "region", "country", "country_code", "zip", "lat", "lon",
                  "timezone", "isp", "org", "as", "status"]

_API = ("http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,zip,"
        "lat,lon,timezone,isp,org,as,query")


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    ip = (query or "").strip()
    if not ip:
        return []
    row = {c: "" for c in IPINFO_COLUMNS}
    row.update(query=ip, ip=ip)
    try:
        r = yp_us.pooled_get(_API.format(ip=ip), timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None or r.status_code != 200:
        row["status"] = "lookup failed"
        return [row]
    try:
        d = r.json()
    except ValueError:
        row["status"] = "no data"
        return [row]
    if d.get("status") != "success":
        row["status"] = d.get("message") or "not found"
        return [row]
    row.update({
        "ip": d.get("query") or ip,
        "city": d.get("city") or "",
        "region": d.get("regionName") or "",
        "country": d.get("country") or "",
        "country_code": d.get("countryCode") or "",
        "zip": d.get("zip") or "",
        "lat": d.get("lat") or "",
        "lon": d.get("lon") or "",
        "timezone": d.get("timezone") or "",
        "isp": d.get("isp") or "",
        "org": d.get("org") or "",
        "as": d.get("as") or "",
        "status": "ok",
    })
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list, limit: int | None = None) -> None:
    from .db import ipinfo
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await ipinfo.insert_many(rows)
                total += len(rows)
            await jobs_update(job_id, total)
        await jobs_update(job_id, total, done=True)
    except Exception as e:
        from .db import jobs
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})


async def jobs_update(job_id, total, done=False):
    from .db import jobs
    upd = {"total_scraped": total}
    if done:
        upd.update(status="done", finished_at=datetime.utcnow())
    await jobs.update_one({"job_id": job_id}, {"$set": upd})
