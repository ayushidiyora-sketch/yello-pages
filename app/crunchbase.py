"""Crunchbase Scraper — company profiles from crunchbase.com organization pages.

A query is a Crunchbase organization URL (https://www.crunchbase.com/organization/<slug>). Each page
is fetched through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool —
NEVER the real IP). One row per company. `limit` caps companies per query (one company per URL).

Crunchbase is protected by Cloudflare (JS challenge) — the same aggressive anti-bot tier as Immowelt
/ Allegro / Mobile.de. The datacenter free pool (and even a real IP) gets a 403, so live scraping
needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The parser below handles both Crunchbase data
shapes — JSON-LD `Organization` and the embedded Angular app-state JSON (the `properties`/`cards`
field map) — so it returns rows as soon as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

CB_COLUMNS = [
    "query", "company", "description", "website", "founded", "employees",
    "industries", "city", "region", "country", "rank", "operating_status",
    "linkedin", "twitter", "facebook", "logo",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _txt(v):
    """Crunchbase fields are often {value: ...} or {value:[{value:..}]}."""
    if isinstance(v, dict):
        return _txt(v.get("value"))
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
    sameas = d.get("sameAs") or []
    if isinstance(sameas, str):
        sameas = [sameas]
    socials = {"linkedin": "", "twitter": "", "facebook": ""}
    for s in sameas:
        sl = str(s).lower()
        if "linkedin" in sl:
            socials["linkedin"] = s
        elif "twitter" in sl or "x.com" in sl:
            socials["twitter"] = s
        elif "facebook" in sl:
            socials["facebook"] = s
    emp = d.get("numberOfEmployees") or ""
    if isinstance(emp, dict):
        emp = emp.get("value") or emp.get("minValue") or ""
    row = {c: "" for c in CB_COLUMNS}
    row.update({
        "query": query,
        "company": _u(d.get("name") or d.get("legalName")),
        "description": _u(re.sub(r"<[^>]+>", " ", str(d.get("description") or "")))[:500].strip(),
        "website": _u(d.get("url") or d.get("sameAs") if isinstance(d.get("url"), str) else ""),
        "founded": _u(str(d.get("foundingDate") or "")[:10]),
        "employees": _u(str(emp or "")),
        "city": _u(addr.get("addressLocality")),
        "region": _u(addr.get("addressRegion")),
        "country": _u(addr.get("addressCountry")),
        "linkedin": socials["linkedin"],
        "twitter": socials["twitter"],
        "facebook": socials["facebook"],
        "logo": _first(d.get("logo") if not isinstance(d.get("logo"), dict) else d.get("logo", {}).get("url")),
    })
    return row


# ---------------- embedded Angular app-state shape ----------------

def _props_map(props) -> dict:
    """Crunchbase 'properties' come as either a dict or a list of {name/identifier, value}."""
    out = {}
    if isinstance(props, dict):
        for k, v in props.items():
            out[k] = v
    elif isinstance(props, list):
        for p in props:
            if isinstance(p, dict):
                k = p.get("name") or p.get("identifier") or p.get("field_id")
                if k:
                    out[k] = p.get("value", p)
    return out


def _row_from_state(html_text: str, query: str) -> dict | None:
    blobs = []
    for m in re.finditer(r'<script[^>]*id="(?:client-app-state|ng-state|serverApp-state)"[^>]*>(.*?)</script>', html_text, re.S):
        blobs.append(m.group(1))
    for m in re.finditer(r'window\.__(?:APP_STATE|INITIAL_STATE|APP)__\s*=\s*(\{.*?\})\s*[;<]', html_text, re.S):
        blobs.append(m.group(1))
    for raw in blobs:
        txt = raw.strip()
        # Angular escapes &q; for quotes in some builds
        txt = txt.replace("&q;", '"').replace("&a;", "&").replace("&s;", "'")
        try:
            data = json.loads(txt)
        except Exception:
            continue
        # find an entity with org-ish properties
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                props = _props_map(cur.get("properties") or cur.get("fields") or {})
                name = _txt(props.get("name") or props.get("identifier"))
                if name and (("short_description" in props) or ("location_identifiers" in props) or ("website" in props) or ("categories" in props)):
                    return _row_from_props(props, name, query)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return None


def _row_from_props(props: dict, name: str, query: str) -> dict:
    locs = props.get("location_identifiers") or props.get("locations") or []
    city = region = country = ""
    if isinstance(locs, list):
        for loc in locs:
            lt = (loc.get("location_type") or "").lower() if isinstance(loc, dict) else ""
            val = _txt(loc)
            if lt == "city" and not city:
                city = val
            elif lt in ("region", "state") and not region:
                region = val
            elif lt == "country" and not country:
                country = val
        if not city and locs:
            city = _txt(locs[0])
    cats = props.get("categories") or props.get("category_groups") or []
    website = props.get("website") or props.get("website_url") or props.get("homepage_url") or ""
    if isinstance(website, dict):
        website = website.get("value") or ""
    row = {c: "" for c in CB_COLUMNS}
    row.update({
        "query": query,
        "company": _u(name),
        "description": _u(_txt(props.get("short_description") or props.get("description")))[:500].strip(),
        "website": _u(website),
        "founded": _u(_txt(props.get("founded_on"))[:10]),
        "employees": _u(_txt(props.get("num_employees_enum")).replace("c_", "").replace("_", "-")),
        "industries": _u(_txt(cats)),
        "city": _u(city),
        "region": _u(region),
        "country": _u(country),
        "rank": _u(_txt(props.get("rank_org") or props.get("rank"))),
        "operating_status": _u(_txt(props.get("operating_status"))),
        "linkedin": _u(_txt(props.get("linkedin"))),
        "twitter": _u(_txt(props.get("twitter"))),
        "facebook": _u(_txt(props.get("facebook"))),
        "logo": _u(_txt(props.get("logo_url") or props.get("profile_image_url"))),
    })
    return row


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
    return {c: doc.get(c, "") for c in CB_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Crunchbase organization URL and store one row per company."""
    from .db import jobs, crunchbase_results
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
                await crunchbase_results.insert_many(rows)
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
