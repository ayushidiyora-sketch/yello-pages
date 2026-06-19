"""Glassdoor Job Scraper — glassdoor.com job-search results.

Same design as the other live scrapers: every request goes through a proxy IP (a paid PROXY_URL
if set, otherwise the rotating free pool — NEVER the real IP). Glassdoor is bot-protected
(Cloudflare), so on the free pool it is often blocked and returns 0 until a paid PROXY_URL is set
(same behaviour as the BBB scraper).

A query is a Glassdoor job-search URL, e.g.
    https://www.glassdoor.com/Job/los-angeles-ca-us-python-jobs-SRCH_IL.0,17_IC1146821_KO18,24.htm
Each query yields up to `limit` job rows.
"""
import asyncio
import json
import re
import threading
from datetime import datetime
from urllib.parse import urlparse, urlencode, parse_qsl

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

BASE = "https://www.glassdoor.com"
MAX_PAGES = 10
_GD_TIMEOUT = 15
_GOOD_PROXY = None          # last proxy that passed glassdoor.com — reused before re-rotating
_PIN_LOCK = threading.Lock()
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

# one row per job
GLASSDOOR_JOB_COLUMNS = [
    "query", "job_title", "company", "company_rating", "location", "salary",
    "date_posted", "easy_apply", "job_url", "snippet", "logo", "position",
]


def _blank_row():
    return {c: "" for c in GLASSDOOR_JOB_COLUMNS}


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    """A real glassdoor.com page (not a Cloudflare block)."""
    if r is None or r.status_code != 200:
        return False
    low = (r.text or "").lower()
    if any(s in low for s in ("just a moment", "cf-browser-verification",
                              "attention required", "/cdn-cgi/challenge")):
        return False
    return "__next_data__" in low or "data-test=\"jobListing\"".lower() in low \
        or "joblisting" in low or "glassdoor" in low


def _try(url: str, px: str):
    try:
        r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                     timeout=_GD_TIMEOUT, verify=False, allow_redirects=True)
        return r if _ok(r) else None
    except Exception:
        return None


def _proxied_get(url: str):
    """Fetch through a free proxy (NEVER the real IP): reuse the last known-good proxy, else
    rotate the pool until one passes and pin it. Raises if none pass."""
    global _GOOD_PROXY
    pinned = _GOOD_PROXY
    if pinned:
        r = _try(url, pinned)
        if r is not None:
            return r
    from . import yp_us
    yp_us.ensure_pool(_SEED, 8)
    seen, candidates = {pinned}, []
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px not in seen:
            seen.add(px)
            candidates.append(px)
    for px in candidates[:15]:
        r = _try(url, px)
        if r is not None:
            with yp_us._LOCK:
                if px in yp_us._GOOD:
                    yp_us._GOOD.remove(px)
                yp_us._GOOD.insert(0, px)
            with _PIN_LOCK:
                _GOOD_PROXY = px
            return r
    raise RuntimeError("no free proxy passed glassdoor.com")


def _get_text(url: str) -> str | None:
    """Fetch one URL through a proxy. Paid PROXY_URL if set, else rotate the free pool.
    Returns HTML on a real page, None if blocked/failed. NEVER the real IP."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                         timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
        except Exception:
            return None
        return r.text if _ok(r) else None
    try:
        return _proxied_get(url).text
    except Exception:
        return None


# ---------------- URL / pagination ----------------

def _page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    q["p"] = str(page)
    return u._replace(query=urlencode(q)).geturl()


# ---------------- parsing ----------------

def _txt(node):
    return node.get_text(" ", strip=True) if node else ""


def _row_from_jsonld(j: dict, query: str) -> dict | None:
    """One row from a schema.org JobPosting object."""
    if not isinstance(j, dict):
        return None
    org = j.get("hiringOrganization") or {}
    if isinstance(org, dict):
        company = org.get("name") or ""
        logo = org.get("logo") or ""
    else:
        company, logo = str(org or ""), ""
    loc = ""
    jl = j.get("jobLocation")
    if isinstance(jl, list) and jl:
        jl = jl[0]
    if isinstance(jl, dict):
        addr = jl.get("address") or {}
        if isinstance(addr, dict):
            loc = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x)
    salary = ""
    bs = j.get("baseSalary") or {}
    if isinstance(bs, dict):
        val = bs.get("value") or {}
        if isinstance(val, dict):
            lo, hi, unit = val.get("minValue"), val.get("maxValue"), val.get("unitText")
            cur = bs.get("currency") or ""
            if lo or hi:
                salary = f"{cur} {lo or ''}-{hi or ''} {unit or ''}".strip()
    row = _blank_row()
    row["query"] = query
    row["job_title"] = j.get("title") or ""
    row["company"] = company
    row["location"] = loc
    row["salary"] = salary
    row["date_posted"] = j.get("datePosted") or ""
    row["job_url"] = j.get("url") or ""
    row["snippet"] = re.sub(r"<[^>]+>", " ", j.get("description") or "")[:500].strip()
    row["logo"] = logo
    return row if row["job_title"] else None


def _parse(html: str, query: str) -> list[dict]:
    """Jobs from JSON-LD (JobPosting) first, then visible HTML cards as a fallback."""
    out, seen = [], set()
    soup = BeautifulSoup(html, "lxml")

    # 1) schema.org JobPosting blocks (most stable when present)
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            cand = []
            if obj.get("@type") == "JobPosting":
                cand = [obj]
            elif obj.get("@type") == "ItemList":
                cand = [(el.get("item") if isinstance(el, dict) else None) for el in (obj.get("itemListElement") or [])]
            for j in cand:
                row = _row_from_jsonld(j, query) if j else None
                if row:
                    key = (row["job_title"], row["company"])
                    if key not in seen:
                        seen.add(key)
                        out.append(row)

    # 2) visible job cards (data-test attrs are the most stable selectors Glassdoor exposes)
    cards = soup.select('li[data-test="jobListing"], ul[aria-label="Jobs List"] > li, '
                        'li.react-job-listing, [data-test="job-card"]')
    for c in cards:
        a = (c.select_one('a[data-test="job-title"]') or c.select_one('a[data-test="job-link"]')
             or c.select_one('a.JobCard_jobTitle__rbjTE') or c.select_one('a[href*="/job-listing/"]')
             or c.select_one('a[href*="/Job/"]'))
        title = _txt(a) or _txt(c.select_one('[data-test="job-title"]'))
        if not title:
            continue
        row = _blank_row()
        row["query"] = query
        row["job_title"] = title
        href = a.get("href") if a else ""
        if href:
            row["job_url"] = href if href.startswith("http") else BASE + href
        row["company"] = (_txt(c.select_one('[data-test="employer-short-name"]'))
                          or _txt(c.select_one('.EmployerProfile_compactEmployerName__9MGcV'))
                          or _txt(c.select_one('[class*="EmployerProfile"] span')))
        row["company_rating"] = _txt(c.select_one('[data-test="detailRating"]'))
        row["location"] = _txt(c.select_one('[data-test="emp-location"]'))
        row["salary"] = _txt(c.select_one('[data-test="detailSalary"]'))
        row["date_posted"] = _txt(c.select_one('[data-test="job-age"]'))
        row["easy_apply"] = "Yes" if c.select_one('[data-test="easyApply"], [class*="easyApply"]') else ""
        row["snippet"] = _txt(c.select_one('[data-test="descSnippet"]'))
        img = c.select_one('img[alt][src]')
        if img:
            row["logo"] = img.get("src") or ""
        key = (row["job_title"], row["company"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    rows, page, last = [], 1, MAX_PAGES
    while page <= last:
        html = _get_text(_page_url(query, page))
        if html is None:
            break   # blocked / no proxy passed — finish quietly (pool already rotated proxies)
        page_rows = _parse(html, query)
        if not page_rows:
            break
        rows += page_rows
        if limit and len(rows) >= limit:
            break
        page += 1
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in GLASSDOOR_JOB_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    """Background task: scrape each Glassdoor search URL and store the job rows."""
    from .db import jobs, gjobs
    total = 0
    try:
        if not settings.PROXY_URL.strip():
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "glassdoor-free-pool"}})
        else:
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "glassdoor-paid-proxy"}})

        for q in queries:
            rows = await search(q, limit)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await gjobs.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
