"""Allegro Scraper — products from allegro.pl search/listing and offer pages.

A query is an Allegro listing/search URL (…/listing?string=… or …/kategoria/…) or a single
offer URL (…/oferta/…). Each page is fetched through the proxy pool (paid PROXY_URL / PROXY_LIST
if set, else the rotating free pool — NEVER the real IP). One row per product. `limit` caps
products per query.

Allegro is protected by DataDome (geo.captcha-delivery.com) — the same aggressive anti-bot tier
as Trustpilot / Kununu / Thuisbezorgd / 1688. The datacenter free pool (and even a real IP) gets a
403 captcha, so live scraping needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The parser below
handles both Allegro data shapes — JSON-LD `Product` (offer pages) and the embedded
`__listing_StoreState` blob (listing/search pages) — so it returns rows as soon as a residential
proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

AL_COLUMNS = [
    "query", "title", "price", "currency", "condition", "seller",
    "rating", "reviews", "url", "image", "description",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _num(v):
    """Pull a numeric price out of strings like '1 299,00 zł' or {'amount':'129.00'}."""
    if isinstance(v, dict):
        v = v.get("amount") or v.get("value") or v.get("price") or ""
    s = str(v or "")
    m = re.search(r"\d[\d\s.,]*", s)
    if not m:
        return ""
    n = m.group(0).replace(" ", "").replace(" ", "")
    # 1.299,00 -> 1299.00 ; 1,299.00 -> 1299.00 ; 129,00 -> 129.00
    if "," in n and "." in n:
        n = n.replace(".", "").replace(",", ".") if n.rfind(",") > n.rfind(".") else n.replace(",", "")
    elif "," in n:
        n = n.replace(",", ".")
    return n


def _row_from_ld(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict) or not d.get("name"):
        return None
    offers = d.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    cond = offers.get("itemCondition") or d.get("itemCondition") or ""
    if isinstance(cond, str):
        cond = cond.rsplit("/", 1)[-1].replace("Condition", "")
    agg = d.get("aggregateRating") or {}
    seller = offers.get("seller") or {}
    if isinstance(seller, dict):
        seller = seller.get("name") or ""
    row = {c: "" for c in AL_COLUMNS}
    row.update({
        "query": query,
        "title": _u(d.get("name")),
        "price": _num(offers.get("price")),
        "currency": offers.get("priceCurrency") or "PLN",
        "condition": _u(cond),
        "seller": _u(seller),
        "rating": str(agg.get("ratingValue") or ""),
        "reviews": str(agg.get("reviewCount") or agg.get("ratingCount") or ""),
        "url": offers.get("url") or d.get("url") or "",
        "image": _first(d.get("image")),
        "description": _u(re.sub(r"<[^>]+>", " ", str(d.get("description") or "")))[:500].strip(),
    })
    return row


def _row_from_item(it: dict, query: str) -> dict | None:
    """Row from one element of Allegro's embedded __listing_StoreState items list."""
    if not isinstance(it, dict):
        return None
    title = it.get("title") or it.get("name") or ""
    if isinstance(title, dict):
        title = title.get("text") or title.get("name") or ""
    if not title:
        return None
    price = it.get("price") or it.get("sellingMode") or {}
    cur = "PLN"
    if isinstance(price, dict):
        norm = price.get("normal") or price.get("price") or price
        if isinstance(norm, dict):
            cur = norm.get("currency") or cur
        price = norm
    seller = it.get("seller") or {}
    if isinstance(seller, dict):
        seller = seller.get("login") or seller.get("name") or ""
    rate = it.get("rating") or it.get("aggregateRating") or {}
    rv = rc = ""
    if isinstance(rate, dict):
        rv = rate.get("averageRating") or rate.get("ratingValue") or rate.get("normalizedValue") or ""
        rc = rate.get("count") or rate.get("reviewCount") or rate.get("ratingCount") or ""
    url = it.get("url") or it.get("link") or ""
    if url and url.startswith("/"):
        url = "https://allegro.pl" + url
    imgs = it.get("images") or it.get("photos") or it.get("image") or []
    img = ""
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        img = first.get("url") or first.get("original") or "" if isinstance(first, dict) else first
    elif isinstance(imgs, str):
        img = imgs
    row = {c: "" for c in AL_COLUMNS}
    row.update({
        "query": query,
        "title": _u(title),
        "price": _num(price),
        "currency": cur,
        "seller": _u(seller),
        "rating": str(rv or ""),
        "reviews": str(rc or ""),
        "url": url,
        "image": img or "",
    })
    return row


def _parse_listing_state(html_text: str, query: str) -> list[dict]:
    """Pull product rows out of the embedded __listing_StoreState JSON blob."""
    m = re.search(r"__listing_StoreState\s*=\s*(\{.*?\})\s*;?\s*</script>", html_text, re.S)
    if not m:
        m = re.search(r'"__listing_StoreState"\s*:\s*(\{.*?\})\s*[,}]', html_text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out, seen = [], set()
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            # an items container: {"items": {"elements": [...]}} or {"items": [...]}
            if "title" in cur and ("price" in cur or "sellingMode" in cur):
                row = _row_from_item(cur, query)
                if row and row["title"]:
                    key = (row["title"], row["price"], row["url"])
                    if key not in seen:
                        seen.add(key)
                        out.append(row)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def _parse(html_text: str, query: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    out, seen = [], set()
    # 1) JSON-LD Product (offer pages, sometimes embedded in listings)
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                t = cur.get("@type")
                if (t == "Product" or (isinstance(t, list) and "Product" in t)) and cur.get("name"):
                    row = _row_from_ld(cur, query)
                    if row:
                        key = (row["title"], row["price"], row["url"] or row["image"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    # 2) embedded listing state (search/category pages)
    for row in _parse_listing_state(html_text, query):
        key = (row["title"], row["price"], row["url"] or row["image"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = (query or "").strip()
    if not url.lower().startswith("http"):
        return []
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return []
    if r is None or r.status_code != 200:
        return []
    rows = _parse(r.text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in AL_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Allegro listing/offer URL and store one row per product."""
    from .db import jobs, allegro_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await allegro_results.insert_many(rows)
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
