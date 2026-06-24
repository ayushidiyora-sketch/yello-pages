"""Google Shopping Reviews Scraper — reviews for a list of Google Shopping products.

Input = a Google Shopping product link or product id (the long number, e.g. 7016166685587850095).
Reads Google's own internal data (the `AF_initDataCallback({…data:[…]})` payload its scripts consume)
from the product's reviews page — NOT the rendered HTML DOM — the same approach as the Search
Shopping / Jobs / Videos scrapers. DOM is only a fallback. PROXY-ONLY, paid/residential REQUIRED
(Google blocks free/datacenter IPs): set PROXY_URL in .env; the free pool returns a clear "blocked"
error (the real IP is never used). Review field-mapping is best-effort and finalizes against a real
proxied response.
"""
import asyncio
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .gsjobs import _balanced_array          # string-aware nested-array JSON parser (reused)
from .scraper import STOP_REQUESTS

PRODUCT_URL = "https://www.google.com/shopping/product/{pid}/reviews"

GSR_COLUMNS = ["product_id", "author", "rating", "date", "title", "review", "source"]

_BLOCK = ("unusual traffic", "/sorry/", "captcha", "recaptcha", "before you continue",
          "enablejs", "not a robot")
_AF_DATA = re.compile(r"AF_initDataCallback\(\{[^{}]*?data:\s*(\[)", re.DOTALL)
_URLISH = re.compile(r"^https?://")
_RATING = re.compile(r"^[0-5](?:\.\d)?$")
_PID = re.compile(r"(\d{8,})")           # Google Shopping product ids are long digit strings
_DATE_RE = re.compile(r"\b(?:19|20)\d{2}\b|\bago\b|"
                      r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\b", re.I)


def _product_id(s: str) -> str:
    s = (s or "").strip()
    m = _PID.search(s)
    return m.group(1) if m else ""


def _flat_strings(node, out, depth=0):
    if depth > 8:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            _flat_strings(x, out, depth + 1)


def _collect_reviews(node, rows, seen, product_id, depth=0):
    """A review ENTRY is a list whose direct children include a long free-text string (the review
    body) plus a 1–5 rating (an int child, or a '0–5' string in the subtree). Best-effort — finalize
    the indices against a real proxied response."""
    if depth > 60 or not isinstance(node, list):
        return
    direct_str = [x for x in node if isinstance(x, str)]
    texts = [x for x in direct_str if len(x.strip()) >= 15 and " " in x and not _URLISH.match(x)]
    if texts:
        body = max(texts, key=len)
        key = body[:120]
        if key not in seen:
            seen.add(key)
            sub = []
            _flat_strings(node, sub)
            rating = next((x for x in node if isinstance(x, (int, float)) and 1 <= x <= 5), "")
            if rating == "":
                rating = next((x for x in sub if _RATING.match(x.strip())), "")
            # reviewer name: a short string with no digits (review ids like "gp:AOq…"/"rid1" have
            # digits/colons, so this skips them) and not a date/URL.
            author = next((x for x in direct_str if x != body and 2 <= len(x) <= 40
                           and not _URLISH.match(x) and not _DATE_RE.search(x)
                           and not any(c.isdigit() for c in x) and ":" not in x), "")
            date = next((x for x in sub if _DATE_RE.search(x)), "")
            # optional review headline: a multi-word string that isn't the body/author/an id.
            title = next((x for x in direct_str if x not in (body, author) and " " in x
                          and 3 <= len(x) <= 100 and not _URLISH.match(x)
                          and not _DATE_RE.search(x)), "")
            rows.append({"product_id": product_id, "author": author,
                         "rating": rating, "date": date, "title": title,
                         "review": body, "source": ""})
        return
    for x in node:
        _collect_reviews(x, rows, seen, product_id, depth + 1)


def _internal_reviews(html_text: str, product_id: str) -> list[dict]:
    rows, seen = [], set()
    for m in _AF_DATA.finditer(html_text):
        arr = _balanced_array(html_text, m.start(1))
        if arr is not None:
            _collect_reviews(arr, rows, seen, product_id)
    return rows


def _dom_reviews(soup, product_id: str) -> list[dict]:
    """Fallback: read review cards from the rendered HTML (selectors obfuscated — finalize live)."""
    rows, seen = [], set()
    for c in soup.select("div.z6XoBf, div.fJfMrb, div[data-reviewid]"):
        body_el = c.select_one("div.g1lvWe, div.GsNJValue, p")
        body = body_el.get_text(" ", strip=True) if body_el else ""
        if not body or body in seen:
            continue
        seen.add(body)
        author_el = c.select_one("div.Vpc5Fe, span.author, div.Mz0Q4")
        rows.append({"product_id": product_id,
                     "author": author_el.get_text(strip=True) if author_el else "",
                     "rating": "", "date": "", "title": "",
                     "review": body, "source": ""})
    return rows


def search_sync(query: str, limit: int | None = None, language: str = "en", region: str = "us",
                job_id: str | None = None) -> list[dict]:
    pid = _product_id(query)
    if not pid:
        raise RuntimeError(f"Could not read a Google Shopping product id from '{query}' "
                           "(use the product link or the long numeric id).")
    headers = {"Accept-Language": f"{(language or 'en')}-{(region or 'us').upper()},"
                                  f"{(language or 'en')};q=0.9"}
    params = {"hl": language or "en", "gl": (region or "us").lower()}
    r = yp_us.pooled_get(PRODUCT_URL.format(pid=pid), params, timeout=20, headers=headers)
    if r is None or r.status_code != 200:
        raise RuntimeError("Google Shopping needs a paid residential PROXY_URL in .env — Google "
                           "blocks free/datacenter IPs (no real IP is used).")
    low = (r.text or "").lower()
    if any(b in low for b in _BLOCK):
        raise RuntimeError("Google blocked this request (CAPTCHA / unusual traffic). Use a cleaner "
                           "residential PROXY_URL.")
    rows = _internal_reviews(r.text, pid)               # PRIMARY: Google's internal review data
    if not rows:
        rows = _dom_reviews(BeautifulSoup(r.text, "lxml"), pid)   # fallback: rendered DOM cards
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, language: str = "en", region: str = "us",
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, language, region, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None, language: str,
                  region: str) -> None:
    from .db import jobs, gsreviews_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, language, region, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gsreviews_results.insert_many(rows)
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
