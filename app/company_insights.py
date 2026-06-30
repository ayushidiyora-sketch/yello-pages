"""Company Insights — firmographics for each company from its website: public status, employee
count, tech stack signals and the site's title/description.

Input is a list of domains or URLs; each site is fetched THROUGH A PROXY (free rotating pool, paid
PROXY_URL first if set — the real IP is never used) and parsed by the shared `enrich` extractor (which
crawls about/team pages for an employee count). Public status is resolved against SEC EDGAR's free
public-company list. One row per company.

Note: revenue and founding year are NOT available from website metadata alone (they need a paid
firmographics provider); the fields below are what can be derived for free from the site itself.
"""
import asyncio
from datetime import datetime

from . import enrich
from .emails_contacts import _url

CI_COLUMNS = ["query", "domain", "company_name", "is_public", "employees", "website_title",
              "website_description", "website_generator", "has_fb_pixel", "has_google_tag"]


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input domain/URL -> one firmographics row."""
    url = _url(query)
    if not url:
        return []
    d = enrich._fetch_sync(url)
    name = d.get("contact_name") or d.get("website_title") or ""
    return [{
        "query": query, "domain": url,
        "company_name": name,
        "is_public": enrich.is_public_company(name) if name else False,
        "employees": d.get("employees"),
        "website_title": d.get("website_title") or "",
        "website_description": d.get("website_description") or "",
        "website_generator": d.get("website_generator") or "",
        "has_fb_pixel": bool(d.get("website_has_fb_pixel")),
        "has_google_tag": bool(d.get("website_has_google_tag")),
    }]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, company_insights
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await company_insights.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("0 companies resolved — the sites returned nothing on the free proxy pool "
                            "(blocked); retry, or set a residential PROXY_URL.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
