"""Angi Scraper — home-service companies from angi.com search / listing pages.

A query is an angi.com listing or search URL — a company-list page
(angi.com/companylist/us/<state>/<category>.htm) or a near-me search
(angi.com/nearme/<category>/?postalCode=<zip>). Each page is fetched through the proxy pool
(paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the real IP).

Angi is reachable on the free pool (not hard bot-walled). The listing page embeds each company as a
JSON-LD `LocalBusiness` (name + address + profile URL); for MAXIMUM DETAIL we then open each company's
profile page and pull its richer JSON-LD — aggregate rating, review count, logo image, opening hours,
phone and description. One row per company. `limit` caps companies per query.
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
    "query", "name", "category", "rating", "review_count", "phone", "description",
    "street", "city", "region", "postal", "country", "image", "hours", "profile_url",
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


def _u(v):
    return html.unescape(str(v)) if v else ""   # Angi JSON-LD double-encodes & as &amp;


def _row(d: dict, category: str, query: str) -> dict | None:
    addr = d.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        addr = {}
    row = {c: "" for c in ANGI_COLUMNS}
    row.update({
        "query": query,
        "name": _u(d.get("name")),
        "category": category,
        "street": _u(addr.get("streetAddress")),
        "city": _u(addr.get("addressLocality")),
        "region": _u(addr.get("addressRegion")),
        "postal": _u(addr.get("postalCode")),
        "country": _u(addr.get("addressCountry")),
        "profile_url": _abs_url(d.get("url") or ""),
    })
    return row if row["name"] else None


_DAY_ABBR = {"Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed", "Thursday": "Thu",
             "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun"}


def _hours_str(spec) -> str:
    """Flatten openingHoursSpecification into 'Mon 5:00-23:00; ...'."""
    if isinstance(spec, dict):
        spec = [spec]
    if not isinstance(spec, list):
        return ""
    parts = []
    for s in spec:
        if not isinstance(s, dict):
            continue
        day = s.get("dayOfWeek") or ""
        if isinstance(day, list):
            day = day[0] if day else ""
        day = _DAY_ABBR.get(str(day).rsplit("/", 1)[-1], str(day).rsplit("/", 1)[-1])
        opens = ":".join(str(s.get("opens") or "").split(":")[:2])
        closes = ":".join(str(s.get("closes") or "").split(":")[:2])
        if day and (opens or closes):
            parts.append(f"{day} {opens}-{closes}")
    return "; ".join(parts)


def _detail_node(html_text: str) -> dict:
    """The main business JSON-LD node on a profile page (the one with aggregateRating/hours)."""
    soup = BeautifulSoup(html_text, "lxml")
    best = {}
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if cur.get("name") and (cur.get("aggregateRating") or cur.get("openingHoursSpecification") or cur.get("image")):
                    # prefer the richest node
                    if len(cur) > len(best):
                        best = cur
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return best


def _phone(html_text: str) -> str:
    m = re.search(r'href="tel:([+\d().\- ]{7,})"', html_text)
    if m:
        return m.group(1).strip()
    m = re.search(r'\(?\d{3}\)?[ .\-]?\d{3}[ .\-]?\d{4}', BeautifulSoup(html_text, "lxml").get_text(" "))
    return m.group(0) if m else ""


def _enrich_from_profile(row: dict, html_text: str) -> dict:
    """Merge richer fields from a company's profile page into its listing row."""
    node = _detail_node(html_text)
    agg = node.get("aggregateRating") or {}
    if isinstance(agg, dict) and agg.get("ratingValue") is not None:
        try:
            row["rating"] = str(round(float(agg.get("ratingValue")), 2))
        except Exception:
            row["rating"] = str(agg.get("ratingValue"))
        row["review_count"] = str(agg.get("reviewCount") or agg.get("ratingCount") or "")
    img = node.get("image")
    row["image"] = _u(img[0] if isinstance(img, list) and img else img)
    row["hours"] = _hours_str(node.get("openingHoursSpecification"))
    # address may be fuller on the profile page
    addr = node.get("address") or {}
    if isinstance(addr, dict):
        row["street"] = row["street"] or _u(addr.get("streetAddress"))
        row["country"] = row["country"] or _u(addr.get("addressCountry"))
    row["phone"] = _phone(html_text)
    md = re.search(r'<meta name="description" content="([^"]+)"', html_text)
    if md:
        row["description"] = _u(md.group(1))[:500]
    return row


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

def _fetch(url: str):
    """Fetch a URL through the proxy pool, retrying a few times for flaky free IPs."""
    for _ in range(4):
        try:
            r = yp_us.pooled_get(url, {}, timeout=25)
        except Exception:
            continue
        if r is not None and r.status_code == 200 and len(r.text or "") > 3000:
            return r.text
    return None


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = (query or "").strip()
    if not url.lower().startswith("http"):
        return []
    html_text = _fetch(url)
    if not html_text:
        return []
    rows = _parse(html_text, query)
    rows = rows[:limit] if limit else rows
    # MAXIMUM DETAIL: open each company's profile page for rating, reviews, phone, hours, etc.
    for row in rows:
        prof = row.get("profile_url")
        if not prof:
            continue
        page = _fetch(prof)
        if page:
            _enrich_from_profile(row, page)
    return rows


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
