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
from .config import settings

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


# ---------------- headless "all products" (best-effort) ----------------

# Extract every hydrated product card in the live DOM (the ~18 PerimeterX resolves client-side).
_EXTRACT_JS = r"""() => {
  const out = [];
  for (const li of document.querySelectorAll('li.product-list-item')) {
    const a = li.querySelector("a[href*='/site/'],a[href*='skuId=']");
    const name = a ? a.textContent.trim() : '';
    if (!name || name.length < 8) continue;
    const t = li.innerText || '';
    const price = (t.match(/\$[\d,]+(?:\.\d{2})?/) || [''])[0];
    const rat = (t.match(/(\d(?:\.\d)?)\s*out of 5/) || ['',''])[1];
    const rev = (t.match(/([\d,]+)\s*reviews?/i) || ['',''])[1];
    let sku = li.getAttribute('data-product-id') || '';
    if (!sku && a) { const m = a.href.match(/skuId=(\d+)/) || a.href.match(/\/(\d{6,})\.p/); if (m) sku = m[1]; }
    const img = li.querySelector('img');
    out.push({sku, name, price, rating: rat, reviews: (rev||'').replace(/,/g,''),
              image: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
              url: a ? a.href : ''});
  }
  return out;
}"""


def _headless_rows_sync(url: str, proxy: str, want: int) -> list[dict]:
    """Render BestBuy through `proxy` (US, never the real IP) and scrape the hydrated product cards —
    the ~18 products PerimeterX resolves client-side and never puts in the HTML. Best-effort: PerimeterX
    may blank the page or `goto` may time out → returns [] (the curl path still provides the ~5 floor).
    Keeps the richest snapshot seen (most named cards) before any blanking."""
    if not proxy:
        return []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    server = proxy if proxy.startswith("http") else "http://" + proxy
    best: list[dict] = []
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, proxy={"server": server},
                                   args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = br.new_context(locale="en-US", user_agent=_UA, viewport={"width": 1366, "height": 1200})
            ctx.route(re.compile(r"\.(png|jpg|jpeg|webp|gif|woff2?|mp4|svg|css)(\?|$)"),
                      lambda r: r.abort())
            pg = ctx.new_page()
            pg.goto(url, timeout=70000, wait_until="commit")
            for i in range(20):
                pg.wait_for_timeout(1500)
                try:
                    rows = [r for r in pg.evaluate(_EXTRACT_JS) if r.get("name")]
                except Exception:
                    rows = []
                if len(rows) > len(best):
                    best = rows
                if len(best) >= want or len(best) >= 18:
                    break
                try:                                   # light scroll nudges lazy hydration
                    pg.evaluate(f"window.scrollTo(0,{(i % 6) * 1400})")
                except Exception:
                    pass
            br.close()
    except Exception:
        return best
    return best


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _headless_proxy() -> str | None:
    """A US proxy server for the headless render: BestBuy-only → global paid → a warm free-pool proxy."""
    px = settings.BESTBUY_PROXY_URL.strip() or settings.PROXY_URL.strip()
    if px:
        return px
    try:
        yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 4)
        with yp_us._LOCK:
            return (list(yp_us._GOOD) or [None])[0]
    except Exception:
        return None


def _headless_rows(url: str, want: int) -> list[dict]:
    """Best-effort headless scrape with retries (the render is flaky against PerimeterX)."""
    proxy = _headless_proxy()
    if not proxy:
        return []
    best: list[dict] = []
    for _ in range(3):
        rows = _headless_rows_sync(url, proxy, want)
        if len(rows) > len(best):
            best = rows
        if len(best) >= 18 or len(best) >= want:
            break
    # normalize to BESTBUY_COLUMNS rows
    out = []
    for r in best:
        sku = str(r.get("sku") or "")
        out.append({
            "query": "", "name": (r.get("name") or "")[:250], "sku": sku,
            "brand": (r.get("name") or "").split(" - ")[0].strip(),
            "model": "", "price": r.get("price") or "",
            "rating": r.get("rating") or "", "reviews": r.get("reviews") or "",
            "image": r.get("image") or "",
            "url": r.get("url") or (f"{BASE}/site/-/{sku}.p?skuId={sku}" if sku else ""),
        })
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """Per page (`&cp=2,3,…`): the curl path SSRs only ~5 fully-parseable products (the reliable floor);
    when more is wanted, a best-effort headless render adds the ~18 PerimeterX resolves client-side.
    The two are merged + de-duped by sku. Headless is flaky (it may time out / get blanked) → on failure
    we still return the curl floor. Proxy-only — never the real IP."""
    want_more = (limit is None) or (limit > 5)
    rows, seen, miss = [], set(), 0
    for page in range(1, 30):
        if limit and len(rows) >= limit:
            break
        page_url = _page_url(query, page)
        html = _get(page_url)
        page_rows = _parse_listing(html, query) if html else []     # reliable ~5
        if want_more:                                               # best-effort: the JS-only rest
            for hr in _headless_rows(page_url, limit or 24):
                if hr["sku"] and not any(p["sku"] == hr["sku"] for p in page_rows):
                    hr["query"] = query
                    page_rows.append(hr)
        new = [r for r in page_rows if r["sku"] and r["sku"] not in seen]
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
        elif limit and 0 < total <= 6:
            done["note"] = ("BestBuy server-renders only ~5 products/page; the rest are PerimeterX "
                            "browser-only and the headless render timed out / was blocked this run — "
                            "retry, or use a residential proxy. The real IP is never used.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
