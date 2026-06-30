"""Willhaben Scraper — classified listings from willhaben.at search and detail pages.

A query is a willhaben.at search/category URL (…/iad/gebrauchtwagen/… or …/iad/kaufen-und-verkaufen/…)
or a single listing URL (…/iad/.../d/...-<id>). Each page is fetched through the proxy pool (paid
PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the real IP). One row per listing.
`limit` caps listings per query.

Willhaben serves clean data: search pages embed a Next.js `__NEXT_DATA__` blob with
`advertSummaryList.advertSummary[]` (each advert's fields live in an `attributes.attribute[]`
name/value list), and detail pages carry JSON-LD `Product`/`Offer`. The page renders fine with a
normal browser fingerprint, but willhaben 403s datacenter IPs by reputation — so the free pool is
blocked and live scraping needs a RESIDENTIAL proxy in PROXY_URL / PROXY_LIST. The parser below
reads both shapes, so rows come back as soon as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

WH_COLUMNS = [
    "query", "title", "price", "currency", "location", "postcode", "state",
    "condition", "mileage", "year", "fuel", "gearbox", "url", "image", "description",
]

_BASE = "https://www.willhaben.at/iad/"


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _num(v):
    """Pull a numeric value out of '€ 16.800' / '200000' / '16800.0' / {'value':..}."""
    if isinstance(v, dict):
        v = v.get("value") or v.get("amount") or ""
    s = str(v or "")
    m = re.search(r"\d[\d\s.,]*", s)
    if not m:
        return ""
    n = m.group(0).replace(" ", "").replace(" ", "")
    if "," in n and "." in n:
        n = n.replace(".", "").replace(",", ".") if n.rfind(",") > n.rfind(".") else n.replace(",", "")
    elif "," in n:
        n = n.replace(",", ".") if re.search(r",\d{1,2}$", n) else n.replace(",", "")
    elif "." in n:
        # 16.800 (German thousands) -> 16800 ; keep . only as a 1-2 digit decimal (16800.0 -> 16800)
        if re.search(r"\.\d{1,2}$", n) and n.count(".") == 1:
            n = str(int(float(n))) if n.endswith(".0") or n.endswith(".00") else n
        else:
            n = n.replace(".", "")
    return n


def _year(v):
    m = re.search(r"(19|20)\d{2}", str(v or ""))
    return m.group(0) if m else ""


def _attrs(advert: dict) -> dict:
    """Flatten Willhaben's attributes.attribute[] (name/values) into {NAME: first_value}."""
    out = {}
    attrs = (advert.get("attributes") or {})
    lst = attrs.get("attribute") if isinstance(attrs, dict) else attrs
    if isinstance(lst, list):
        for at in lst:
            if isinstance(at, dict) and at.get("name"):
                vals = at.get("values") or at.get("value") or []
                out[at["name"]] = (vals[0] if isinstance(vals, list) and vals else vals) or ""
    return out


def _row_from_advert(advert: dict, query: str) -> dict | None:
    if not isinstance(advert, dict):
        return None
    a = _attrs(advert)
    title = a.get("HEADING") or advert.get("description") or ""
    if not title:
        return None
    price = a.get("PRICE_FOR_DISPLAY") or a.get("PRICE") or a.get("PRICE/AMOUNT") or ""
    seo = a.get("SEO_URL") or ""
    url = (_BASE + seo.lstrip("/")) if seo else ""
    if not url:
        sl = advert.get("contextLinkList") or {}
        # fall back to any self/seo link present
        url = advert.get("selfLink") or ""
    img = ""
    ail = (advert.get("advertImageList") or {}).get("advertImage") if isinstance(advert.get("advertImageList"), dict) else None
    if isinstance(ail, list) and ail:
        img = ail[0].get("mainImageUrl") or ail[0].get("thumbnailImageUrl") or ""
    if not img:
        all_imgs = a.get("ALL_IMAGE_URLS") or ""
        img = all_imgs.split(";")[0] if all_imgs else ""
    loc = a.get("LOCATION") or a.get("ADDRESS") or ""
    row = {c: "" for c in WH_COLUMNS}
    row.update({
        "query": query,
        "title": _u(title),
        "price": _num(price),
        "currency": "EUR",
        "location": _u(loc),
        "postcode": str(a.get("POSTCODE") or ""),
        "state": _u(a.get("STATE") or a.get("COUNTRY") or ""),
        "condition": _u(a.get("CONDITION_RESOLVED") or ""),
        "mileage": _num(a.get("MILEAGE")),
        "year": _year(a.get("YEAR_MODEL")),
        "fuel": _u(a.get("ENGINE/FUEL_RESOLVED") or ""),
        "gearbox": _u(a.get("TRANSMISSION_RESOLVED") or ""),
        "url": url,
        "image": img,
        "description": _u(re.sub(r"<[^>]+>", " ", str(a.get("BODY_DYN") or "")))[:500].strip(),
    })
    return row


def _addr_parts(addr):
    if isinstance(addr, dict):
        city = addr.get("addressLocality") or ""
        zc = addr.get("postalCode") or ""
        region = addr.get("addressRegion") or ""
        return _u(", ".join([p for p in [(str(zc) + " " + str(city)).strip(), region] if p.strip()])), _u(zc), _u(region)
    return _u(addr), "", ""


def _row_from_ld(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict):
        return None
    name = d.get("name") or ""
    offers = d.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    price = offers.get("price") or d.get("price") or ""
    cur = offers.get("priceCurrency") or "EUR"
    addr = offers.get("availableAtOrFrom") or d.get("address") or ""
    if isinstance(addr, dict) and addr.get("address"):
        addr = addr.get("address")
    loc, zc, region = _addr_parts(addr)
    if not (name or price):
        return None
    row = {c: "" for c in WH_COLUMNS}
    row.update({
        "query": query,
        "title": _u(name),
        "price": _num(price),
        "currency": cur,
        "location": loc,
        "postcode": zc,
        "state": region,
        "condition": _u((offers.get("itemCondition") or "").rsplit("/", 1)[-1].replace("Condition", "")) if offers.get("itemCondition") else "",
        "url": d.get("url") or offers.get("url") or "",
        "image": _first(d.get("image")),
        "description": _u(re.sub(r"<[^>]+>", " ", str(d.get("description") or "")))[:500].strip(),
    })
    return row


def _parse_next(html_text: str, query: str) -> list[dict]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html_text, re.S)
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
            adverts = cur.get("advertSummary")
            if isinstance(adverts, list):
                for ad in adverts:
                    row = _row_from_advert(ad, query)
                    if row:
                        key = (row["title"], row["price"], row["url"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def _parse(html_text: str, query: str) -> list[dict]:
    out, seen = [], set()
    # 1) Next.js advert list (search/category pages)
    for row in _parse_next(html_text, query):
        key = (row["title"], row["price"], row["url"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    # 2) JSON-LD Product/Offer (detail pages)
    soup = BeautifulSoup(html_text, "lxml")
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
                tset = set(t) if isinstance(t, list) else {t}
                if tset & {"Product", "Offer", "Car", "Vehicle"} and (cur.get("name") or cur.get("offers")):
                    row = _row_from_ld(cur, query)
                    if row:
                        key = (row["title"], row["price"], row["url"] or row["image"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
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
    return {c: doc.get(c, "") for c in WH_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Willhaben search/listing URL and store one row per listing."""
    from .db import jobs, willhaben_results
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
                await willhaben_results.insert_many(rows)
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
