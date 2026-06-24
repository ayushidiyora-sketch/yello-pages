"""Glassdoor Company Search — company search results from Glassdoor.

Same design as the other live scrapers: every request goes through a proxy IP (a paid PROXY_URL if
set, otherwise the rotating free pool — NEVER the real IP). Glassdoor is bot-protected (Cloudflare),
so on the free pool it is often blocked and returns 0 until a paid residential PROXY_URL is set (same
behaviour as the Glassdoor Jobs / Reviews scrapers — the proxy fetch is reused from glassdoor_jobs).

A query line is a company name (e.g. "google") or a Glassdoor search/explore URL. The `domain` picks
the Glassdoor country site (glassdoor.com, glassdoor.co.in, fr.glassdoor.ca, ...). Company data is
read from the page's embedded JSON (`__NEXT_DATA__` / Apollo cache); visible cards are the fallback.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from .glassdoor_jobs import _get_text          # proxy fetch (paid PROXY_URL or free pool; never real IP)
from .scraper import STOP_REQUESTS

GLASSDOOR_COMPANY_COLUMNS = ["query", "company", "rating", "reviews", "industry", "size",
                             "headquarters", "website", "company_url", "logo"]


def _base(domain: str) -> str:
    """A Glassdoor country domain -> a https base. 'glassdoor.com' -> www.glassdoor.com;
    a domain that already carries a country subdomain ('nl.glassdoor.be') is used as-is."""
    d = (domain or "glassdoor.com").strip().strip("/")
    if d.startswith("http"):
        return d.rstrip("/")
    host = ("www." + d) if d.startswith("glassdoor.") else d
    return "https://" + host


def _search_url(query: str, domain: str) -> str:
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q
    return f"{_base(domain)}/Search/results.htm?keyword={quote(q)}"


# ---------------- parsing ----------------

_EMP_TYPENAMES = ("employer", "employersearchresult", "companysearchresult")


def _industry(node: dict) -> str:
    pi = node.get("primaryIndustry")
    if isinstance(pi, dict):
        return pi.get("industryName") or pi.get("name") or ""
    return node.get("industryName") or node.get("industry") or ""


def _hq(node: dict) -> str:
    h = node.get("headquarters") or node.get("hqLocation")
    if isinstance(h, dict):
        return ", ".join(x for x in (h.get("cityName") or h.get("city"),
                                     h.get("regionName") or h.get("country")) if x)
    return h or ""


def _walk_employers(node, out, seen, base: str):
    """Recursively collect company-like dicts from the page's embedded JSON. An entry needs a name
    plus a company signal (Employer typename, an overall rating, or a review/rating count)."""
    if isinstance(node, dict):
        tn = str(node.get("__typename") or "").lower()
        name = node.get("name") or node.get("shortName") or node.get("employerName")
        rating = node.get("overallRating")
        if rating is None:
            rating = node.get("rating")
        looks_company = (tn in _EMP_TYPENAMES or any(k in node for k in
                         ("overallRating", "reviewCount", "ratingCount", "numberOfRatings")))
        if name and isinstance(name, str) and looks_company:
            key = name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                eid = node.get("id") or node.get("employerId")
                curl = node.get("websiteUrl") or ""
                overview = ""
                if eid:
                    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
                    overview = f"{base}/Overview/Working-at-{slug}-EI_IE{eid}.htm"
                out.append({
                    "company": name,
                    "rating": rating if rating not in (None, "") else "",
                    "reviews": (node.get("reviewCount") or node.get("ratingCount")
                                or node.get("numberOfRatings") or ""),
                    "industry": _industry(node),
                    "size": node.get("sizeCategory") or node.get("size") or "",
                    "headquarters": _hq(node),
                    "website": curl,
                    "company_url": overview,
                    "logo": (node.get("squareLogoUrl") or node.get("logoUrl")
                             or node.get("logo") or ""),
                })
        for v in node.values():
            _walk_employers(v, out, seen, base)
    elif isinstance(node, list):
        for v in node:
            _walk_employers(v, out, seen, base)


def _parse(html: str, query: str, base: str) -> list[dict]:
    rows, seen = [], set()
    soup = BeautifulSoup(html, "lxml")
    # 1) embedded JSON (Next.js __NEXT_DATA__ or any inline app state) — most reliable when present
    blobs = []
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        blobs.append(nd.string)
    for sc in soup.find_all("script", attrs={"type": "application/json"}):
        if sc.string:
            blobs.append(sc.string)
    for b in blobs:
        try:
            data = json.loads(b)
        except Exception:
            continue
        _walk_employers(data, rows, seen, base)
    # 2) fallback: visible company cards (selectors obfuscated — finalize against a real response)
    if not rows:
        for c in soup.select('[data-test="employer-card"], div.employer-card, a[href*="/Overview/"]'):
            name = c.get_text(" ", strip=True)[:80]
            href = c.get("href") if c.name == "a" else ""
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            rows.append({"company": name, "rating": "", "reviews": "", "industry": "",
                         "size": "", "headquarters": "", "website": "",
                         "company_url": (base + href) if href and href.startswith("/") else (href or ""),
                         "logo": ""})
    for r in rows:
        r["query"] = query
    return rows


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None, domain: str) -> list[dict]:
    base = _base(domain)
    html = _get_text(_search_url(query, domain))
    if html is None:
        return []
    rows = _parse(html, query, base)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, domain: str = "glassdoor.com") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, domain)


async def run_job(job_id: str, queries: list[str], limit: int | None,
                  domain: str = "glassdoor.com") -> None:
    from .db import jobs, gcompanies
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, domain)
            if not rows and job_id not in STOP_REQUESTS:      # free proxies flaky — one retry
                rows = await search(q, limit, domain)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await gcompanies.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        done = {"status": "stopped" if stopped else "done", "total_scraped": total,
                "finished_at": datetime.utcnow()}
        if not total and not stopped:
            done["note"] = ("Glassdoor returned 0 companies — its Cloudflare bot-wall blocked the "
                            "free proxies. Set a paid residential PROXY_URL in .env for reliable "
                            "results (the real IP is never used).")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
