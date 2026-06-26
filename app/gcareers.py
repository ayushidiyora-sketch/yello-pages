"""Google Search Careers Scraper — jobs from a careers.google.com search (proxy-only).

Google Careers (careers.google.com/jobs/results/) is a boq/BOQ single-page app with no clean REST
API, but it server-embeds the full results set as a JSON blob in an `AF_initDataCallback({key:'ds:1'
…})` script on the page. We fetch the results URL through the proxy pool (`yp_us.pooled_get` — the
paid PROXY_URL if set, else a warm free-pool proxy; the REAL IP is never used), pull that `ds:1`
array, and read each job (title, company, location, apply URL, qualifications, description).

Input is a careers.google.com/jobs/results/ search URL (its query params — location/distance/
has_remote/q — are passed straight through); a bare keyword becomes a `q=<keyword>` search. Paginates
with the page's `page` param (~20 jobs/page); "Pages limit per one query" caps how many pages.
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlencode, urlparse, parse_qsl

from . import yp_us
from .scraper import STOP_REQUESTS

RESULTS = "https://careers.google.com/jobs/results/"

GCAREERS_COLUMNS = ["query", "title", "company", "location", "apply_url", "posted",
                    "qualifications", "responsibilities", "description", "job_id"]

_TAG = re.compile(r"<[^>]+>")


def _strip(node) -> str:
    """ds:1 rich-text fields are [null, "<html>"] pairs; pull the text out of the second element."""
    if isinstance(node, list):
        node = next((x for x in node if isinstance(x, str)), "")
    return unescape(_TAG.sub(" ", node or "")).replace("\xa0", " ").strip()


def _to_results_url(q: str) -> str:
    """A careers.google.com results URL as-is; a bare keyword -> a ?q=<keyword> search."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        return q
    return RESULTS + "?" + urlencode({"q": q})


def _params(url: str, page: int) -> dict:
    """The URL's own query params + the 1-based page number."""
    params = dict(parse_qsl(urlparse(url).query))
    params["page"] = str(page)
    return params


def _extract_ds1(html: str):
    """Return the parsed `data:[…]` array from the page's AF_initDataCallback ds:1 block, or None."""
    i = html.find("key: 'ds:1'")
    if i < 0:
        return None
    j = html.find("data:", i)
    if j < 0:
        return None
    depth, start = 0, None
    for k in range(j + len("data:"), len(html)):
        c = html[k]
        if c == "[":
            if start is None:
                start = k
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:k + 1])
                except Exception:
                    return None
    return None


def _location(job: list) -> str:
    """Join the distinct display locations from job[9] (a list of [display, [addr…], …] entries)."""
    out = []
    locs = job[9] if len(job) > 9 and isinstance(job[9], list) else []
    for entry in locs:
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            if entry[0] not in out:
                out.append(entry[0])
        elif isinstance(entry, str) and entry not in out:
            out.append(entry)
    return " | ".join(out)


def _posted(job: list) -> str:
    """Published time from job[13] ([epoch_seconds, nanos]) -> ISO date (best-effort)."""
    for idx in (13, 12, 14):
        if len(job) > idx and isinstance(job[idx], list) and job[idx] and isinstance(job[idx][0], (int, float)):
            try:
                return datetime.fromtimestamp(int(job[idx][0]), tz=timezone.utc).date().isoformat()
            except Exception:
                pass
    return ""


def _row(query: str, job: list) -> dict:
    g = lambda i: job[i] if len(job) > i else None
    return {
        "query": query,
        "title": (g(1) or "").strip() if isinstance(g(1), str) else "",
        "company": (g(7) or "").strip() if isinstance(g(7), str) else "",
        "location": _location(job),
        "apply_url": (g(2) or "").strip() if isinstance(g(2), str) else "",
        "posted": _posted(job),
        "qualifications": _strip(g(4)),
        "responsibilities": _strip(g(3)),
        "description": _strip(g(10)),
        "job_id": (g(0) or "").strip() if isinstance(g(0), str) else "",
    }


_MAX_PAGES = 50   # safety cap when fetching "all" (~20 jobs/page)


def search_sync(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    """Return up to `limit` jobs for the query (None/0 = all). Pages the careers results internally
    (~20 jobs/page) until it has `limit` rows or runs out of pages."""
    cap = limit if (limit and limit > 0) else None
    url = _to_results_url(query)
    rows: list[dict] = []
    seen = set()
    for pg in range(1, _MAX_PAGES + 1):
        if job_id and job_id in STOP_REQUESTS:
            break
        r = yp_us.pooled_get(url.split("?")[0], _params(url, pg), timeout=25)
        if r is None:
            if pg == 1:
                raise RuntimeError("No proxy available to reach Google Careers (set a PROXY_URL, or "
                                   "wait for the free pool to warm up). The real IP is never used.")
            break
        arr = _extract_ds1(r.text)
        jobs = arr[0] if isinstance(arr, list) and arr and isinstance(arr[0], list) else []
        new = 0
        for job in jobs:
            if not isinstance(job, list) or not job:
                continue
            jid = job[0] if isinstance(job[0], str) else None
            if jid and jid in seen:
                continue
            if jid:
                seen.add(jid)
            row = _row(query, job)
            if row["title"]:
                rows.append(row)
                new += 1
        if not new:                       # no fresh jobs on this page → last page reached
            break
        if cap and len(rows) >= cap:       # collected enough rows → stop paging
            break
    return rows[:cap] if cap else rows


async def search(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from .db import jobs, gcareers_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:               # Stop button pressed
                break
            rows = await search(q, limit, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gcareers_results.insert_many(rows)
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
