"""ZoomInfo Scraper — company profiles from zoominfo.com company pages.

A query is a ZoomInfo company URL (https://www.zoominfo.com/c/<slug>/<id>). Each page is fetched
through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the
real IP). One row per company. `limit` caps companies per query (one company per URL).

ZoomInfo is protected by Cloudflare (JS challenge) — the same aggressive anti-bot tier as Crunchbase
/ Immowelt / Allegro. The datacenter free pool (and even a real IP) gets a 403, so live scraping
needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The parser below handles both ZoomInfo data
shapes — JSON-LD `Organization`/`Corporation` and the embedded Next/Angular app-state JSON — so it
returns rows as soon as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

ZI_COLUMNS = [
    "query", "company", "website", "phone", "employees", "revenue", "industry",
    "founded", "address", "city", "state", "country", "ticker", "logo",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _txt(v):
    if isinstance(v, dict):
        return _txt(v.get("value") or v.get("name") or v.get("text"))
    if isinstance(v, list):
        return ", ".join(_txt(x) for x in v if _txt(x))
    return str(v) if v not in (None, "") else ""


# ---------------- JSON-LD shape ----------------

def _row_from_ld(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict) or not (d.get("name") or d.get("legalName")):
        return None
    addr = d.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        addr = {}
    emp = d.get("numberOfEmployees") or ""
    if isinstance(emp, dict):
        emp = emp.get("value") or emp.get("minValue") or ""
    row = {c: "" for c in ZI_COLUMNS}
    row.update({
        "query": query,
        "company": _u(d.get("name") or d.get("legalName")),
        "website": _u(d.get("url") if isinstance(d.get("url"), str) else ""),
        "phone": _u(d.get("telephone")),
        "employees": _u(str(emp or "")),
        "industry": _u(_txt(d.get("industry") or d.get("naics") or "")),
        "founded": _u(str(d.get("foundingDate") or "")[:10]),
        "address": _u(addr.get("streetAddress")),
        "city": _u(addr.get("addressLocality")),
        "state": _u(addr.get("addressRegion")),
        "country": _u(addr.get("addressCountry")),
        "ticker": _u(_txt(d.get("tickerSymbol"))),
        "logo": _first(d.get("logo") if not isinstance(d.get("logo"), dict) else d.get("logo", {}).get("url")),
    })
    return row


# ---------------- embedded app-state shape ----------------

def _looks_like_company(d: dict) -> bool:
    keys = set(d.keys())
    has_name = bool(keys & {"name", "companyName", "legalName"})
    has_attr = bool(keys & {"revenue", "employeeCount", "numberOfEmployees", "website",
                            "companyWebsite", "industry", "primaryIndustry", "phone"})
    return has_name and has_attr


def _row_from_obj(d: dict, query: str) -> dict | None:
    name = _txt(d.get("name") or d.get("companyName") or d.get("legalName"))
    if not name:
        return None
    addr = d.get("address") or d.get("headquarters") or d.get("location") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        addr = {}
    row = {c: "" for c in ZI_COLUMNS}
    row.update({
        "query": query,
        "company": _u(name),
        "website": _u(_txt(d.get("website") or d.get("companyWebsite") or d.get("url") or d.get("websiteUrl"))),
        "phone": _u(_txt(d.get("phone") or d.get("phoneNumber") or d.get("telephone"))),
        "employees": _u(_txt(d.get("employeeCount") or d.get("numberOfEmployees") or d.get("employees"))),
        "revenue": _u(_txt(d.get("revenue") or d.get("annualRevenue") or d.get("revenueRange"))),
        "industry": _u(_txt(d.get("primaryIndustry") or d.get("industry") or d.get("industries"))),
        "founded": _u(_txt(d.get("foundedYear") or d.get("founded") or d.get("foundingDate"))[:10]),
        "address": _u(_txt(addr.get("street") or addr.get("streetAddress") or addr.get("addressLine1"))),
        "city": _u(_txt(addr.get("city") or addr.get("addressLocality"))),
        "state": _u(_txt(addr.get("state") or addr.get("addressRegion"))),
        "country": _u(_txt(addr.get("country") or addr.get("addressCountry"))),
        "ticker": _u(_txt(d.get("ticker") or d.get("tickerSymbol") or d.get("stockSymbol"))),
        "logo": _u(_txt(d.get("logo") or d.get("logoUrl") or d.get("companyLogo"))),
    })
    return row


def _row_from_state(html_text: str, query: str) -> dict | None:
    blobs = []
    for m in re.finditer(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html_text, re.S):
        blobs.append(m.group(1))
    for m in re.finditer(r'<script[^>]*id="(?:serverApp-state|ng-state|client-app-state)"[^>]*>(.*?)</script>', html_text, re.S):
        blobs.append(m.group(1).replace("&q;", '"').replace("&a;", "&").replace("&s;", "'"))
    for m in re.finditer(r'window\.__(?:APP_STATE|INITIAL_STATE|PRELOADED_STATE|appData)__?\s*=\s*(\{.*?\})\s*[;<]', html_text, re.S):
        blobs.append(m.group(1))
    for raw in blobs:
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if _looks_like_company(cur):
                    row = _row_from_obj(cur, query)
                    if row:
                        return row
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return None


def _parse(html_text: str, query: str) -> dict | None:
    soup = BeautifulSoup(html_text, "lxml")
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                t = cur.get("@type")
                tset = set(t) if isinstance(t, list) else {t}
                if tset & {"Organization", "Corporation", "LocalBusiness"} and (cur.get("name") or cur.get("legalName")):
                    row = _row_from_ld(cur, query)
                    if row:
                        return row
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return _row_from_state(html_text, query)


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
    row = _parse(r.text, query)
    return [row] if row else []


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in ZI_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each ZoomInfo company URL and store one row per company."""
    from .db import jobs, zoominfo_results
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
                await zoominfo_results.insert_many(rows)
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
