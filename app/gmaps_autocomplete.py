"""Google Maps Autocomplete — autocomplete suggestions for Google Maps search queries.

Uses Google's own lightweight suggest endpoint (`/complete/search`, the same one the search box
calls) through the proxy pool (`yp_us` — paid PROXY_URL if set, else a free-pool proxy; the real IP
is never used). Unlike Google Search/Maps result pages, this endpoint is NOT bot-blocked, so it works
on the FREE pool. Each query line returns its suggestion list (location-biased by the proxy region /
the `region` parameter). `coordinates` (@lat,lng,zoom) is accepted for parity and passed through.
"""
import asyncio
import json
import re
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

SUGGEST_URL = "https://www.google.com/complete/search"

GMA_COLUMNS = ["query", "suggestion", "position", "coordinates"]

_LATLNG = re.compile(r"@?(-?\d+\.\d+),\s*(-?\d+\.\d+)")


def _suggestions(text: str) -> list[str]:
    """Parse the suggest payload: ["<query>", ["sug1","sug2",...], ...]. Each suggestion may be a
    bare string or a [string, ...] pair."""
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        return []
    out = []
    for s in data[1]:
        val = s[0] if isinstance(s, list) and s else s
        if isinstance(val, str) and val.strip():
            out.append(re.sub(r"</?b>", "", val).strip())   # strip bold markup if present
    return out


def search_sync(query: str, coordinates: str = "", language: str = "en", region: str = "us",
                job_id: str | None = None) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    params = {"client": "firefox", "q": q, "hl": language or "en", "gl": (region or "us").lower()}
    r = yp_us.pooled_get(SUGGEST_URL, params, timeout=15)
    if r is None or r.status_code != 200:
        raise RuntimeError("Google's suggest endpoint did not respond — the free proxy pool may be "
                           "warming up; try again (the real IP is never used).")
    rows = []
    for i, sug in enumerate(_suggestions(r.text), 1):
        rows.append({"query": q, "suggestion": sug, "position": i, "coordinates": coordinates or ""})
    return rows


async def search(query: str, coordinates: str = "", language: str = "en", region: str = "us",
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, coordinates, language, region, job_id)


async def _run(job_id: str, queries: list[str], coordinates: str, language: str, region: str,
               coll) -> None:
    """Shared loop — scrape suggestions for each query and store into `coll`. Reused by both the
    Maps Autocomplete and Search Autocomplete services (same Google suggest endpoint)."""
    from .db import jobs
    total = 0
    last_err = ""
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            try:
                rows = await search(q, coordinates, language, region, job_id)
            except Exception as qe:
                last_err = str(qe)
                rows = []
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await coll.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        done = {"status": "stopped" if stopped else "done", "total_scraped": total,
                "finished_at": datetime.utcnow()}
        if not total and not stopped:
            done["note"] = last_err or "No suggestions returned — try a different query."
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})


async def run_job(job_id: str, queries: list[str], coordinates: str, language: str,
                  region: str) -> None:
    from .db import gmaps_autocomplete
    await _run(job_id, queries, coordinates, language, region, gmaps_autocomplete)


async def run_job_search(job_id: str, queries: list[str], language: str, region: str) -> None:
    """Google Search Autocomplete — same suggest endpoint, no coordinates, stored separately."""
    from .db import gsearch_autocomplete
    await _run(job_id, queries, "", language, region, gsearch_autocomplete)
