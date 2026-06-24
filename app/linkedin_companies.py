"""LinkedIn Companies Scraper — company details from a linkedin.com/company page (proxy-only).

LinkedIn shows a logged-out "authwall", but its public company pages still embed an SEO
`application/ld+json` Organization block (name, description, employees, address, logo, website). We
fetch the page through the proxy pool (`yp_us.pooled_get` — the paid PROXY_URL if set, else a warm
free-pool proxy; the REAL IP is never used) and read that JSON-LD; follower count comes from the page.

Input per line: a company URL (linkedin.com/company/<slug>), a bare slug ("outscraper"), or a numeric
company id. LinkedIn aggressively rate-limits datacenter/free IPs — a paid residential PROXY_URL is
the most reliable; on the free pool a company may come back empty (authwall) until retried.
"""
import asyncio
import json
import re
from datetime import datetime
from html import unescape

from . import yp_us
from .scraper import STOP_REQUESTS

LINKEDIN_COMPANY_COLUMNS = ["query", "name", "industry", "description", "website", "headquarters",
                            "company_size", "employees", "company_type", "founded", "specialties",
                            "followers", "slogan", "logo", "linkedin_url"]

_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_FOLLOWERS = re.compile(r'([\d,]+)\s+followers', re.I)
_DT_DD = re.compile(r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _to_url(q: str) -> str:
    """A linkedin.com/company URL as-is (normalized); a bare slug or numeric id -> a /company/ URL."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        return q.split("?")[0].rstrip("/") + "/"
    return f"https://www.linkedin.com/company/{q.strip('/')}/"


def _richest_org(node) -> dict | None:
    """Collect every schema.org Organization object in the ld+json graph and return the one with the
    most fields (the page has several stub Organization refs alongside the real one)."""
    orgs: list[dict] = []

    def collect(d):
        if isinstance(d, dict):
            ty = d.get("@type")
            if ty == "Organization" or (isinstance(ty, list) and "Organization" in ty):
                orgs.append(d)
            for v in d.values():
                collect(v)
        elif isinstance(d, list):
            for v in d:
                collect(v)

    collect(node)
    return max(orgs, key=lambda o: len(o.keys())) if orgs else None


def _txt(v) -> str:
    return unescape(_TAG.sub(" ", str(v))).replace("\xa0", " ").strip() if v else ""


def _about_pairs(html: str) -> dict:
    """The company 'About' panel renders detail rows as <dt>Label</dt><dd>Value</dd> (Website,
    Industry, Company size, Headquarters, Type, Founded, Specialties). Return them as {label: value}."""
    out = {}
    for k, v in _DT_DD.findall(html or ""):
        label = re.sub(r"\s+", " ", _txt(k)).lower()
        value = re.sub(r"\s+", " ", _txt(v)).strip()
        if label and value and label not in out:
            # "Website" value trails "External link for <name>" — keep just the URL/first part
            out[label] = value.split(" External link")[0].strip()
    return out


def _row(query: str, html: str) -> dict | None:
    org = None
    for block in _LD.findall(html or ""):
        try:
            org = _richest_org(json.loads(block.strip()))
        except Exception:
            continue
        if org and org.get("name"):
            break
    if not org or not org.get("name"):
        return None

    addr = org.get("address") or {}
    hq = ", ".join(str(addr.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")
                   if isinstance(addr, dict) and addr.get(k))
    emp = org.get("numberOfEmployees")
    if isinstance(emp, dict):
        emp = emp.get("value")
    logo = org.get("logo")
    if isinstance(logo, dict):
        logo = logo.get("contentUrl") or logo.get("url")
    fol = _FOLLOWERS.search(html or "")
    about = _about_pairs(html)            # Website/Industry/Company size/Headquarters/Type/Founded/Specialties
    website = about.get("website") or (org.get("sameAs") if isinstance(org.get("sameAs"), str) else "")
    return {
        "query": query,
        "name": _txt(org.get("name")),
        "industry": about.get("industry") or _txt(org.get("industry")),
        "description": _txt(org.get("description")),
        "website": (website or "").strip(),
        "headquarters": about.get("headquarters") or hq,
        "company_size": about.get("company size", ""),
        "employees": emp if isinstance(emp, (int, float)) else _txt(emp),
        "company_type": about.get("type", ""),
        "founded": about.get("founded", ""),
        "specialties": about.get("specialties", ""),
        "followers": fol.group(1) if fol else "",
        "slogan": _txt(org.get("slogan")),
        "logo": logo if isinstance(logo, str) else "",
        "linkedin_url": (org.get("url") or "").strip(),
    }


def search_sync(query: str, job_id: str | None = None) -> list[dict]:
    r = yp_us.pooled_get(_to_url(query).split("?")[0], {}, timeout=20)
    if r is None:
        raise RuntimeError("No proxy available to reach LinkedIn (set a PROXY_URL, or wait for the "
                           "free pool to warm up). The real IP is never used.")
    row = _row(query, r.text)
    return [row] if row else []


async def search(query: str, job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, job_id)


async def run_job(job_id: str, queries: list[str]) -> None:
    from .db import jobs, linkedin_companies_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:               # Stop button pressed
                break
            rows = await search(q, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await linkedin_companies_results.insert_many(rows)
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
