"""Kununu Reviews Scraper — employer reviews from kununu.com (DACH region, German).

A query is a kununu company URL (e.g. https://www.kununu.com/de/mercedes-benz-group) or a company
name (resolved to /de/<slug>/kommentare). Reviews are read from the company's `/kommentare` page.

Same design as the other live scrapers: every request goes through a proxy IP (a paid PROXY_URL /
PROXY_LIST if set, else the rotating free pool — NEVER the real IP). kununu is bot-protected
(DataDome — "Human Verification"), so on free/datacenter IPs it is blocked and returns 0 until a
paid RESIDENTIAL PROXY_URL is set (same behaviour as the Trustpilot / Booking scrapers).

kununu is a Next.js app: reviews live in the `__NEXT_DATA__` JSON (props.pageProps). We extract them
from there, with JSON-LD `Review` and a DOM read as fallbacks. `sort` maps to kununu's order.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlparse, urlencode

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

BASE = "https://www.kununu.com"
MAX_PAGES = 20
_TIMEOUT = 20
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}

# UI sort label -> kununu ?sort= value ("" = kununu's default ordering)
SORT_PARAM = {
    "": "", "Date": "date", "Newest": "newest", "Oldest": "oldest",
    "Beste": "best", "Schlechteste": "worst",
}

KUNUNU_REVIEW_COLUMNS = [
    "query", "company", "position", "rating", "title", "pros", "cons", "text",
    "date", "recommended", "review_url",
]


def _blank_row():
    return {c: "" for c in KUNUNU_REVIEW_COLUMNS}


# ---------------- URL ----------------

def _company_url(query: str) -> str:
    """A kununu company URL -> its /kommentare page; a bare name -> a best-effort /de/<slug>/kommentare."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        base = q.split("?")[0].rstrip("/")
        if "/kommentare" not in base:
            base = base + "/kommentare"
        return base
    slug = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")
    return f"{BASE}/de/{slug}/kommentare"


def _page_url(base: str, page: int, sort: str) -> str:
    params = {}
    val = SORT_PARAM.get(sort or "", "")
    if val:
        params["sort"] = val
    if page > 1:
        params["page"] = str(page)
    return base + ("?" + urlencode(params) if params else "")


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    """A real kununu page (not a DataDome / "Human Verification" challenge)."""
    if r is None or r.status_code not in (200,):
        return False
    low = (r.text or "").lower()
    if any(s in low for s in ("human verification", "captcha-delivery", "datadome",
                              "just a moment", "/cdn-cgi/challenge", "access denied")):
        return False
    return "__next_data__" in low or "kununu" in low


def _get_text(url: str) -> str | None:
    """Fetch one URL through a proxy. Paid PROXY_URL if set, else rotate the free/list pool.
    Returns HTML on a real page, None if blocked/failed. NEVER the real IP."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                         timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True,
                         headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        except Exception:
            return None
        return r.text if _ok(r) else None
    # free / PROXY_LIST pool — rotate a few until one passes (datacenter IPs usually blocked)
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
                         timeout=_TIMEOUT, verify=False, allow_redirects=True,
                         headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        except Exception:
            continue
        if _ok(r):
            return r.text
    return None


# ---------------- parsing ----------------

def _company_name(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return re.split(r"\s*[|–-]\s*kununu", og["content"], flags=re.I)[0].strip()
    h1 = soup.select_one("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def _is_review_node(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    has_text = any(k in d for k in ("text", "reviewText", "summary", "title"))
    has_score = any(k in d for k in ("score", "rating", "totalScore", "ratingValue"))
    return has_text and has_score


def _num(v):
    if isinstance(v, dict):
        v = v.get("score") or v.get("value") or v.get("ratingValue") or v.get("total")
    try:
        return str(round(float(v), 2)) if v is not None and str(v).strip() != "" else ""
    except Exception:
        return str(v or "")


def _aspects(d: dict, *keys) -> str:
    """Join positive/negative aspect text (kununu reviews carry pro/contra blocks)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list):
            parts = [x.get("text") if isinstance(x, dict) else str(x) for x in v]
            parts = [p for p in parts if p]
            if parts:
                return " | ".join(parts)
    return ""


def _row_from_node(d: dict, company: str, query: str) -> dict | None:
    row = _blank_row()
    row["query"] = query
    row["company"] = company
    row["position"] = (d.get("position") or d.get("jobTitle") or d.get("authorPosition") or "")
    row["rating"] = _num(d.get("score") or d.get("rating") or d.get("totalScore") or d.get("ratingValue"))
    row["title"] = (d.get("title") or d.get("headline") or "")
    row["pros"] = _aspects(d, "positiveAspects", "positives", "pros", "good")
    row["cons"] = _aspects(d, "negativeAspects", "negatives", "cons", "bad", "suggestions")
    txt = d.get("text") or d.get("reviewText") or d.get("summary") or ""
    row["text"] = re.sub(r"<[^>]+>", " ", str(txt))[:2000].strip()
    row["date"] = (d.get("createdAt") or d.get("date") or d.get("datePublished")
                   or d.get("publishedAt") or "")
    rec = d.get("recommended")
    row["recommended"] = ("Yes" if rec else "No") if isinstance(rec, bool) else (str(rec) if rec else "")
    uid = d.get("uuid") or d.get("id") or d.get("slug")
    row["review_url"] = f"{query.split('?')[0]}#{uid}" if uid and not str(query).startswith("http") is False else ""
    return row if (row["title"] or row["text"] or row["pros"] or row["cons"]) else None


def _reviews_from_nextdata(html: str):
    """Pull review objects + company name from kununu's __NEXT_DATA__ JSON (best-effort tree walk)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return [], ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return [], ""
    company, found = "", []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if _is_review_node(cur):
                found.append(cur)
            if not company and cur.get("name") and (cur.get("profileUuid") or cur.get("companyId")
                                                    or cur.get("slug")):
                company = cur.get("name")
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found, company


def _reviews_from_jsonld(soup: BeautifulSoup, company: str, query: str) -> list[dict]:
    out = []
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            revs = [obj] if obj.get("@type") == "Review" else (obj.get("review") or [])
            for j in (revs if isinstance(revs, list) else [revs]):
                if isinstance(j, dict):
                    row = _row_from_node(j, company, query)
                    if row:
                        out.append(row)
    return out


def _parse(html: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    company = _company_name(soup)
    out, seen = [], set()

    nd_reviews, nd_company = _reviews_from_nextdata(html)
    company = nd_company or company
    for rv in nd_reviews:
        row = _row_from_node(rv, company, query)
        if row:
            key = (row["title"], (row["text"] or "")[:60], row["date"])
            if key not in seen:
                seen.add(key)
                out.append(row)

    if not out:                                   # JSON-LD fallback
        for row in _reviews_from_jsonld(soup, company, query):
            key = (row["title"], (row["text"] or "")[:60], row["date"])
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    base = _company_url(query)
    rows, page = [], 1
    while page <= MAX_PAGES:
        html = _get_text(_page_url(base, page, sort))
        if html is None:                          # blocked / no proxy passed — finish quietly
            break
        page_rows = _parse(html, query)
        if not page_rows:
            break
        rows += page_rows
        if limit and len(rows) >= limit:
            break
        page += 1
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in KUNUNU_REVIEW_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "") -> None:
    """Background task: scrape each company's kununu reviews and store the rows."""
    from .db import jobs, kununu_reviews
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, sort)
            if not rows:                          # proxies flaky — retry once
                rows = await search(q, limit, sort)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position_index"] = total + i + 1
            if rows:
                await kununu_reviews.insert_many(rows)
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
