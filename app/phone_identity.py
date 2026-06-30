"""Phone Identity Finder — owner name + address behind a (US) phone number.

Reuses the shared reverse-phone lookup (`whitepages.lookup_sync`, free via thatsthem.com) which fetches
the owner's JSON-LD `Person` record THROUGH A PROXY (real IP never used). US numbers only; thatsthem is
rate-limited / Cloudflare-prone on datacenter IPs, so a number with no record (or a blocked proxy) just
yields empty owner fields — best-effort, never an error. One row per number.
"""
import asyncio
from datetime import datetime

from . import whitepages

PI_COLUMNS = ["phone", "owner_name", "address", "found"]


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input phone -> one owner row (name + address; empty when no public record)."""
    raw = (query or "").strip()
    if not raw:
        return []
    res = whitepages.lookup_sync(raw)
    name = res.get("name") or ""
    return [{
        "phone": raw,
        "owner_name": name,
        "address": res.get("address") or "",
        "found": bool(name),
    }]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, phone_identity
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await phone_identity.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("No owner records — thatsthem may have been rate-limited/blocked on the "
                            "proxy, or the numbers have no public listing; retry, or use residential proxies.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
