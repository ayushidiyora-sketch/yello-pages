"""Emails & Contacts Scraper — emails, social links, phones and website meta from any website.

Input is a list of domains or URLs (e.g. "stripe.com" or "https://stripe.com/contact"); for each one
the site is fetched THROUGH A PROXY (free rotating pool, paid PROXY_URL first if set — the real IP is
never used) and parsed by the shared `enrich` extractor. General company websites are not anti-bot
protected, so this works on the free datacenter pool. One result row per input domain.
"""
import asyncio
from datetime import datetime

from . import enrich

# the social platforms enrich extracts (in display order) + the rest of the flattened columns
_SOCIALS = ["facebook", "instagram", "linkedin", "twitter", "youtube", "tiktok"]
EC_COLUMNS = (["query", "domain", "emails", "phones"] + _SOCIALS +
              ["website_title", "website_description", "contact_name"])


def _url(q: str) -> str:
    """A http(s) URL as-is; a bare domain -> https://<domain>."""
    q = (q or "").strip()
    if not q:
        return ""
    return q if q.lower().startswith("http") else "https://" + q.lstrip("/")


def _row(query: str, url: str, d: dict) -> dict:
    """Flatten enrich's dict into one display/export row (lists -> '; '-joined strings)."""
    row = {
        "query": query,
        "domain": url,
        "emails": "; ".join(d.get("emails") or []),
        "phones": "; ".join(str(p) for p in (d.get("phones_extra") or [])),
        "website_title": d.get("website_title") or "",
        "website_description": d.get("website_description") or "",
        "contact_name": d.get("contact_name") or "",
    }
    for s in _SOCIALS:
        row[s] = d.get(s) or ""
    return row


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input domain/URL -> one row of emails/socials/phones/meta (empty fields if blocked/no data)."""
    url = _url(query)
    if not url:
        return []
    data = enrich._fetch_sync(url)
    return [_row(query, url, data)]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, emails_contacts
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await emails_contacts.insert_many(rows)
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
