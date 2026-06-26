"""Glassdoor Company Jobs Scraper — every open job for one company on glassdoor.com.

A query is a company URL — either the company Jobs page
    https://www.glassdoor.com/Jobs/USA-Jobs-Jobs-E221792.htm
or the company Overview page
    https://www.glassdoor.com/Overview/Working-at-USA-Jobs-EI_IE221792.11,19.htm
(both carry the employer id, which is all we need). We normalise it to the company Jobs URL and page
through it, returning one row per job. The Sorting option is "Most relevant" (Glassdoor's order) or
"Newest First" (we request the date sort AND re-order the parsed rows by recency, so the order holds
even if Glassdoor ignores the param).

Same design as the other live scrapers: requests go through a proxy IP (a paid PROXY_URL if set, else
the rotating free pool — NEVER the real IP). Glassdoor is Cloudflare-protected, so on the free pool it
is often blocked and returns 0 until a paid PROXY_URL is set (same as the Glassdoor Job Scraper, whose
proxy + parsing code this reuses).
"""
import asyncio
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import settings
# reuse the Glassdoor Job Scraper's proxy fetch + job parsing (identical page structure)
from .glassdoor_jobs import BASE, _get_text, _parse, _txt, GLASSDOOR_JOB_COLUMNS

GLASSDOOR_COMPANY_JOB_COLUMNS = GLASSDOOR_JOB_COLUMNS
MAX_PAGES = 10


# ---------------- URL normalisation ----------------

def _jobs_url(query: str) -> str:
    """Normalise any company URL to its company Jobs page (…-Jobs-E<id>.htm)."""
    q = (query or "").strip()
    if re.search(r"/Jobs/.+-Jobs-E\d+", q):                  # already a company Jobs URL
        return q.split("?")[0]
    m = re.search(r"Working-at-(.+?)-EI_IE(\d+)", q)         # Overview URL -> build Jobs URL
    if m:
        return f"{BASE}/Jobs/{m.group(1)}-Jobs-E{m.group(2)}.htm"
    m = re.search(r"[-_]E(\d+)\b", q)                         # any URL carrying an employer id
    if m:
        slug = "Company"
        s = re.search(r"/(?:Jobs|Overview|Reviews)/(?:Working-at-)?(.+?)-(?:Jobs|EI|E|Reviews)", q)
        if s:
            slug = s.group(1)
        return f"{BASE}/Jobs/{slug}-Jobs-E{m.group(1)}.htm"
    return q


def _page_url(url: str, page: int) -> str:
    """Glassdoor company-jobs pagination inserts _IP<n> before .htm (keeps any ?sortBy query)."""
    if page <= 1:
        return url
    u = urlparse(url)
    path = re.sub(r"\.htm$", f"_IP{page}.htm", u.path)
    return u._replace(path=path).geturl()


# ---------------- company name + recency ----------------

def _company_name(soup: BeautifulSoup) -> str:
    h1 = _txt(soup.select_one("h1"))
    if h1:
        return re.sub(r"\s+Jobs\s*$", "", h1).strip()
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return re.split(r"\s+Jobs\b", og["content"])[0].strip()
    return ""


def _age_hours(s: str) -> int:
    """Relative posted-age -> hours, for the Newest-First sort. Unknown sorts last."""
    s = (s or "").lower()
    if "just" in s or "today" in s or "hour" in s:
        if (m := re.search(r"(\d+)\s*h", s)):
            return int(m.group(1))
        return 0
    m = re.search(r"(\d+)\s*([hd])", s)
    if m:
        n = int(m.group(1))
        return n if m.group(2) == "h" else n * 24
    return 10 ** 9


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None, sort: str = "relevant") -> list[dict]:
    base = _jobs_url(query)
    newest = (sort or "").lower().startswith("new")
    if newest:
        base = base + ("&" if "?" in base else "?") + "sortBy=date_desc"
    rows, seen, page = [], set(), 1
    while page <= MAX_PAGES:
        html = _get_text(_page_url(base, page))
        if html is None:                       # blocked / no proxy passed — finish quietly
            break
        soup = BeautifulSoup(html, "lxml")
        company = _company_name(soup)
        page_rows = _parse(html, query)
        new = 0
        for r in page_rows:
            if not r.get("company") and company:
                r["company"] = company
            key = (r["job_title"], r.get("job_url") or r.get("location"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
            new += 1
        if not new:
            break
        if limit and len(rows) >= limit:
            break
        page += 1
    if newest:                                 # guarantee order regardless of Glassdoor's param
        rows.sort(key=lambda r: _age_hours(r.get("date_posted")))
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "relevant") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in GLASSDOOR_COMPANY_JOB_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "relevant") -> None:
    """Background task: scrape each company's jobs and store one row per job."""
    from .db import jobs, glassdoor_company_jobs_results as gcj
    from .scraper import STOP_REQUESTS
    total = 0
    try:
        mode = "glassdoor-paid-proxy" if settings.PROXY_URL.strip() else "glassdoor-free-pool"
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": mode}})

        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, sort)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit, sort)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await gcj.insert_many(rows)
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
