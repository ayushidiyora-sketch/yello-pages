"""Whitepages Addresses Scraper — insights about an address and its residents.

Two parts per address:
  - location: geocoded via OpenStreetMap (reliable, works on the free pool) — reused from `geocoding`.
  - residents: best-effort reverse-address lookup (thatsthem) THROUGH A PROXY (real IP never used).
    whitepages.com itself is Cloudflare-walled; thatsthem serves the same data but is rate-limited on
    datacenter IPs, so residents usually come back empty on the free pool (needs a residential proxy).
One row per address.
"""
import asyncio
import json
import re
from datetime import datetime

from . import geocoding, yp_us
from .config import settings

AR_COLUMNS = ["address", "lat", "lon", "matched_address", "residents", "status"]
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _slug(addr: str) -> str:
    """thatsthem address URL: /address/<street-slug>/<city-state-slug> (best-effort from a flat string)."""
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if not parts:
        return ""
    street = re.sub(r"[^a-z0-9]+", "-", parts[0].lower()).strip("-")
    locality = re.sub(r"[^a-z0-9]+", "-", " ".join(parts[1:]).lower()).strip("-")
    return f"https://thatsthem.com/address/{street}/{locality}" if locality else ""


def _residents(addr: str) -> tuple:
    """(residents_str, status). Best-effort thatsthem reverse-address; empty on block/no record."""
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
    names = []
    for m in _LD_RE.finditer(r.text):
        try:
            d = json.loads(m.group(1))
        except (ValueError, json.JSONDecodeError):
            continue
        graph = d.get("@graph", [d]) if isinstance(d, dict) else (d if isinstance(d, list) else [d])
        for n in graph:
            if isinstance(n, dict) and n.get("@type") == "Person" and n.get("name"):
                if n["name"] not in names:
                    names.append(n["name"])
    return ("; ".join(names[:10]), "ok") if names else ("", "no public residents")


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
    from .db import jobs, whitepages_addresses
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await whitepages_addresses.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
