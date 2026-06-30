"""Mobile.de Scraper — vehicle listings from mobile.de search and detail pages.

A query is a mobile.de search URL (suchen.mobile.de/fahrzeuge/search.html?…) or a single vehicle
detail URL (…/details.html?id=…). Each page is fetched through the proxy pool (paid PROXY_URL /
PROXY_LIST if set, else the rotating free pool — NEVER the real IP). One row per vehicle. `limit`
caps vehicles per query.

Mobile.de is protected by Akamai Bot Manager (AkamaiGHost — "Zugriff verweigert / Access denied") —
the same aggressive anti-bot tier as Immowelt / Allegro / Trustpilot. The datacenter free pool (and
even a real IP) gets a 403, so live scraping needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The
parser below handles both Mobile.de data shapes — JSON-LD (`Car` / `Vehicle` / `Product` / `Offer`)
and the embedded JSON state — so it returns rows as soon as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

MD_COLUMNS = [
    "query", "title", "price", "currency", "mileage", "year", "fuel",
    "gearbox", "power", "location", "seller", "url", "image", "description",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _u(v):
    return html.unescape(str(v)) if v else ""


def _num(v):
    """Pull a numeric value out of strings like '24.900 €' / '120.000 km' / {'value':24900}."""
    if isinstance(v, dict):
        v = v.get("value") or v.get("amount") or v.get("price") or ""
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
        # German thousands separator (24.900 / 1.250.000) -> strip; keep . only as a 1-2 digit decimal
        n = n if re.search(r"\.\d{1,2}$", n) and n.count(".") == 1 else n.replace(".", "")
    return n


def _year(v):
    """Extract a 4-digit year from '06/2019', '2019-06', 2019, etc."""
    m = re.search(r"(19|20)\d{2}", str(v or ""))
    return m.group(0) if m else ""


def _addr(addr):
    if isinstance(addr, dict):
        city = addr.get("addressLocality") or addr.get("addressRegion") or ""
        zc = addr.get("postalCode") or ""
        country = addr.get("addressCountry") or ""
        if isinstance(country, dict):
            country = country.get("name") or ""
        return _u(", ".join([p for p in [(str(zc) + " " + str(city)).strip(), str(country)] if p.strip()]))
    return _u(addr)


def _row_from_ld(d: dict, query: str) -> dict | None:
    if not isinstance(d, dict):
        return None
    name = d.get("name") or d.get("model") or ""
    offers = d.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    price = offers.get("price") or d.get("price") or ""
    cur = offers.get("priceCurrency") or "EUR"
    seller = offers.get("seller") or d.get("seller") or {}
    if isinstance(seller, dict):
        seller = seller.get("name") or ""
    addr = (offers.get("availableAtOrFrom") or {}).get("address") if isinstance(offers.get("availableAtOrFrom"), dict) else d.get("address")
    if not (name or price):
        return None
    row = {c: "" for c in MD_COLUMNS}
    row.update({
        "query": query,
        "title": _u(name),
        "price": _num(price),
        "currency": cur,
        "mileage": _num(d.get("mileageFromOdometer")),
        "year": _year(d.get("vehicleModelDate") or d.get("productionDate") or d.get("dateVehicleFirstRegistered")),
        "fuel": _u((d.get("fuelType") or "")),
        "gearbox": _u(d.get("vehicleTransmission") or ""),
        "power": _num(d.get("vehicleEngine", {}).get("enginePower") if isinstance(d.get("vehicleEngine"), dict) else ""),
        "location": _addr(addr),
        "seller": _u(seller),
        "url": d.get("url") or offers.get("url") or "",
        "image": _first(d.get("image")),
        "description": _u(re.sub(r"<[^>]+>", " ", str(d.get("description") or "")))[:500].strip(),
    })
    return row


def _row_from_item(it: dict, query: str) -> dict | None:
    """Row from one element of Mobile.de's embedded search-result JSON."""
    if not isinstance(it, dict):
        return None
    title = it.get("title") or it.get("name") or it.get("modelDescription") or ""
    if isinstance(title, dict):
        title = title.get("text") or title.get("value") or ""
    price = it.get("price") or it.get("priceRating", {}).get("price") if isinstance(it.get("priceRating"), dict) else it.get("price")
    if price is None:
        price = it.get("price") or ""
    cur = "EUR"
    if isinstance(price, dict):
        cur = price.get("currency") or price.get("currencyCode") or cur
    attrs = it.get("attributes") or it.get("vehicleData") or {}
    mileage = it.get("mileage") or (attrs.get("mileage") if isinstance(attrs, dict) else "") or ""
    year = it.get("firstRegistration") or it.get("registrationDate") or (attrs.get("firstRegistration") if isinstance(attrs, dict) else "") or ""
    fuel = it.get("fuel") or it.get("fuelType") or (attrs.get("fuel") if isinstance(attrs, dict) else "") or ""
    gearbox = it.get("gearbox") or it.get("transmission") or ""
    power = it.get("power") or it.get("ps") or ""
    seller = it.get("seller") or it.get("dealer") or {}
    if isinstance(seller, dict):
        seller = seller.get("name") or seller.get("companyName") or ""
    loc = it.get("location") or it.get("city") or ""
    if isinstance(loc, dict):
        loc = _addr(loc)
    url = it.get("url") or it.get("detailPageUrl") or it.get("link") or ""
    if url and url.startswith("/"):
        url = "https://suchen.mobile.de" + url
    imgs = it.get("images") or it.get("previewImage") or it.get("image") or []
    img = ""
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        img = (first.get("url") or first.get("src") or first.get("uri") or "") if isinstance(first, dict) else first
    elif isinstance(imgs, dict):
        img = imgs.get("url") or imgs.get("src") or ""
    elif isinstance(imgs, str):
        img = imgs
    if not (title or price):
        return None
    row = {c: "" for c in MD_COLUMNS}
    row.update({
        "query": query,
        "title": _u(title),
        "price": _num(price),
        "currency": cur,
        "mileage": _num(mileage),
        "year": _year(year),
        "fuel": _u(fuel),
        "gearbox": _u(gearbox),
        "power": _num(power),
        "location": _u(loc),
        "seller": _u(seller),
        "url": url,
        "image": img or "",
    })
    return row


def _looks_like_listing(d: dict) -> bool:
    keys = set(d.keys())
    has_title = bool(keys & {"title", "name", "modelDescription"})
    has_price = bool(keys & {"price", "priceRating"})
    has_vehicle = bool(keys & {"mileage", "firstRegistration", "fuel", "fuelType", "detailPageUrl"})
    return has_title and (has_price or has_vehicle)


def _parse_state(html_text: str, query: str) -> list[dict]:
    out, seen = [], set()
    blobs = []
    for m in re.finditer(r"__NEXT_DATA__[^>]*>(\{.*?\})</script>", html_text, re.S):
        blobs.append(m.group(1))
    for m in re.finditer(r"window\.__(?:INITIAL_STATE|NUXT|PRELOADED_STATE|SEARCH_RESULTS)__\s*=\s*(\{.*?\})\s*[;<]", html_text, re.S):
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
                    if row and row["title"]:
                        key = (row["title"], row["price"], row["url"] or row["mileage"])
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
    types = {"Car", "Vehicle", "Product", "Motorcycle", "Offer", "IndividualProduct"}
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
                        key = (row["title"], row["price"], row["url"] or row["image"])
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    for row in _parse_state(html_text, query):
        key = (row["title"], row["price"], row["url"] or row["mileage"])
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
    return {c: doc.get(c, "") for c in MD_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Mobile.de search/detail URL and store one row per vehicle."""
    from .db import jobs, mobilede_results
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
                await mobilede_results.insert_many(rows)
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
