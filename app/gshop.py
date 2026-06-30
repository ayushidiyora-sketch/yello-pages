"""Google Search Shopping Scraper — product results for a query via Google Shopping (tbm=shop).

Reads Google's own internal data (the `AF_initDataCallback({…data:[…]})` payload its scripts consume)
— NOT the rendered HTML DOM — same approach as the Jobs / Videos scrapers. DOM is only a fallback.
PROXY-ONLY, paid/residential REQUIRED (Google blocks free/datacenter IPs): set PROXY_URL in .env;
the free pool returns a clear "blocked" error (the real IP is never used). The product field-mapping
is best-effort and finalizes against a real proxied response.
"""
import asyncio
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from . import yp_us
from .gsjobs import _balanced_array          # string-aware nested-array JSON parser (reused)
from .scraper import STOP_REQUESTS

GOOGLE_S = "https://www.google.com/search"

GSH_COLUMNS = ["query", "title", "price", "store", "rating", "reviews", "link", "thumbnail"]

_BLOCK = ("unusual traffic", "/sorry/", "captcha", "recaptcha", "before you continue",
          "enablejs", "not a robot")
_AF_DATA = re.compile(r"AF_initDataCallback\(\{[^{}]*?data:\s*(\[)", re.DOTALL)
_URLISH = re.compile(r"^https?://")
_PRICE = re.compile(r"^(?:[$₹€£¥]|Rs\.?|US\$)\s?\d[\d,]*(?:\.\d{1,2})?$")
_RATING = re.compile(r"^[0-5](?:\.\d)?$")
_THUMB_HOST = ("gstatic.com", "googleusercontent.com", "ggpht.com", "encrypted-tbn")


def _host(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").replace("www.", "")
    except Exception:
        return ""


def _flat_strings(node, out, depth=0):
    if depth > 8:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            _flat_strings(x, out, depth + 1)


def _collect_products(node, rows, seen, query, depth=0):
    """A product ENTRY is a list with >=2 direct string children whose subtree holds a price string
    (and a title). Best-effort — finalize indices on a real proxied response."""
    if depth > 60 or not isinstance(node, list):
        return
    direct = [x for x in node if isinstance(x, str)]
    if len(direct) >= 2:
        sub = []
        _flat_strings(node, sub)
        price = next((x for x in sub if _PRICE.match(x.strip())), "")
        titles = [x for x in direct if 4 <= len(x) <= 200 and not _URLISH.match(x)
                  and not _PRICE.match(x.strip()) and " " in x]
        if price and titles:
            title = max(titles, key=len)
            key = title[:100]
            if key not in seen:
                seen.add(key)
                link = next((x for x in sub if _URLISH.match(x)
                             and not any(t in x for t in _THUMB_HOST)), "")
                thumb = next((x for x in sub if _URLISH.match(x)
                              and any(t in x for t in _THUMB_HOST)), "")
                rating = next((x for x in sub if _RATING.match(x.strip())), "")
                store = next((x for x in direct if x != title and 1 < len(x) <= 60
                              and not _PRICE.match(x.strip()) and not _RATING.match(x.strip())
                              and not _URLISH.match(x)), "")
                rows.append({"query": query, "title": title, "price": price.strip(),
                             "store": store, "rating": rating, "reviews": "",
                             "link": link, "thumbnail": thumb})
            return
    for x in node:
        _collect_products(x, rows, seen, query, depth + 1)


def _internal_products(html_text: str, query: str) -> list[dict]:
    rows, seen = [], set()
    for m in _AF_DATA.finditer(html_text):
        arr = _balanced_array(html_text, m.start(1))
        if arr is not None:
            _collect_products(arr, rows, seen, query)
    return rows


def _dom_products(soup, query: str) -> list[dict]:
    """Fallback: read Shopping cards from the rendered HTML (selectors obfuscated — finalize live)."""
    rows, seen = [], set()
    for c in soup.select("div.sh-dgr__content, div.i0X6df, div.KZmu8e"):
        t = c.select_one("h3, div.tAxDx, div.A2sOrd")
        if not t:
            continue
        title = t.get_text(" ", strip=True)
        if not title or title in seen:
            continue
        seen.add(title)
        price_el = c.select_one("span.a8Pemb, span.kHxwFf, b")
        a = c.select_one("a[href]")
        rows.append({"query": query, "title": title,
                     "price": price_el.get_text(strip=True) if price_el else "",
                     "store": (c.select_one("div.aULzUe, div.IuHnof").get_text(strip=True)
                               if c.select_one("div.aULzUe, div.IuHnof") else ""),
                     "rating": "", "reviews": "",
                     "link": (a["href"] if a else ""), "thumbnail": ""})
    return rows


def search_sync(query: str, limit: int | None = None, language: str = "en", region: str = "us",
                job_id: str | None = None) -> list[dict]:
    headers = {"Accept-Language": f"{(language or 'en')}-{(region or 'us').upper()},"
                                  f"{(language or 'en')};q=0.9"}
    rows, seen = [], set()
    for start in range(0, 100, 20):
        if job_id and job_id in STOP_REQUESTS:
            break
        params = {"q": query, "tbm": "shop", "num": "40", "start": str(start),
                  "hl": language or "en", "gl": (region or "us").lower()}
        r = yp_us.pooled_get(GOOGLE_S, params, timeout=20, headers=headers)
        if r is None or r.status_code != 200:
            if rows:
                break
            raise RuntimeError("Google Shopping needs a paid residential PROXY_URL in .env — Google "
                               "blocks free/datacenter IPs (no real IP is used).")
        low = (r.text or "").lower()
        if any(b in low for b in _BLOCK):
            if rows:
                break
            raise RuntimeError("Google blocked this request (CAPTCHA / unusual traffic). Use a "
                               "cleaner residential PROXY_URL.")
        # PRIMARY: Google's internal shopping data; fallback: rendered DOM cards
        batch = _internal_products(r.text, query)
        if not batch:
            batch = _dom_products(BeautifulSoup(r.text, "lxml"), query)
        new = [p for p in batch if p["title"] not in seen]
        for p in new:
            seen.add(p["title"])
            rows.append(p)
            if limit and len(rows) >= limit:
                return rows
        if not new:
            break
    return rows


async def search(query: str, limit: int | None = None, language: str = "en", region: str = "us",
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, language, region, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None, language: str,
                  region: str) -> None:
    from .db import jobs, gshop_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, language, region, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gshop_results.insert_many(rows)
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
