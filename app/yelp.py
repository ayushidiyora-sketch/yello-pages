"""Y.E.L.P Businesses Scraper — businesses from yelp.com search (URL or category + location).

Yelp hard-blocks free/datacenter IPs (curl AND headless both get a ~1.6 KB challenge), so this is
PROXY-ONLY and needs a US **residential** PROXY_URL to actually return data — the real IP is NEVER
used. A query is either a yelp.com /search URL or a "Category | Location" pair (the structured form
builds those from categories × locations).

Data comes from Yelp's own embedded JSON-LD (`ItemList` of `LocalBusiness`) plus the rendered
`[data-testid="serp-ia-card"]` cards as a fallback. Parser is best-effort — finalized against a real
(residential-proxy) results page.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import quote_plus, urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup

from .config import settings

BASE = "https://www.yelp.com"

YELP_COLUMNS = ["query", "name", "rating", "reviews", "categories", "price", "phone",
                "address", "url", "image"]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _search_url(query: str) -> str:
    """A query is a yelp.com URL (used as-is) or a 'Category | Location' pair -> /search URL."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q
    if "|" in q:
        cat, loc = (p.strip() for p in q.split("|", 1))
    else:
        cat, loc = q, ""
    return f"{BASE}/search?find_desc={quote_plus(cat)}&find_loc={quote_plus(loc)}"


def _page_url(url: str, start: int) -> str:
    if start <= 0:
        return url
    u = urlparse(url)
    q = parse_qs(u.query)
    q["start"] = [str(start)]
    return urlunparse(u._replace(query=urlencode({k: v[0] for k, v in q.items()})))


def _has_results(text: str) -> bool:
    low = (text or "").lower()
    return "/biz/" in low and len(text) > 50000


# ---------------- proxy fetch (never the real IP) ----------------

def _headless_get_sync(url: str, proxy: str | None) -> str | None:
    """Render Yelp in headless Chrome THROUGH `proxy` (clears the JS challenge). PROXY-ONLY: returns
    None without a proxy — the real IP is never used."""
    if not proxy:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    server = proxy if proxy.startswith("http") else "http://" + proxy
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy={"server": server},
                                        args=["--no-sandbox",
                                              "--disable-blink-features=AutomationControlled"])
            page = browser.new_context(locale="en-US", user_agent=_UA).new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            for _ in range(8):
                if _has_results(page.content()):
                    break
                page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
            return html if _has_results(html) else None
    except Exception:
        return None


def _get_html(url: str) -> str | None:
    """Yelp results page, PROXY-ONLY — never the real IP. A residential PROXY_URL clears Yelp's
    anti-bot; the free datacenter pool is hard-blocked (returns None -> clear note)."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return _headless_get_sync(url, proxy)
    # free pool: Yelp hard-blocks datacenter IPs, but try a couple of headless renders anyway
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "New York, NY", "page": "1"}, 4)
    seen = set()
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px in seen:
            continue
        seen.add(px)
        html = _headless_get_sync(url, px)
        if html:
            return html
        if len(seen) >= 4:
            break
    return None


# ---------------- parsing ----------------

def _txt(node):
    return node.get_text(" ", strip=True) if node else ""


def _from_jsonld(html: str, query: str) -> list[dict]:
    """PRIMARY: Yelp ships its results as schema.org JSON-LD (ItemList of LocalBusiness)."""
    out = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = []
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") == "ItemList":
                items += [el.get("item", el) for el in obj.get("itemListElement") or []]
            elif "LocalBusiness" in str(obj.get("@type", "")) or obj.get("@type") == "Restaurant":
                items.append(obj)
        for it in items:
            if not isinstance(it, dict):
                continue
            agg = it.get("aggregateRating") or {}
            addr = it.get("address") or {}
            out.append({
                "query": query, "name": it.get("name") or "",
                "rating": str(agg.get("ratingValue") or "") if isinstance(agg, dict) else "",
                "reviews": str(agg.get("reviewCount") or "") if isinstance(agg, dict) else "",
                "categories": ", ".join(it.get("servesCuisine") or []) if isinstance(it.get("servesCuisine"), list) else (it.get("servesCuisine") or ""),
                "price": it.get("priceRange") or "",
                "phone": it.get("telephone") or "",
                "address": ", ".join(x for x in [addr.get("streetAddress"), addr.get("addressLocality"),
                                                 addr.get("addressRegion")] if x) if isinstance(addr, dict) else "",
                "url": it.get("url") or "", "image": it.get("image") or "",
            })
    return [r for r in out if r["name"]]


def _from_cards(html: str, query: str) -> list[dict]:
    """Fallback: rendered SERP cards (selectors finalize against a real residential response)."""
    soup = BeautifulSoup(html or "", "lxml")
    out, seen = [], set()
    for c in soup.select('[data-testid="serp-ia-card"], li div[class*="businessName"]'):
        a = c.select_one('a[href*="/biz/"]')
        name = _txt(a) or _txt(c.select_one('[class*="businessName"] a, h3, h4'))
        if not name or name in seen:
            continue
        seen.add(name)
        href = a.get("href") if a else ""
        rtext = c.get_text(" ", strip=True)
        rm = re.search(r"(\d(?:\.\d)?)\s*star", rtext, re.I)
        vm = re.search(r"\(([\d,]+)\)|([\d,]+)\s+reviews", rtext, re.I)
        img = c.select_one("img")
        out.append({
            "query": query, "name": name,
            "rating": rm.group(1) if rm else "",
            "reviews": (vm.group(1) or vm.group(2)).replace(",", "") if vm else "",
            "categories": "", "price": "", "phone": "",
            "address": _txt(c.select_one('[class*="secondaryAttributes"], address')),
            "url": (BASE + href) if href.startswith("/") else (href or ""),
            "image": (img.get("src") or img.get("data-src")) if img else "",
        })
    return out


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    base = _search_url(query)
    rows, seen = [], set()
    for page in range(0, 25):
        html = _get_html(_page_url(base, page * 10))
        if html is None:
            break
        page_rows = _from_jsonld(html, query) or _from_cards(html, query)
        new = [r for r in page_rows if r["name"] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r["name"])
            rows.append(r)
            if limit and len(rows) >= limit:
                return rows
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from .db import jobs, yelp_results
    total = 0
    try:
        per = max(1, limit // len(queries)) if limit else None      # split the total over queries
        for q in queries:
            rows = await search(q, per)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await yelp_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
            if limit and total >= limit:
                break
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("Yelp returned 0 — it hard-blocks free/datacenter IPs (the real IP is "
                            "never used). Set a US residential PROXY_URL in .env to scrape it.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
