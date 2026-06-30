"""1688 Search Scraper — wholesale product offers from 1688.com (Alibaba, China).

A query is a 1688 search URL (s.1688.com/selloffer/offer_search.htm?keywords=…) or a bare keyword
(built into that search URL). Every request goes through a proxy IP (paid PROXY_URL / PROXY_LIST if
set, else the rotating free pool — NEVER the real IP).

1688 is protected by Alibaba's anti-bot system (x5sec / "punish" / nocaptcha slider) and is a China
site, so on free/datacenter IPs it returns the challenge page and yields 0 until a paid RESIDENTIAL
(ideally China-exit) PROXY_URL is set — same behaviour as the other bot-protected scrapers. Offer
data is embedded in a `window.__*` JSON blob; we walk it for offer nodes (subject/title + price).
`limit` caps offers per query.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlencode, quote

from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

BASE = "https://s.1688.com/selloffer/offer_search.htm"
_TIMEOUT = 20
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}
_BLOCK = ("x5sec", "captcha", "punish", "nocaptcha", "baxia", "滑块", "/_____tmd_____/")

S1688_COLUMNS = [
    "query", "title", "price", "min_order", "company", "location", "sales", "offer_url", "image",
]


def _search_url(query: str) -> str:
    """A 1688 search URL as-is; a bare keyword -> the offer_search URL."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q
    return BASE + "?" + urlencode({"keywords": q})


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    if r is None or r.status_code != 200 or len(r.text or "") < 20000:
        return False
    low = r.text.lower()
    return not any(s in low for s in _BLOCK)


def _get_text(url: str) -> str | None:
    """Fetch through a proxy. Paid PROXY_URL if set, else rotate the free/list pool. NEVER real IP."""
    headers = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    proxy = settings.PROXY_URL.strip()
    if proxy:
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                         timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True,
                         headers=headers)
        except Exception:
            return None
        return r.text if _ok(r) else None
    try:
        yp_us.ensure_pool(_SEED, 6)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        candidates = warm + yp_us._fetch_candidates()
    except Exception:
        candidates = []
    for px in candidates[:8]:
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                         timeout=_TIMEOUT, verify=False, allow_redirects=True, headers=headers)
        except Exception:
            continue
        if _ok(r):
            return r.text
    return None


# ---------------- parsing ----------------

def _is_offer(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    has_name = any(k in d for k in ("subject", "title", "name", "subjectTrans"))
    has_price = any(k in d for k in ("price", "priceInfo", "showPrice", "unitPrice"))
    return has_name and has_price


def _price(d: dict) -> str:
    for k in ("price", "showPrice", "unitPrice"):
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v)
    pi = d.get("priceInfo")
    if isinstance(pi, dict):
        return str(pi.get("price") or pi.get("value") or "")
    return ""


def _img(d: dict) -> str:
    for k in ("image", "imageUrl", "picUrl", "img"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v if v.startswith("http") else "https:" + v
        if isinstance(v, dict):
            u = v.get("url") or v.get("uri")
            if u:
                return u if u.startswith("http") else "https:" + u
    return ""


def _row(d: dict, query: str) -> dict | None:
    oid = d.get("offerId") or d.get("id") or ""
    company = d.get("companyName") or d.get("company") or ""
    if isinstance(company, dict):
        company = company.get("name") or ""
    row = {c: "" for c in S1688_COLUMNS}
    row.update({
        "query": query,
        "title": d.get("subjectTrans") or d.get("subject") or d.get("title") or d.get("name") or "",
        "price": _price(d),
        "min_order": str(d.get("minOrderQuantity") or d.get("quantityBegin") or ""),
        "company": company,
        "location": d.get("city") or d.get("province") or d.get("location") or "",
        "sales": str(d.get("saleQuantity") or d.get("soldCount") or d.get("sales") or ""),
        "offer_url": (d.get("detailUrl") or (f"https://detail.1688.com/offer/{oid}.html" if oid else "")),
        "image": _img(d),
    })
    return row if row["title"] else None


def _json_blobs(html: str):
    """Yield parsed JSON from `window.X = {...};` assignments and <script type=application/json>."""
    for m in re.finditer(r"window\.[A-Za-z_$][\w$.]*\s*=\s*(\{.*?\})\s*;", html or "", re.S):
        try:
            yield json.loads(m.group(1))
        except Exception:
            continue
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html or "", re.S):
        try:
            yield json.loads(m.group(1))
        except Exception:
            continue


def _parse(html: str, query: str) -> list[dict]:
    out, seen = [], set()
    for data in _json_blobs(html):
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if _is_offer(cur):
                    row = _row(cur, query)
                    if row:
                        key = (row["title"], row["price"], row["company"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    html = _get_text(_search_url(query))
    if html is None:
        return []
    rows = _parse(html, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in S1688_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each 1688 search and store one row per offer."""
    from .db import jobs, s1688_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # proxies flaky — retry once
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await s1688_results.insert_many(rows)
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
