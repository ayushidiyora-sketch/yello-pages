"""SimilarWeb Scraper — traffic, rank, engagement and audience data for a domain.

Reads SimilarWeb's own INTERNAL JSON API (the endpoint its frontend/extension calls):
`https://data.similarweb.com/api/v1/data?domain=<domain>` — no HTML scraping, no auth. Fetched THROUGH
A PROXY (real IP never used).

IMPORTANT — residential-only: SimilarWeb fronts this endpoint with a CloudFront WAF that hard-blocks
datacenter IPs (403 on every free/datacenter proxy, verified). It returns data only from residential
IPs. On a blocked IP the row comes back empty with a clear note — best-effort, never an error. One row
per domain.
"""
import asyncio
from datetime import datetime

from . import yp_us
from .config import settings
from .emails_contacts import _url

SW_COLUMNS = ["domain", "site_name", "category", "global_rank", "country_code", "country_rank",
              "total_visits", "bounce_rate", "pages_per_visit", "avg_visit_duration",
              "top_countries", "status"]

_API = "https://data.similarweb.com/api/v1/data?domain="


def _domain_only(q: str) -> str:
    """Bare registrable domain from a URL/domain input (the API wants 'example.com', not a URL)."""
    u = _url(q)
    host = u.split("//", 1)[-1].split("/", 1)[0]
    return host.lower().lstrip("www.") if host.startswith("www.") else host.lower()


def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _duration(secs) -> str:
    s = _fnum(secs)
    if s is None:
        return ""
    s = int(s)
    return f"{s // 60:02d}:{s % 60:02d}"


def _top_countries(d: dict) -> str:
    """'US 31%, IN 12%, GB 7%' from TopCountryShares (best 3)."""
    out = []
    for c in (d.get("TopCountryShares") or [])[:3]:
        if not isinstance(c, dict):
            continue
        code = c.get("CountryCode") or c.get("Country")
        share = _fnum(c.get("Value"))
        if code is not None and share is not None:
            out.append(f"{code} {round(share * 100, 1)}%")
    return ", ".join(out)


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input domain -> one SimilarWeb row (empty + status when the IP is WAF-blocked)."""
    dom = _domain_only(query)
    if not dom:
        return []
    row = {c: "" for c in SW_COLUMNS}
    row["domain"] = dom
    try:
        r = yp_us.pooled_get(_API + dom, timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None:
        row["status"] = "no response (proxy)"
        return [row]
    if r.status_code == 403:
        row["status"] = "blocked (needs residential proxy)"
        return [row]
    if r.status_code != 200:
        row["status"] = f"http {r.status_code}"
        return [row]
    try:
        d = r.json()
    except ValueError:
        row["status"] = "no data"
        return [row]
    if not isinstance(d, dict) or not d.get("SiteName"):
        row["status"] = "no data"
        return [row]

    eng = d.get("Engagments") or {}
    gr = (d.get("GlobalRank") or {}).get("Rank")
    cr = d.get("CountryRank") or {}
    visits = _fnum(eng.get("Visits"))
    bounce = _fnum(eng.get("BounceRate"))
    ppv = _fnum(eng.get("PagePerVisit"))
    row.update({
        "site_name": d.get("SiteName") or "",
        "category": d.get("Category") or "",
        "global_rank": gr if gr is not None else "",
        "country_code": cr.get("CountryCode") or "",
        "country_rank": cr.get("Rank") if cr.get("Rank") is not None else "",
        "total_visits": int(visits) if visits is not None else "",
        "bounce_rate": f"{round(bounce * 100, 1)}%" if bounce is not None else "",
        "pages_per_visit": round(ppv, 2) if ppv is not None else "",
        "avg_visit_duration": _duration(eng.get("TimeOnSite")),
        "top_countries": _top_countries(d),
        "status": "ok",
    })
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, similarweb
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
                await similarweb.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if blocked:
            done["note"] = (f"{blocked} domain(s) were WAF-blocked (403) — SimilarWeb's API rejects "
                            "datacenter IPs; use a residential PROXY_URL to get traffic data.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
