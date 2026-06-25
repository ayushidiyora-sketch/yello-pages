"""BestBuy Products Scraper — product listings from bestbuy.com category/search/brand/product URLs.

bestbuy.com geo-blocks non-US IPs (a tiny "Select your Country" interstitial), so EVERY request goes
through a US proxy IP — the rotating free US pool via `yp_us.pooled_get` (or a paid PROXY_URL); the
real IP is NEVER used. Through a US proxy the real page loads un-blocked on the FREE pool (same as the
yellowpages scraper).

Product data is read from BestBuy's own embedded GraphQL/Apollo JSON (`detailedProductSearch` —
skuId / customerPrice / name / manufacturer / modelNumber) merged with the rendered
`li.product-list-item` cards (sku in `data-product-id`, image, rating/reviews in the item text).
A query is a category / search / brand / product (`/p/`) URL; `limit` caps the rows.
"""
import asyncio
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup

from . import yp_us

BASE = "https://www.bestbuy.com"

BESTBUY_COLUMNS = ["query", "name", "sku", "brand", "model", "price", "rating", "reviews",
                   "image", "url", "position"]

_HEADERS = {"Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _ok(text: str) -> bool:
    low = (text or "").lower()
    if "select your country" in low:        # geo-block interstitial (non-US IP)
        return False
    return "product-list-item" in low or '"skuid"' in low or '"sku":' in low


def _page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    u = urlparse(url)
    q = parse_qs(u.query)
    q["cp"] = [str(page)]
    return urlunparse(u._replace(query=urlencode({k: v[0] for k, v in q.items()})))


def _get(url: str) -> str | None:
    """Fetch a BestBuy page through a US proxy (free pool or paid PROXY_URL) — never the real IP.
    Some free proxies aren't US (BestBuy then serves a 'select your country' page), so retry across a
    few rotated proxies until one returns the real US page."""
    for _ in range(6):
        r = yp_us.pooled_get(url, {}, timeout=25, headers=_HEADERS)
        if r is not None and r.status_code == 200 and _ok(r.text):
            return r.text
    return None


# ---------------- parsing ----------------

def _unescape(s: str) -> str:
    return (s or "").encode("utf-8").decode("unicode_escape", "ignore") if "\\" in (s or "") else s


def _sku_price_map(html: str) -> dict:
    """{sku: customerPrice} from the embedded price blocks
    (`"price":{...,"customerPrice":N,...,"skuId":"<sku>"}`)."""
    out = {}
    for price, sku in re.findall(r'"customerPrice":([\d.]+),"mobileContracts":\[\],"skuId":"(\d+)"',
                                 html):
        out.setdefault(sku, price)
    return out


def _parse_listing(html: str, query: str) -> list[dict]:
    """Parse BestBuy's embedded GraphQL product documents (the DOM SSRs only ~2 cards; the JSON has
    all of them). The canonical product name is the description `title` immediately before its
    `skuId`; rating/reviews/model come from the same product document, price from the price block."""
    prices = _sku_price_map(html)
    rows, seen = [], set()
    for m in re.finditer(r'"product":\{"__typename":"Product","skuId":"(\d+)"', html):
        sku = m.group(1)
        if sku in seen:
            continue
        seg = html[m.start():m.start() + 9000]
        # pick the product name from this document's titles: BestBuy names carry " - " separators;
        # skip 3D-asset filenames (.usdz/.glb) and deal/badge labels.
        cands = [_unescape(x) for x in re.findall(r'"title":"((?:[^"\\]|\\.)*)"', seg[:6000])]
        cands = [x for x in cands if not re.search(r'\.(usdz|glb|gltf|jpg|png|webp|mp4)', x, re.I)]
        named = [x for x in cands if " - " in x and len(x) > 18 and "Deal" not in x]
        name = (max(named, key=len) if named else max(cands, key=len, default=""))[:250]
        if not name or len(name) < 6 or name.lower() in seen:   # dedup variants by product name
            continue
        seen.add(sku)
        seen.add(name.lower())
        rat = re.search(r'"averageRating":([\d.]+)', seg)
        rev = re.search(r'"reviewCount":(\d+)', seg)
        model = re.search(r'"modelNumber":"([^"]+)"', seg)
        img = re.search(r'https://pisces\.bbystatic\.com/[^"\\]+\.(?:jpg|png|webp)', seg)
        pm = re.search(r'/product/[^/]+/[A-Z0-9]+/sku/' + sku, seg)
        url = (BASE + pm.group(0)) if pm else f"{BASE}/site/-/{sku}.p?skuId={sku}"
        brand = name.split(" - ")[0].strip() if " - " in name else (name.split()[0] if name else "")
        rows.append({
            "query": query, "name": name, "sku": sku, "brand": brand,
            "model": model.group(1) if model else "",
            "price": f"${prices.get(sku, '')}" if prices.get(sku) else "",
            "rating": rat.group(1) if rat else "",
            "reviews": rev.group(1) if rev else "",
            "image": img.group(0) if img else "",
            "url": url,
        })
    return rows


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """BestBuy SSRs only a handful of fully-parseable products per page (the rest are JS-resolved
    references), so accumulate across pages (`&cp=2,3,…`). Tolerate the odd failed/empty page (free
    proxies are flaky) instead of stopping on the first one."""
    rows, seen, miss = [], set(), 0
    for page in range(1, 30):
        if limit and len(rows) >= limit:
            break
        html = _get(_page_url(query, page))
        page_rows = _parse_listing(html, query) if html else []
        new = [r for r in page_rows if r["sku"] not in seen]
        if not new:
            miss += 1
            if miss >= 3:                       # 3 empty/failed pages in a row → done
                break
            continue
        miss = 0
        for r in new:
            seen.add(r["sku"])
            rows.append(r)
            if limit and len(rows) >= limit:
                break
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from .db import jobs, bestbuy_results
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await bestbuy_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("BestBuy returned 0 — the US free proxy may have been geo-blocked; try "
                            "again (it rotates proxies) or set a US PROXY_URL. The real IP is never used.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
