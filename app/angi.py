"""Angi Scraper — home-service companies from angi.com search / listing pages.

A query is an angi.com listing or search URL — a company-list page
(angi.com/companylist/us/<state>/<category>.htm) or a near-me search
(angi.com/nearme/<category>/?postalCode=<zip>). Each page is fetched through the proxy pool
(paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the real IP).

Angi is reachable on the free pool (not hard bot-walled). Company entries are embedded as JSON-LD
`LocalBusiness` / `HomeAndConstructionBusiness` objects (name + postal address + profile URL); we
parse those. One row per company. `limit` caps companies per query.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

BASE = "https://www.angi.com"

ANGI_COLUMNS = [
    "query", "name", "category", "street", "city", "region", "postal", "country", "profile_url",
]

_BIZ_TYPES = ("LocalBusiness", "HomeAndConstructionBusiness", "Plumber", "Electrician",
              "GeneralContractor", "RoofingContractor", "HVACBusiness", "Locksmith")


def _category(query: str) -> str:
    """Best-effort service category from the URL (…/nearme/<cat>/… or …/companylist/.../<cat>.htm)."""
    q = (query or "").lower()
    m = re.search(r"/nearme/([a-z-]+)", q) or re.search(r"/companylist/[^?]*?/([a-z-]+)\.htm", q)
    return m.group(1).replace("-", " ") if m else ""


def _is_business(d: dict) -> bool:
    if not isinstance(d, dict) or not d.get("name"):
        return False
    t = d.get("@type", "")
    types = t if isinstance(t, list) else [t]
    return any(("Business" in str(x)) or str(x) in _BIZ_TYPES for x in types)


def _abs_url(u: str) -> str:
    if not u:
        return ""
    return u if u.startswith("http") else BASE + (u if u.startswith("/") else "/" + u)


def _row(d: dict, category: str, query: str) -> dict | None:
    addr = d.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        addr = {}
    u = lambda v: html.unescape(str(v)) if v else ""   # Angi JSON-LD double-encodes & as &amp;
    row = {c: "" for c in ANGI_COLUMNS}
    row.update({
        "query": query,
        "name": u(d.get("name")),
        "category": category,
        "street": u(addr.get("streetAddress")),
        "city": u(addr.get("addressLocality")),
        "region": u(addr.get("addressRegion")),
        "postal": u(addr.get("postalCode")),
        "country": u(addr.get("addressCountry")),
        "profile_url": _abs_url(d.get("url") or ""),
    })
    return row if row["name"] else None


def _parse(html: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    category = _category(query)
    out, seen = [], set()
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if _is_business(cur):
                    row = _row(cur, category, query)
                    if row:
                        key = (row["name"], row["street"], row["city"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = (query or "").strip()
    if not url.lower().startswith("http"):
        return []
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return []
    if r is None or r.status_code != 200:
        return []
    rows = _parse(r.text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in ANGI_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Angi listing/search URL and store one row per company."""
    from .db import jobs, angi_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await angi_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "stopped" if stopped else "done", "total_scraped": total,
            "finished_at": datetime.utcnow()}})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
