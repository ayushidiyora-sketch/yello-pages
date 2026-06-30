"""Google Search Events Scraper — local/event listings for a query via Google's events pack (proxy-only).

Fetches Google's events results (`google.com/search?q=<query>&ibp=htl;events`) through the proxy pool
(`yp_us.pooled_get` — the paid PROXY_URL if set, else a warm free-pool proxy; the REAL IP is never
used). No API key. Each query → event rows: title, date, venue, address, link, description, thumbnail.

PROXY-ONLY: Google hard-blocks datacenter/free IPs ("unusual traffic"/CAPTCHA), so reliable results
need a paid residential PROXY_URL — same as the other Google scrapers in this project (Maps, Videos).
On the free pool it is frequently challenged and returns 0 (a clear empty), never the real IP. The
events markup is read from Google's embedded JSON-LD first, then a defensive HTML heuristic.
"""
import asyncio
import json
import re
from datetime import datetime
from html import unescape

from . import yp_us
from .scraper import STOP_REQUESTS

SEARCH = "https://www.google.com/search"

GEVENTS_COLUMNS = ["query", "title", "date", "venue", "address", "link", "description", "thumbnail"]

_TAG = re.compile(r"<[^>]+>")
_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


def _params(query: str, country: str, language: str, start: int) -> dict:
    p = {"q": query, "ibp": "htl;events", "hl": (language or "en").lower(),
         "gl": (country or "us").upper()}
    if start:
        p["start"] = start
    return p


def _strip(txt: str) -> str:
    return unescape(_TAG.sub("", txt or "")).strip()


def _event_from_ld(query: str, d: dict) -> dict | None:
    """Map a schema.org Event object (Google embeds these in ld+json) to a result row."""
    if not isinstance(d, dict) or "event" not in (d.get("@type") or "").lower():
        return None
    loc = d.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    venue = loc.get("name") if isinstance(loc, dict) else None
    addr = loc.get("address") if isinstance(loc, dict) else None
    if isinstance(addr, dict):
        addr = ", ".join(str(addr.get(k)) for k in
                         ("streetAddress", "addressLocality", "addressRegion", "postalCode")
                         if addr.get(k))
    img = d.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url")
    return {
        "query": query,
        "title": _strip(d.get("name") or ""),
        "date": (d.get("startDate") or "").strip(),
        "venue": _strip(venue or ""),
        "address": _strip(addr or ""),
        "link": (d.get("url") or "").strip(),
        "description": _strip(d.get("description") or ""),
        "thumbnail": (img or "").strip() if isinstance(img, str) else "",
    }


def _from_ld(html: str, query: str) -> list[dict]:
    """Primary parse: schema.org Event objects from the page's ld+json blocks."""
    out: list[dict] = []
    for block in _LD.findall(html or ""):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        # ld+json can nest events under @graph
        for it in list(items):
            if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                items.extend(it["@graph"])
        for it in items:
            row = _event_from_ld(query, it)
            if row and row["title"]:
                out.append(row)
    return out


def _from_heuristic(html: str, query: str) -> list[dict]:
    """Fallback parse: pull event cards from Google's embedded events JSON array. The events pack
    encodes each card as ["Title",...,"Date",...] tuples — best-effort, verifiable against a real
    proxied response. Returns whatever is confidently extractable (title + a date-like sibling)."""
    out: list[dict] = []
    seen = set()
    # event blocks carry a date phrase like "Sat, Jul 12" / "Tomorrow" near the title in the JSON
    for m in re.finditer(r'\["([^"]{4,120})",(?:[^\]]*?)"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[^"]{2,40}|'
                         r'Today[^"]{0,30}|Tomorrow[^"]{0,30})"', html or ""):
        title = _strip(m.group(1))
        date = _strip(m.group(2))
        key = (title.lower(), date.lower())
        if title and key not in seen and not title.startswith("http"):
            seen.add(key)
            out.append({"query": query, "title": title, "date": date, "venue": "",
                        "address": "", "link": "", "description": "", "thumbnail": ""})
    return out


def search_sync(query: str, pages: int = 1, country: str = "us", language: str = "en",
                job_id: str | None = None) -> list[dict]:
    pages = max(1, min(int(pages or 1), 20))
    rows: list[dict] = []
    seen = set()
    for pg in range(pages):
        if job_id and job_id in STOP_REQUESTS:
            break
        r = yp_us.pooled_get(SEARCH, _params(query, country, language, pg * 10), timeout=20)
        if r is None:
            if pg == 0:
                raise RuntimeError("No proxy available to reach Google Events (set a PROXY_URL, or "
                                   "wait for the free pool to warm up). The real IP is never used.")
            break
        page_rows = _from_ld(r.text, query) or _from_heuristic(r.text, query)
        new = 0
        for row in page_rows:
            key = (row["title"].lower(), row["date"].lower())
            if key not in seen:
                seen.add(key)
                rows.append(row)
                new += 1
        if not new:          # no fresh events on this page → stop paginating
            break
    return rows


async def search(query: str, pages: int = 1, country: str = "us", language: str = "en",
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, pages, country, language, job_id)


async def run_job(job_id: str, queries: list[str], pages: int, country: str, language: str) -> None:
    from .db import jobs, gevents_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:               # Stop button pressed
                break
            rows = await search(q, pages, country, language, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gevents_results.insert_many(rows)
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
