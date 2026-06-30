"""Yahoo Search Scraper — organic web results for a query from search.yahoo.com.

Scrapes the Yahoo SERP HTML THROUGH A PROXY (real IP never used). Yahoo is far more lenient than Google
and works on the free pool. Each result: title, real URL (decoded from Yahoo's redirect), description.
Paginates with the `b` offset (10/page). One row per result.
"""
import asyncio
import re
from datetime import datetime
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

from . import yp_us
from .config import settings

YS_COLUMNS = ["query", "position", "title", "url", "description"]

_RU_RE = re.compile(r"/RU=([^/]+)/")


def _real_url(href: str) -> str:
    """Yahoo wraps result links as .../RU=<urlencoded target>/RK=... — pull the target out."""
    if not href:
        return ""
    m = _RU_RE.search(href)
    return unquote(m.group(1)) if m else href


def _parse(html: str, query: str, start: int) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for blk in soup.select("div.algo"):
        h = blk.select_one("h3")
        a = blk.select_one("h3 a") or blk.select_one("a")
        if not (h and a):
            continue
        desc = blk.select_one("div.compText") or blk.select_one("p")
        out.append({
            "query": query,
            "position": start + len(out) + 1,
            "title": h.get_text(" ", strip=True),
            "url": _real_url(a.get("href", "")),
            "description": desc.get_text(" ", strip=True) if desc else "",
        })
    return out


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One query -> ranked Yahoo results (paginated to `limit`, default all up to ~100)."""
    q = (query or "").strip()
    if not q:
        return []
    cap = limit if (limit and limit > 0) else 100
    base = q if q.lower().startswith("http") else f"https://search.yahoo.com/search?p={quote(q)}"
    rows: list[dict] = []
    for page in range(10):  # up to 10 pages
        offset = page * 10
        sep = "&" if "?" in base else "?"
        url = base + (f"{sep}b={offset + 1}" if offset else "")
        # Yahoo serves the real SERP on some free IPs and a consent/interstitial (no results) on others;
        # the pool rotates IPs per call, so retry an empty page a few times before giving up. Retry harder
        # on page 1 (a bad first IP would otherwise yield 0 for the whole query).
        tries = 4 if page == 0 else 2
        page_rows = []
        for _ in range(tries):
            try:
                r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
            except Exception:
                continue
            if r is not None and r.status_code == 200:
                page_rows = _parse(r.text, q, len(rows))
                if page_rows:
                    break
        if not page_rows:
            break
        rows += page_rows
        if len(rows) >= cap:
            break
    return rows[:cap]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, yahoo_search
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await yahoo_search.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = "Yahoo returned 0 — the free proxy may have been blocked; retry."
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
