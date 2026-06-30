"""OLX Scraper — product/classified listings from an olx.* search URL (free internal API, proxy-only).

OLX exposes its own keyless JSON API (`/api/v1/offers/`) — the same one its app uses — so we hit that
directly instead of scraping the search-page DOM. Each query is an olx.* search URL; the search term
is taken from `?q=` or the `/q-<term>/` path segment, other query params pass through, and results are
paged via `offset`/`limit`. Returns title, price, location, date, url, image, and a details line built
from the listing's key params (e.g. year / mileage).

PROXY-ONLY: every request goes through the proxy pool (`yp_us.pooled_get` — a paid PROXY_URL if set,
else the free US pool); the real IP is never used. OLX isn't bot-walled, so the free pool works.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, unquote

from . import yp_us
from .scraper import STOP_REQUESTS

OLX_COLUMNS = ["query", "title", "price", "location", "date", "url", "image", "details"]
_PER = 40


def _api_target(url: str):
    """(api_base, query, passthrough_params) from an olx.* search URL."""
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}" if u.scheme else "https://www.olx.ro"
    params = dict(parse_qsl(u.query))
    query = params.pop("q", "")
    if not query:                                          # OLX puts the term in the path: /q-<term>/
        m = re.search(r"/q-([^/]+)", u.path)
        if m:
            query = unquote(m.group(1)).replace("-", " ")
    return base, query, params


def _label(o: dict, key: str) -> str:
    for p in o.get("params") or []:
        if p.get("key") == key:
            v = p.get("value")
            if isinstance(v, dict):
                return v.get("label") or v.get("key") or ""
            return str(v) if v is not None else ""
    return ""


def _row(query: str, o: dict) -> dict:
    loc = o.get("location") or {}
    city = (loc.get("city") or {}).get("name") or ""
    region = (loc.get("region") or {}).get("name") or ""
    photos = o.get("photos") or []
    img = (photos[0].get("link") if photos and isinstance(photos[0], dict) else "") or ""
    img = img.replace("{width}", "800").replace("{height}", "600")
    details = " · ".join(x for x in (_label(o, k) for k in (o.get("key_params") or [])) if x)
    return {
        "query": query,
        "title": o.get("title") or "",
        "price": _label(o, "price"),
        "location": ", ".join(x for x in (city, region) if x),
        "date": (o.get("created_time") or "")[:10],
        "url": o.get("url") or "",
        "image": img,
        "details": details,
    }


def search_sync(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    base, q, extra = _api_target(query)
    api = base + "/api/v1/offers/"
    rows, seen = [], set()
    offset = 0
    for _ in range(60):                                    # safety cap (~2400 listings)
        if job_id and job_id in STOP_REQUESTS:
            break
        params = {"offset": str(offset), "limit": str(_PER), **extra}
        if q:
            params["query"] = q
        r = yp_us.pooled_get(api, params, timeout=20)
        if r is None:
            if offset == 0:
                raise RuntimeError("No proxy available to reach OLX (set a PROXY_URL, or wait for the "
                                   "free pool to warm up). The real IP is never used.")
            break
        try:
            data = json.loads(r.text).get("data") or []
        except Exception:
            break
        if not data:
            break
        new = 0
        for o in data:
            if not isinstance(o, dict):
                continue
            oid = o.get("id") or o.get("url")
            if oid in seen:
                continue
            seen.add(oid)
            rows.append(_row(query, o))
            new += 1
        if not new:
            break
        offset += _PER
        if limit and len(rows) >= limit:
            break
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, olx_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await olx_results.insert_many(rows)
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
