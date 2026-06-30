"""Domain Information — WHOIS/registration data for a domain (registrar, dates, nameservers, status).

Uses RDAP (the modern, structured JSON replacement for WHOIS) via the public `rdap.org` bootstrap,
fetched THROUGH A PROXY (real IP never used). No key. Registrant contact details are usually redacted
(GDPR) but registrar, creation/expiry dates, nameservers and status come through. One row per domain.
"""
import asyncio
from datetime import datetime

from . import yp_us
from .config import settings
from .emails_contacts import _url

DI_COLUMNS = ["domain", "registrar", "created", "updated", "expires", "status", "nameservers",
              "registrant"]

_API = "https://rdap.org/domain/"


def _domain_only(q: str) -> str:
    host = _url(q).split("//", 1)[-1].split("/", 1)[0].lower()
    return host[4:] if host.startswith("www.") else host


def _vcard_name(entity: dict) -> str:
    """Pull the 'fn' (full name / org) from an RDAP entity's jCard vcardArray."""
    arr = entity.get("vcardArray")
    if isinstance(arr, list) and len(arr) == 2 and isinstance(arr[1], list):
        for field in arr[1]:
            if isinstance(field, list) and len(field) >= 4 and field[0] == "fn":
                return field[3] or ""
    return ""


def _entity_by_role(entities, role: str) -> dict:
    for e in entities or []:
        if isinstance(e, dict) and role in (e.get("roles") or []):
            return e
    return {}


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input domain -> one WHOIS/RDAP row."""
    dom = _domain_only(query)
    if not dom:
        return []
    row = {c: "" for c in DI_COLUMNS}
    row["domain"] = dom
    try:
        r = yp_us.pooled_get(_API + dom, timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None or r.status_code != 200:
        row["status"] = "not found" if (r is not None and r.status_code == 404) else "lookup failed"
        return [row]
    try:
        d = r.json()
    except ValueError:
        return [row]

    events = {e.get("eventAction"): e.get("eventDate") for e in (d.get("events") or [])
              if isinstance(e, dict)}
    entities = d.get("entities") or []
    nameservers = [ns.get("ldhName", "") for ns in (d.get("nameservers") or []) if isinstance(ns, dict)]
    statuses = d.get("status") or []
    row.update({
        "registrar": _vcard_name(_entity_by_role(entities, "registrar")),
        "created": (events.get("registration") or "")[:10],
        "updated": (events.get("last changed") or "")[:10],
        "expires": (events.get("expiration") or "")[:10],
        "status": ", ".join(statuses) if isinstance(statuses, list) else str(statuses),
        "nameservers": ", ".join(n for n in nameservers if n),
        "registrant": _vcard_name(_entity_by_role(entities, "registrant")),
    })
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, domain_info
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await domain_info.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
