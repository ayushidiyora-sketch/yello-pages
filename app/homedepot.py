"""Home Depot Products Scraper — product listings from a homedepot.com URL.

Data comes from Home Depot's own internal GraphQL API (federation-gateway, `searchModel` operation) —
the same free, key-less endpoint the site's frontend uses — instead of parsing page HTML. The HTML
page parser (ld+json -> embedded GraphQL blob -> DOM) is kept as a non-breaking fallback for when the
API yields nothing (e.g. a /p/ product-detail URL, which the search API doesn't cover).

homedepot.com is protected by Akamai Bot Manager: it returns a JS sensor "challenge" page to every
free/datacenter proxy (and to the bare real IP via curl), so it CANNOT be scraped on the free tier —
both the GraphQL API and the HTML page need a paid residential PROXY_URL. PROXY-ONLY: the real IP is
never used. With a residential proxy set, the API returns clean structured products; the minimal
`searchModel` query is best-effort against the live schema and safely falls back to HTML if rejected.
"""
import asyncio
import json
import re

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings


def _is_block(html: str) -> bool:
    """True for an Akamai challenge / access-denied page (no product data)."""
    if not html or len(html) < 8000:
        low = (html or "").lower()
        return ("akamai" in low or "sec-if-cpt-container" in low or "access denied" in low
                or "reference #" in low or len(html or "") < 8000)
    low = html.lower()
    return ("sec-if-cpt-container" in low or "powered and protected by" in low
            or "access to this page has been denied" in low)


def _has_products(html: str) -> bool:
    low = (html or "").lower()
    return ('"productlabel"' in low or "product-pod" in low or '"canonicalurl"' in low
            or 'data-testid="product-pod"' in low or '"brandname"' in low)


def _ok(r) -> bool:
    return r is not None and r.status_code == 200 and not _is_block(r.text) and _has_products(r.text)


def _get(url: str):
    """Fetch through a proxy — NEVER the real IP. Paid PROXY_URL if set, else fail fast on the free
    pool (homedepot.com Akamai-blocks those → clear blocked error)."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                        timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 3)
    seen = set()
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px in seen:
            continue
        seen.add(px)
        try:
            r = cffi.get(url, impersonate="chrome", proxies={"http": px, "https": px},
                         timeout=7, verify=False, allow_redirects=True)
            if _ok(r):
                return r
        except Exception:
            pass
        if len(seen) >= 4:
            break
    raise RuntimeError("homedepot.com blocks free proxies (Akamai Bot Manager) — set a paid "
                       "residential PROXY_URL to scrape it. No real IP was used.")


# ---------------- Home Depot's free internal GraphQL API (federation-gateway) ----------------
# homedepot.com's own frontend loads the product grid from this GraphQL endpoint (no key — just the
# same Akamai-gated proxy the HTML page needs). We POST the `searchModel` operation and read the
# structured product objects directly, instead of regex-scraping them out of the page HTML.
GQL_URL = "https://www.homedepot.com/federation-gateway/graphql"

_GQL_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "apollographql-client-name": "general-merchandise",
    "apollographql-client-version": "0.0.0",
    "x-experience-name": "general-merchandise",
    "x-debug": "false",
    "x-hd-dc": "origin",
    "origin": "https://www.homedepot.com",
}

# Minimal searchModel query — only the fields `_from_product_obj` consumes, to reduce the chance of a
# schema-mismatch rejection. Pagination via startIndex/pageSize; navParam drives /b/ browse pages.
_SEARCH_QUERY = (
    "query searchModel($keyword: String, $navParam: String, $storeId: String, "
    "$startIndex: Int, $pageSize: Int) {"
    " searchModel(keyword: $keyword, navParam: $navParam, storeId: $storeId, "
    "startIndex: $startIndex, pageSize: $pageSize) {"
    " searchReport { totalProducts startIndex pageSize }"
    " products {"
    " identifiers { canonicalUrl brandName itemId modelNumber productLabel storeSkuNumber }"
    " pricing { value original }"
    " reviews { ratingsReviews { averageRating totalReviews } }"
    " media { images { url } }"
    " availabilityType { type } } } }"
)


def _api_params(q: str):
    """Map a query to GraphQL inputs: a /b/ browse URL -> navParam (N-...); a /s/ URL or bare
    keyword -> keyword. Returns (keyword, navParam); both None for a /p/ detail URL (HTML handles it)."""
    from urllib.parse import unquote
    q = (q or "").strip()
    if q.lower().startswith("http"):
        m = re.search(r"/(N-[\w]+)", q)
        if m:
            return None, m.group(1)
        m = re.search(r"/s/([^/?#]+)", q)
        if m:
            return unquote(m.group(1)).replace("+", " "), None
        return None, None  # /p/ product detail or unknown -> let HTML fallback handle it
    return q, None


def _api_post(variables: dict):
    """POST the searchModel operation through a proxy — NEVER the real IP."""
    body = {"operationName": "searchModel", "variables": variables, "query": _SEARCH_QUERY}
    url = GQL_URL + "?opname=searchModel"
    kw = dict(impersonate="chrome", json=body, headers=_GQL_HEADERS,
              timeout=settings.REQUEST_TIMEOUT, verify=False)
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return cffi.post(url, proxies={"http": proxy, "https": proxy}, **kw)
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 3)
    seen = set()
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px in seen:
            continue
        seen.add(px)
        try:
            r = cffi.post(url, proxies={"http": px, "https": px}, **{**kw, "timeout": 8})
            if r is not None and r.status_code == 200 and '"products"' in (r.text or ""):
                return r
        except Exception:
            pass
        if len(seen) >= 4:
            break
    return None


def _api_search(query: str, limit: int | None) -> list[dict]:
    """PRIMARY path: pull products from Home Depot's GraphQL API. Returns [] on any failure so the
    caller falls back to the HTML page parser (non-breaking)."""
    keyword, nav_param = _api_params(query)
    if not keyword and not nav_param:
        return []
    rows, start, page_size = [], 0, 24
    for _page in range(20):
        variables = {"keyword": keyword, "navParam": nav_param, "storeId": "121",
                     "startIndex": start, "pageSize": page_size}
        try:
            r = _api_post(variables)
        except Exception:
            break
        if r is None or r.status_code != 200:
            break
        try:
            sm = (((r.json() or {}).get("data") or {}).get("searchModel")) or {}
        except Exception:
            break
        products = sm.get("products") or []
        if not products:
            break
        for p in products:
            if isinstance(p, dict):
                row = _from_product_obj(query, p)
                if row.get("name"):
                    rows.append(row)
        total = _num((sm.get("searchReport") or {}).get("totalProducts"))
        start += page_size
        if (limit and len(rows) >= limit) or (total and start >= int(float(total))):
            break
    return rows


def _to_url(q: str, page: int = 1) -> str:
    """A homedepot.com /b/, /p/ or /s/ URL as-is (paged via Nao= for listings); a bare keyword
    -> a /s/<keyword> search URL."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        base = q
    else:
        from urllib.parse import quote
        base = f"https://www.homedepot.com/s/{quote(q)}"
    if page > 1 and "/p/" not in base.lower():
        nao = (page - 1) * 24
        base += ("&" if "?" in base else "?") + f"Nao={nao}"
    return base


def _num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        m = re.search(r"[\d,.]+", v)
        return m.group(0).replace(",", "") if m else None
    if isinstance(v, dict):
        for k in ("value", "amount", "averageRating", "totalReviews"):
            if k in v:
                return _num(v[k])
    return None


def _row(query, name, brand=None, price=None, original=None, rating=None, reviews=None,
         model=None, sku=None, url=None, image=None, availability=None):
    return {"query": query, "name": name, "brand": brand, "price": price,
            "original_price": original, "rating": rating, "reviews": reviews, "model": model,
            "sku": sku, "url": url, "image": image, "availability": availability}


def _from_product_obj(query, p: dict):
    ids = p.get("identifiers") or {}
    pricing = p.get("pricing") or {}
    rr = ((p.get("reviews") or {}).get("ratingsReviews")) or {}
    media = p.get("media") or {}
    img = None
    imgs = media.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        img = imgs[0].get("url") or imgs[0].get("href")
    url = p.get("canonicalUrl") or ids.get("canonicalUrl")
    if url and url.startswith("/"):
        url = "https://www.homedepot.com" + url
    return _row(
        query,
        name=p.get("productLabel") or ids.get("productLabel") or p.get("name"),
        brand=p.get("brandName") or ids.get("brandName"),
        price=_num(pricing.get("value") or pricing),
        original=_num(pricing.get("original")),
        rating=_num(rr.get("averageRating")),
        reviews=_num(rr.get("totalReviews")),
        model=ids.get("modelNumber"),
        sku=ids.get("itemId") or ids.get("storeSkuNumber"),
        url=url,
        image=img,
        availability=(p.get("availabilityType") or {}).get("type") if isinstance(p.get("availabilityType"), dict) else None,
    )


def _parse(html: str, query: str) -> list[dict]:
    """Best-effort product extraction: ld+json, then embedded GraphQL product objects, then DOM."""
    out, seen = [], set()

    def add(row):
        key = row.get("name"), row.get("url")
        if row.get("name") and key not in seen:
            seen.add(key)
            out.append(row)

    soup = BeautifulSoup(html or "", "lxml")

    # 1) application/ld+json — Product or ItemList
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if not isinstance(d, dict):
                continue
            if d.get("@type") == "ItemList":
                for el in d.get("itemListElement") or []:
                    it = el.get("item") if isinstance(el, dict) else None
                    if isinstance(it, dict):
                        add(_row(query, it.get("name"), brand=(it.get("brand") or {}).get("name") if isinstance(it.get("brand"), dict) else it.get("brand"),
                                 price=_num((it.get("offers") or {}).get("price")) if isinstance(it.get("offers"), dict) else None,
                                 rating=_num((it.get("aggregateRating") or {}).get("ratingValue")) if isinstance(it.get("aggregateRating"), dict) else None,
                                 reviews=_num((it.get("aggregateRating") or {}).get("reviewCount")) if isinstance(it.get("aggregateRating"), dict) else None,
                                 url=it.get("url"), image=it.get("image")))
            elif d.get("@type") == "Product":
                ar = d.get("aggregateRating") or {}
                off = d.get("offers") or {}
                add(_row(query, d.get("name"),
                         brand=(d.get("brand") or {}).get("name") if isinstance(d.get("brand"), dict) else d.get("brand"),
                         price=_num(off.get("price") if isinstance(off, dict) else None),
                         rating=_num(ar.get("ratingValue")), reviews=_num(ar.get("reviewCount")),
                         model=d.get("model"), sku=d.get("sku"), url=d.get("url"), image=d.get("image")))

    # 2) embedded GraphQL product objects (Home Depot's product blob)
    if not out:
        for m in re.finditer(r'\{"identifiers":\{.*?"productLabel":"[^"]+".*?\}', html or ""):
            frag = m.group(0)
            depth, end = 0, None
            for i, ch in enumerate(m.string[m.start():m.start() + 6000]):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            try:
                p = json.loads(m.string[m.start():m.start() + end]) if end else None
                if isinstance(p, dict):
                    add(_from_product_obj(query, p))
            except Exception:
                pass

    # 3) DOM product pods
    if not out:
        for pod in soup.select('[data-testid*="product-pod"], .product-pod'):
            a = pod.select_one('a[href]')
            name = pod.select_one('[data-testid*="attribute-product-label"], .product-pod__title')
            price = pod.select_one('[class*="price"]')
            url = a["href"] if a and a.has_attr("href") else None
            if url and url.startswith("/"):
                url = "https://www.homedepot.com" + url
            add(_row(query, name.get_text(" ", strip=True) if name else None,
                     price=_num(price.get_text(" ", strip=True)) if price else None, url=url))

    return out


def _html_search(query: str, limit: int | None) -> list[dict]:
    """FALLBACK: scrape the HTML page and parse it (ld+json -> embedded GraphQL blob -> DOM)."""
    rows, page = [], 1
    while True:
        page_rows = _parse(_get(_to_url(query, page)).text, query)
        if not page_rows:
            break
        rows += page_rows
        if (limit and len(rows) >= limit) or "/p/" in query.lower() or page >= 20:
            break
        page += 1
    return rows


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    # PRIMARY: Home Depot's free internal GraphQL API. Fall back to HTML only if it yields nothing.
    rows = _api_search(query, limit)
    if not rows:
        rows = _html_search(query, limit)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from datetime import datetime
    from .db import jobs, homedepot_results
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await homedepot_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
