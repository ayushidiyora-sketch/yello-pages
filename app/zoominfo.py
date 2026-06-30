"""Zoominfo by Domains — company firmographics from ZoomInfo for a domain.

ZoomInfo is a paid B2B database behind heavy anti-bot protection: zoominfo.com returns 403 to datacenter
IPs and the underlying data needs a paid ZoomInfo account/API. There is no free source. This service is
wired for completeness and fetches THROUGH A PROXY (real IP never used); on the free pool every domain
comes back with a clear "blocked" status. With a residential proxy + (ideally) a ZoomInfo session it can
return the public company page. One row per domain.
"""
import asyncio
import re
from datetime import datetime

from . import yp_us
from .config import settings
from .emails_contacts import _url

ZI_COLUMNS = ["domain", "company_name", "zoominfo_url", "status"]

_SEARCH = "https://www.zoominfo.com/companies-search/keyword/"


def _domain_only(q: str) -> str:
    host = _url(q).split("//", 1)[-1].split("/", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input domain -> one row (best-effort; 'blocked' on datacenter IPs)."""
    dom = _domain_only(query)
    if not dom:
        return []
    row = {c: "" for c in ZI_COLUMNS}
    row["domain"] = dom
    try:
        r = yp_us.pooled_get(_SEARCH + dom, timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None:
        row["status"] = "no response (proxy)"
        return [row]
    if r.status_code in (403, 429):
        row["status"] = "blocked (ZoomInfo is paid/protected — needs residential proxy + account)"
        return [row]
    if r.status_code != 200:
        row["status"] = f"http {r.status_code}"
        return [row]
    m = re.search(r"<title>([^<]+)</title>", r.text, re.I)
    row["company_name"] = (m.group(1).strip() if m else "")
    row["zoominfo_url"] = _SEARCH + dom
    row["status"] = "ok"
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, zoominfo
    total = 0
    blocked = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
                if r.get("status", "").startswith("blocked"):
                    blocked += 1
            if rows:
                await zoominfo.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if blocked:
            done["note"] = (f"{blocked} domain(s) blocked — ZoomInfo is a paid, anti-bot-protected B2B "
                            "database with no free source; needs a residential proxy + a ZoomInfo account.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
