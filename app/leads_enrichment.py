"""Leads & Contacts Enrichment — contacts for each company: emails, phones, full names, job titles
and social links, from a domain/URL.

Input is a list of domains or URLs; each site is fetched THROUGH A PROXY (free rotating pool, paid
PROXY_URL first if set — the real IP is never used) and parsed by the shared `enrich` extractor, which
also crawls about/team/contact pages for per-person names/titles. One row PER CONTACT (per email);
company-level socials/phone are repeated on each contact row.
"""
import asyncio
from datetime import datetime

from . import enrich
from .emails_contacts import _url

_SOCIALS = ["linkedin", "facebook", "instagram", "twitter", "youtube"]
LE_COLUMNS = (["query", "domain", "email", "full_name", "first_name", "last_name", "title", "phone"]
              + _SOCIALS)


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input domain/URL -> one row per contact found (email + person name/title + company socials)."""
    url = _url(query)
    if not url:
        return []
    d = enrich._fetch_sync(url)
    emails = d.get("emails") or []
    persons = d.get("emails_persons") or []
    socials = {s: (d.get(s) or "") for s in _SOCIALS}
    company_phone = (d.get("phones_extra") or [""])[0] if d.get("phones_extra") else ""
    rows = []
    for i in range(max(len(emails), len(persons))):
        email = emails[i] if i < len(emails) else ""
        p = persons[i] if i < len(persons) else {}
        rows.append({
            "query": query, "domain": url, "email": email,
            "full_name": p.get("full_name") or "",
            "first_name": p.get("first_name") or "",
            "last_name": p.get("last_name") or "",
            "title": p.get("title") or d.get("contact_title") or "",
            "phone": p.get("phone") or company_phone,
            **socials,
        })
    if not rows:  # no contacts found — still emit one company row so the domain shows as processed
        rows.append({"query": query, "domain": url, "email": "", "full_name": "", "first_name": "",
                     "last_name": "", "title": "", "phone": company_phone, **socials})
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, leads_enrichment
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await leads_enrichment.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("0 contacts found — the sites returned nothing on the free proxy pool "
                            "(blocked or no contact data); retry, or set a residential PROXY_URL.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
