"""Immowelt Scraper — property listings from immowelt.de classified-search and expose pages.

A query is an Immowelt search URL (…/classified-search?…) or a single listing/expose URL
(…/expose/…). Each page is fetched through the proxy pool (paid PROXY_URL / PROXY_LIST if set,
else the rotating free pool — NEVER the real IP). One row per property. `limit` caps properties
per query.

Immowelt is protected by DataDome (geo.captcha-delivery.com) behind CloudFront — the same aggressive
anti-bot tier as Allegro / Trustpilot / Kununu / Thuisbezorgd / 1688. The datacenter free pool (and
even a real IP) gets a 403 captcha, so live scraping needs RESIDENTIAL proxies in PROXY_URL /
PROXY_LIST. The parser below handles both Immowelt data shapes — JSON-LD (`RealEstateListing` /
`Product` / `Residence` / `Offer`) and the embedded Next/Nuxt JSON state — so it returns rows as soon
as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

IW_COLUMNS = [
    "query", "title", "price", "currency", "address", "city", "zip",
    "rooms", "living_area", "plot_area", "url", "image", "description",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _num(v):
    """Pull a numeric value out of strings like '450.000 €' or {'amount':'450000'}."""
    if isinstance(v, dict):
        v = v.get("amount") or v.get("value") or v.get("price") or v.get("min") or ""
    s = str(v or "")
    m = re.search(r"\d[\d\s.,]*", s)
    if not m:
        return ""
    n = m.group(0).replace(" ", "").replace(" ", "")
    # German: 450.000,00 -> 450000.00 ; English: 450,000.00 -> 450000.00
    if "," in n and "." in n:
        n = n.replace(".", "").replace(",", ".") if n.rfind(",") > n.rfind(".") else n.replace(",", "")
    elif "," in n:
        # ambiguous: treat , as decimal only if 1-2 trailing digits, else thousands sep
        n = n.replace(",", ".") if re.search(r",\d{1,2}$", n) else n.replace(",", "")
    elif "." in n:
        # dot as thousands separator (e.g. German 450.000 / 1.234.567) -> strip;
        # keep it as a decimal point only when 1-2 trailing digits (e.g. 450.50)
        n = n if re.search(r"\.\d{1,2}$", n) and n.count(".") == 1 else n.replace(".", "")
    return n


def _addr_parts(addr):
    """Return (full, city, zip) from a JSON-LD PostalAddress or string."""
    if isinstance(addr, dict):
        street = addr.get("streetAddress") or ""
        city = addr.get("addressLocality") or addr.get("addressRegion") or ""
        zc = addr.get("postalCode") or ""
        full = ", ".join([p for p in [street, ((zc + " " + city).strip())] if p])
        return _u(full), _u(city), _u(zc)
    s = _u(addr)
    m = re.search(r"\b(\d{4,5})\b", s)
    zc = m.group(1) if m else ""
    return s, "", zc


def _row_from_ld(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict):
        return None
    name = d.get("name") or d.get("headline") or ""
    offers = d.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    price = offers.get("price") or d.get("price") or ""
    cur = offers.get("priceCurrency") or d.get("priceCurrency") or "EUR"
    addr = d.get("address") or (offers.get("address") if isinstance(offers, dict) else "") or ""
    full, city, zc = _addr_parts(addr)
    rooms = d.get("numberOfRooms") or d.get("numberOfRoomsTotal") or ""
    if isinstance(rooms, dict):
        rooms = rooms.get("value") or ""
    area = d.get("floorSize") or d.get("livingArea") or ""
    if isinstance(area, dict):
        area = area.get("value") or ""
    plot = d.get("lotSize") or ""
    if isinstance(plot, dict):
        plot = plot.get("value") or ""
    if not (name or price or full):
        return None
    row = {c: "" for c in IW_COLUMNS}
    row.update({
        "query": query,
        "title": _u(name),
        "price": _num(price),
        "currency": cur,
        "address": full,
        "city": city,
        "zip": zc,
        "rooms": str(rooms or ""),
        "living_area": str(area or ""),
        "plot_area": str(plot or ""),
        "url": d.get("url") or offers.get("url") or "",
        "image": _first(d.get("image")),
        "description": _u(re.sub(r"<[^>]+>", " ", str(d.get("description") or "")))[:500].strip(),
    })
    return row


def _row_from_item(it: dict, query: str) -> dict | None:
    """Row from one element of Immowelt's embedded listing/state JSON."""
    if not isinstance(it, dict):
        return None
    title = it.get("title") or it.get("estateName") or it.get("headline") or it.get("name") or ""
    if isinstance(title, dict):
        title = title.get("text") or title.get("value") or ""
    price = it.get("price") or it.get("primaryPrice") or it.get("hardFacts", {}).get("price") if isinstance(it.get("hardFacts"), dict) else it.get("price")
    if price is None:
        price = it.get("price") or it.get("primaryPrice") or ""
    cur = "EUR"
    if isinstance(price, dict):
        cur = price.get("currency") or cur
    place = it.get("place") or it.get("placeData") or it.get("address") or it.get("location") or {}
    full = city = zc = ""
    if isinstance(place, dict):
        city = place.get("city") or place.get("locality") or place.get("addressLocality") or ""
        zc = place.get("zipCode") or place.get("postcode") or place.get("postalCode") or ""
        street = place.get("street") or place.get("streetAddress") or ""
        full = ", ".join([p for p in [street, ((str(zc) + " " + str(city)).strip())] if p and p.strip()])
    elif isinstance(place, str):
        full, city, zc = _addr_parts(place)
    rooms = it.get("roomsMax") or it.get("rooms") or it.get("numberOfRooms") or ""
    area = it.get("livingSpace") or it.get("livingArea") or it.get("area") or ""
    plot = it.get("plotArea") or it.get("siteArea") or it.get("lotSize") or ""
    url = it.get("url") or it.get("link") or it.get("relativeUrl") or ""
    if url and url.startswith("/"):
        url = "https://www.immowelt.de" + url
    imgs = it.get("pictures") or it.get("images") or it.get("photos") or it.get("image") or []
    img = ""
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        img = (first.get("url") or first.get("imageUri") or first.get("src") or "") if isinstance(first, dict) else first
    elif isinstance(imgs, str):
        img = imgs
    if not (title or full or price):
        return None
    row = {c: "" for c in IW_COLUMNS}
    row.update({
        "query": query,
        "title": _u(title),
        "price": _num(price),
        "currency": cur,
        "address": _u(full),
        "city": _u(city),
        "zip": str(zc or ""),
        "rooms": str(rooms or ""),
        "living_area": str(area or ""),
        "plot_area": str(plot or ""),
        "url": url,
        "image": img or "",
    })
    return row


def _looks_like_listing(d: dict) -> bool:
    keys = set(d.keys())
    has_title = bool(keys & {"title", "estateName", "headline"})
    has_price = bool(keys & {"price", "primaryPrice", "hardFacts"})
    has_place = bool(keys & {"place", "placeData", "location"})
    return has_title and (has_price or has_place)


def _parse_state(html_text: str, query: str) -> list[dict]:
    """Walk embedded JSON state blobs for property listing objects."""
    out, seen = [], set()
    blobs = []
    for m in re.finditer(r"__NEXT_DATA__[^>]*>(\{.*?\})</script>", html_text, re.S):
        blobs.append(m.group(1))
    for m in re.finditer(r"window\.__(?:INITIAL_STATE|NUXT|PRELOADED_STATE)__\s*=\s*(\{.*?\})\s*[;<]", html_text, re.S):
        blobs.append(m.group(1))
    for raw in blobs:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if _looks_like_listing(cur):
                    row = _row_from_item(cur, query)
                    if row and (row["title"] or row["address"]):
                        key = (row["title"], row["price"], row["url"] or row["address"])
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
    # 1) JSON-LD (RealEstateListing / Product / Residence / Offer / Apartment / House)
    types = {"RealEstateListing", "Product", "Residence", "Apartment", "House",
             "SingleFamilyResidence", "Offer", "Accommodation"}
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
                if tset & types:
                    row = _row_from_ld(cur, query)
                    if row:
                        key = (row["title"], row["price"], row["url"] or row["image"] or row["address"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    # 2) embedded state (search/result pages)
    for row in _parse_state(html_text, query):
        key = (row["title"], row["price"], row["url"] or row["address"])
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
    return {c: doc.get(c, "") for c in IW_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Immowelt search/expose URL and store one row per property."""
    from .db import jobs, immowelt_results
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
                await immowelt_results.insert_many(rows)
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
