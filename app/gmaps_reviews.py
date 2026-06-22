"""Google Maps Reviews Scraper — reviews from Google Maps places via SerpApi.

Google blocks DIY review scraping (the internal RPC needs an un-buildable protobuf), so this uses
**SerpApi** (serpapi.com) `engine=google_maps_reviews` — reliable, all reviews, sort + pagination.
Set SERPAPI_KEY in .env (free plan: 250 searches/month; each reviews page ≈ 1 search). The input may
be a Google place_id (ChIJ..), a feature/data id (0x..:0x..), a Google Maps / local-reviews URL, or a
plain "category, city" query (resolved to a place via SerpApi google_maps first).
"""
import asyncio
import re
from datetime import datetime

import httpx

from .config import settings

SERP = "https://serpapi.com/search.json"

GMR_COLUMNS = ["query", "place_name", "place_id", "reviewer", "rating", "date",
               "review", "owner_response", "likes", "language"]

# our sort -> SerpApi google_maps_reviews sort_by
_SORT = {"newest": "newestFirst", "relevant": "qualityScore", "most_relevant": "qualityScore",
         "highest": "ratingHigh", "lowest": "ratingLow"}

_FID = re.compile(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
_PID = re.compile(r"(ChIJ[A-Za-z0-9_\-]{10,})")

_CATEGORIES = None


def categories() -> list[str]:
    """The Google Maps category list (from app/categories.xlsx), cached, popularity-ordered."""
    global _CATEGORIES
    if _CATEGORIES is None:
        import os
        import openpyxl
        path = os.path.join(os.path.dirname(__file__), "categories.xlsx")
        out = []
        try:
            ws = openpyxl.load_workbook(path, read_only=True).active
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                if row and row[0] and str(row[0]).strip():
                    out.append(str(row[0]).strip())
        except Exception:
            out = []
        _CATEGORIES = out
    return _CATEGORIES


def _feature_id(q: str) -> str | None:
    m = _FID.search(q or "")
    return m.group(1) if m else None


def _place_id(q: str) -> str | None:
    m = _PID.search(q or "")
    return m.group(1) if m else None


async def _serp_get(params: dict) -> dict:
    params = {**params, "api_key": settings.SERPAPI_KEY.strip()}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True, trust_env=False) as c:
        r = await c.get(SERP, params=params)
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"SerpApi returned a non-JSON response (HTTP {r.status_code}).")


async def _place_ref(query: str, language: str) -> dict:
    """Turn any input into the SerpApi place kwarg: {'place_id': ..} (ChIJ..) or {'data_id': ..}
    (0x..:0x..). A plain text query is resolved to a place via engine=google_maps."""
    pid = _place_id(query)
    if pid:
        return {"place_id": pid}
    fid = _feature_id(query)
    if fid:
        return {"data_id": fid}
    # plain "category, city" query -> find the top matching place
    d = await _serp_get({"engine": "google_maps", "q": query.strip(), "type": "search",
                         "hl": language or "en"})
    if d.get("error"):
        raise RuntimeError(f"SerpApi place search failed: {d['error']}")
    place = d.get("place_results") or {}
    if place.get("place_id"):
        return {"place_id": place["place_id"]}
    locals_ = d.get("local_results") or []
    if locals_ and isinstance(locals_, list) and locals_[0].get("place_id"):
        return {"place_id": locals_[0]["place_id"]}
    raise RuntimeError(f"No Google Maps place found for '{query}'.")


def _row(rv: dict, query: str, place_name: str, place_id: str) -> dict:
    user = rv.get("user") or {}
    resp = rv.get("response") or {}
    return {
        "query": query,
        "place_name": place_name,
        "place_id": place_id,
        "reviewer": user.get("name") or rv.get("reviewer") or "",
        "rating": str(rv.get("rating") or ""),
        "date": rv.get("date") or rv.get("iso_date") or "",
        "review": rv.get("snippet") or rv.get("extracted_snippet") or "",
        "owner_response": (resp.get("snippet") if isinstance(resp, dict) else "") or "",
        "likes": str(rv.get("likes") or ""),
        "language": rv.get("iso_language_code") or "",
    }


async def search(query: str, sort: str, limit: int | None, language: str) -> list[dict]:
    if not settings.SERPAPI_KEY.strip():
        raise RuntimeError("Google Maps reviews need a SerpApi key — set SERPAPI_KEY in .env "
                           "(free 250 searches/month at serpapi.com).")
    ref = await _place_ref(query, language)
    sort_by = _SORT.get((sort or "newest").lower(), "newestFirst")
    rows: list[dict] = []
    place_name, place_id, token = "", ref.get("place_id", ""), None
    for _ in range(100):                       # hard page cap (safety)
        params = {"engine": "google_maps_reviews", "hl": language or "en",
                  "sort_by": sort_by, **ref}
        if token:
            params["next_page_token"] = token
        d = await _serp_get(params)
        if d.get("error"):
            if rows:
                break
            raise RuntimeError(f"SerpApi: {d['error']}")
        pinfo = d.get("place_info") or {}
        place_name = place_name or pinfo.get("title") or ""
        place_id = place_id or pinfo.get("place_id") or ""
        batch = d.get("reviews") or []
        if not batch:
            break
        for rv in batch:
            rows.append(_row(rv, query, place_name, place_id))
        if limit and len(rows) >= limit:
            break
        token = ((d.get("serpapi_pagination") or {}).get("next_page_token"))
        if not token:
            break
    return rows[:limit] if limit else rows


async def run_job(job_id: str, queries: list[str], sort: str, limit: int | None,
                  language: str) -> None:
    from .db import jobs, gmaps_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, sort, limit, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gmaps_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
