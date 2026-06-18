"""eBay Products scraper.

Same design as the Amazon Products scraper (app/amazon.py): synchronous curl_cffi fetches
(real Chrome TLS fingerprint) wrapped in asyncio.to_thread, every request routed through a
proxy IP (paid PROXY_URL, else the warm free pool). eBay item/search pages are static HTML,
so no headless browser is needed.

Input lines: an eBay item id (digits), an item URL (.../itm/ID), a search URL (.../sch/...),
or a plain search keyword. Item lines yield one product; search lines yield up to `limit`.
Country selects the eBay marketplace domain; postcode is used as the search ship-to location.
"""
import asyncio
import json
import os
import re
from datetime import datetime
from urllib.parse import quote_plus, urlencode, urljoin, urlparse, parse_qsl

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings
from .db import jobs, ebay_products
from . import amazon, yp_us

_HDRS = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}

MAX_SEARCH_PAGES = 5
PROBE_TIMEOUT = 12

# Country (ISO-2) -> eBay marketplace domain; everything else falls back to ebay.com
EBAY_DOMAIN = {
    "US": "ebay.com", "GB": "ebay.co.uk", "DE": "ebay.de", "FR": "ebay.fr", "IT": "ebay.it",
    "ES": "ebay.es", "CA": "ebay.ca", "AU": "ebay.com.au", "AT": "ebay.at", "BE": "ebay.be",
    "CH": "ebay.ch", "IE": "ebay.ie", "NL": "ebay.nl", "PL": "ebay.pl", "HK": "ebay.com.hk",
    "MY": "ebay.com.my", "PH": "ebay.ph", "SG": "ebay.com.sg",
}

EBAY_COLUMNS = [
    "item_id", "title", "price", "price_parsed", "currency", "condition", "availability",
    "sold", "shipping", "item_location", "returns", "seller_name", "seller_feedback_percent",
    "seller_reviews", "brand", "type", "image", "images", "url", "country", "postcode",
    "position", "query",
]

_ITEM_RE = re.compile(r"^\d{11,13}$")
_ITEM_IN_URL = re.compile(r"/itm/(?:[^/]+/)?(\d{11,13})")


def domain_for(country: str) -> str:
    return EBAY_DOMAIN.get((country or "US").upper(), "ebay.com")


def _is_ebay_page(html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    if "pardon our interruption" in low or "/splashui/captcha" in low or "error page | ebay" in low:
        return False
    # require real results/item markup (the 403 error page has none of these)
    return "s-card" in html or "x-item-title" in html or "srp-river-results" in low


def _session_get(url: str, proxy: str | None) -> str | None:
    """eBay blocks /sch/ and /itm/ unless cookies set on the homepage are present. So use a
    session: GET the homepage (warm cookies), then GET the target with a Referer. One proxy
    for both (cookies must persist). Returns HTML or None."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    base = "https://" + urlparse(url).netloc
    try:
        s = cffi.Session(impersonate="chrome")
        s.get(base + "/", headers=_HDRS, proxies=proxies, timeout=PROBE_TIMEOUT, verify=False)
        r = s.get(url, headers={**_HDRS, "Referer": base + "/"},
                  proxies=proxies, timeout=PROBE_TIMEOUT, verify=False)
        if r.status_code < 500 and _is_ebay_page(r.text):
            return r.text
    except Exception:
        pass
    return None


def _fetch_sync(url: str) -> str | None:
    """Fetch one eBay URL through a proxy IP (cookie-warmed session). Paid PROXY_URL first;
    else the warm free pool, then fresh candidates. None if all blocked. Never the real IP."""
    paid = settings.PROXY_URL.strip()
    if paid:
        return _session_get(url, paid)

    with yp_us._LOCK:
        warm = list(yp_us._GOOD)
    if len(warm) < 4:
        yp_us.ensure_pool(amazon.SEED_PARAMS, 8)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
    for px in warm[:8]:
        html = _session_get(url, px)
        if html:
            yp_us._mark_good(px)
            return html
    # warm pool blocked — probe fresh candidates, concurrently
    candidates = [p for p in yp_us._fetch_candidates() if p not in yp_us._GOOD][:60]
    return _probe_batch(candidates, url)


def _probe_batch(proxies: list[str], url: str) -> str | None:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not proxies:
        return None
    ex = ThreadPoolExecutor(max_workers=16)
    try:
        futs = {ex.submit(_session_get, url, px): px for px in proxies}
        for fut in as_completed(futs):
            html = fut.result()
            if html:
                yp_us._mark_good(futs[fut])
                return html
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return None


async def _fetch(url: str) -> str | None:
    return await asyncio.to_thread(_fetch_sync, url)


# ---------------- input classification ----------------

def classify(line: str, domain: str):
    """('item', item_id, url) | ('search', url) | ('search_kw', kw) | None."""
    s = (line or "").strip()
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        m = _ITEM_IN_URL.search(s)
        if m:
            return ("item", m.group(1), s)
        u = urlparse(s)
        q = dict(parse_qsl(u.query))
        if "/sch/" in u.path or "_nkw" in q:
            return ("search", s)
        return ("item", "", s)   # some other eBay product URL — fetch as-is
    if _ITEM_RE.match(s):
        return ("item", s, f"https://www.{domain}/itm/{s}")
    return ("search_kw", s)


def _with_page(search_url: str, page: int, postcode: str | None) -> str:
    u = urlparse(search_url)
    q = dict(parse_qsl(u.query))
    if page > 1:
        q["_pgn"] = str(page)
    if postcode:
        q["_stpos"] = postcode
    return u._replace(query=urlencode(q)).geturl()


# ---------------- parse helpers ----------------

def _txt(node):
    return node.get_text(" ", strip=True) if node else None


# trailing tooltip/marketing text eBay appends inside label-value cells — cut it off
_VALUE_NOISE = ("Shop with confidence", "Estimated delivery dates", "Save on combined",
                "Import fees Customs", "Opens in a new", "opens in a new", "See terms",
                "Delivery time is estimated", "for more information", "qualifying purchases")


def _clean_value(v: str) -> str:
    if not v:
        return ""
    for n in _VALUE_NOISE:
        i = v.find(n)
        if i > 0:
            v = v[:i]
    return re.sub(r"\s+", " ", v).strip(" .|")


def _money(text):
    if not text:
        return "", "", ""
    t = re.sub(r"\s+", " ", text).strip()
    cur = ""
    cm = re.match(r"^\s*([A-Z]{2,3}|[^\d\s.,]{1,3})", t)
    if cm:
        tok = cm.group(1).strip()
        cur = amazon._CUR.get(tok) or (tok.upper() if tok.isalpha() and len(tok) == 3 else "")
    val = ""
    vm = re.search(r"\d[\d,]*\.?\d*", t)
    if vm:
        val = vm.group(0).replace(",", "")
    return t, val, cur


def _blank_row(country, postcode):
    r = {c: "" for c in EBAY_COLUMNS}
    r["images"] = []
    r["country"] = country or ""
    r["postcode"] = postcode or ""
    return r


def parse_item(html: str, item_id: str, domain: str, url: str, country: str, postcode: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    base = f"https://www.{domain}"
    row = _blank_row(country, postcode)
    if not item_id:
        m = _ITEM_IN_URL.search(url)
        item_id = m.group(1) if m else ""
    row["item_id"] = item_id
    row["url"] = url

    row["title"] = (_txt(soup.select_one("h1.x-item-title__mainTitle .ux-textspans"))
                    or _txt(soup.select_one(".x-item-title__mainTitle"))
                    or _txt(soup.select_one("h1")) or "")
    price_raw = (_txt(soup.select_one(".x-price-primary .ux-textspans"))
                 or _txt(soup.select_one('[data-testid="x-price-primary"]'))
                 or _txt(soup.select_one("#prcIsum")) or "")
    row["price"], row["price_parsed"], row["currency"] = _money(price_raw)

    row["condition"] = (_txt(soup.select_one(".x-item-condition-text .ux-textspans"))
                        or _txt(soup.select_one('[data-testid="ux-item-condition"]')) or "")
    row["availability"] = _txt(soup.select_one('.x-quantity__availability .ux-textspans')) or ""
    sold = _txt(soup.select_one('.x-quantity__availability, [data-testid="x-quantity"]')) or ""
    sm = re.search(r"([\d,]+)\s+sold", sold, re.I)
    if sm:
        row["sold"] = sm.group(1).replace(",", "")

    # Shipping / Delivery / Returns / Brand / specifics all live in .ux-labels-values rows
    # (label -> value). Collect them all, then map by label.
    labels = {}
    for row_el in soup.select(".ux-labels-values"):
        lab = _txt(row_el.select_one(".ux-labels-values__labels"))
        val = row_el.select_one(".ux-labels-values__values")
        if lab and val:
            labels.setdefault(lab.rstrip(":").strip().lower(), _clean_value(val.get_text(" ", strip=True)))
    row["shipping"] = labels.get("shipping", "")
    if labels.get("delivery"):   # fold the delivery estimate into shipping so nothing is lost
        row["shipping"] = (row["shipping"] + " | Delivery: " + labels["delivery"]).strip(" |")
    row["returns"] = labels.get("returns", "")
    row["brand"] = labels.get("brand", "")
    row["type"] = labels.get("type", "")
    if not row["condition"]:
        row["condition"] = labels.get("condition", "")
    # "Located in: City, State, Country" — usually inside the shipping value
    loc = re.search(r"Located in:?\s*([^|]+?)(?:\s+(?:Delivery|Returns|Shop|Import)\b|$)", row["shipping"])
    if not loc:
        loc = re.search(r"Located in:?\s*(.+)", labels.get("shipping", ""))
    if loc:
        row["item_location"] = loc.group(1).strip(" .")

    row["seller_name"] = (_txt(soup.select_one('.x-sellercard-atf__info__about-seller .ux-textspans'))
                          or _txt(soup.select_one('.ux-seller-section__item--seller a span'))
                          or _txt(soup.select_one('[data-testid="str-title"] a span')) or "")
    fb = _txt(soup.select_one('.x-sellercard-atf__data-item, '
                              '.ux-seller-section__item--feedback, '
                              '[data-testid="x-sellercard-atf__data-item"]')) or ""
    fm = re.search(r"([\d.]+)\s*%", fb)
    if fm:
        row["seller_feedback_percent"] = fm.group(1)
    rm = re.search(r"\(([\d,]+)\)", fb)
    if rm:
        row["seller_reviews"] = rm.group(1).replace(",", "")

    img = (soup.select_one('.ux-image-carousel-item img') or soup.select_one('#icImg')
           or soup.select_one('img[data-testid="ux-image-carousel-item"]'))
    if img:
        row["image"] = img.get("src") or img.get("data-src") or ""
    imgs = []
    for im in soup.select('.ux-image-grid img, .ux-image-carousel-item img'):
        src = im.get("src") or im.get("data-src")
        if src and src not in imgs:
            imgs.append(src)
    row["images"] = imgs
    if not row["image"] and imgs:
        row["image"] = imgs[0]
    return row


def parse_search(html: str, domain: str, country: str, postcode: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    # eBay now renders search results as li.s-card (older layout used li.s-item)
    for c in soup.select("li.s-card, li.s-item"):
        a = c.select_one("a.s-card__link") or c.select_one("a.s-item__link") or c.select_one("a")
        href = a.get("href") if a else None
        title = (_txt(c.select_one(".s-card__title")) or _txt(c.select_one(".s-item__title")))
        if title:
            title = re.sub(r"\s*Opens in a new window or tab\s*$", "", title).strip()
        if not href or not title or title.strip().lower() == "shop on ebay":
            continue
        row = _blank_row(country, postcode)
        m = _ITEM_IN_URL.search(href)
        row["item_id"] = c.get("data-listingid") or (m.group(1) if m else "")
        row["title"] = title
        row["url"] = href.split("?")[0]
        row["price"], row["price_parsed"], row["currency"] = _money(
            _txt(c.select_one(".s-card__price")) or _txt(c.select_one(".s-item__price")))
        row["condition"] = (_txt(c.select_one(".s-card__subtitle"))
                            or _txt(c.select_one(".s-item__subtitle, .SECONDARY_INFO")) or "")
        # secondary attribute rows hold shipping / location / sold — scan their text
        attrs = " · ".join(t for t in (_txt(e) for e in c.select(
            ".s-card__attribute-row, .s-card__caption, .s-item__shipping, .s-item__location")) if t)
        sm = re.search(r"([\d,]+)\+?\s+sold", attrs, re.I)
        if sm:
            row["sold"] = sm.group(1).replace(",", "")
        ship = re.search(r"(Free (?:shipping|delivery)|[+]?[$£€][\d.,]+\s*(?:shipping|delivery)?)", attrs, re.I)
        if ship:
            row["shipping"] = ship.group(0)
        loc = re.search(r"from\s+([A-Za-z ,]+)$", attrs)
        if loc:
            row["item_location"] = loc.group(1).strip()
        img = c.select_one("img.s-card__image, .s-item__image img")
        if img:
            src = img.get("src") or img.get("data-defer-load") or img.get("data-src")
            if src and "ir.ebaystatic.com" not in src:   # skip the placeholder image
                row["image"] = src
                row["images"] = [src]
        out.append(row)
    return out


# ---------------- persistence ----------------

async def _save(job_id, items, seen, query, position_start):
    added = 0
    for it in items:
        key = it.get("item_id") or it.get("url") or it.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        it["query"] = query
        it["position"] = position_start + added + 1
        it["job_id"] = job_id
        it["scraped_at"] = datetime.utcnow()
        try:
            await ebay_products.insert_one(it)
            added += 1
        except Exception:
            pass
    return added


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in EBAY_COLUMNS}


async def export_products(job_id: str) -> str:
    os.makedirs("exports", exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("exports", f"ebay_{job_id[:8]}_{ts}.json")
    rows = [to_export(d) async for d in ebay_products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    return path


# ---------------- run loop ----------------

async def run_ebay_scrape(job_id: str, queries: list[str], country: str,
                          postcode: str | None, limit: int = 20):
    from .scraper import STOP_REQUESTS
    total = 0
    seen: set = set()
    domain = domain_for(country)
    pc = (postcode or "").strip() or None

    def stopped():
        return job_id in STOP_REQUESTS

    async def finish(status):
        STOP_REQUESTS.discard(job_id)
        path = await export_products(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": status, "total_scraped": total,
            "finished_at": datetime.utcnow(), "export_path": path,
        }})

    try:
        specs = [s for s in (classify(q, domain) for q in queries) if s]
        if not specs:
            await finish("done"); return
        if not settings.PROXY_URL.strip():
            await asyncio.to_thread(yp_us.ensure_pool, amazon.SEED_PARAMS, 8)
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "ebay-free-pool"}})
        else:
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "ebay-paid-proxy"}})

        for spec in specs:
            if stopped():
                await finish("stopped"); return
            kind = spec[0]
            if kind == "item":
                item_id, url = spec[1], spec[2]
                html = await _fetch(url)
                if stopped():
                    await finish("stopped"); return
                if html:
                    p = parse_item(html, item_id, domain, url, country, pc or "")
                    if p.get("title"):
                        total += await _save(job_id, [p], seen, spec[1] or url, total)
            else:
                search_url = spec[1] if kind == "search" else f"https://www.{domain}/sch/i.html?_nkw={quote_plus(spec[1])}"
                got, page = 0, 1
                while got < limit and page <= MAX_SEARCH_PAGES:
                    if stopped():
                        await finish("stopped"); return
                    html = await _fetch(_with_page(search_url, page, pc))
                    if stopped():
                        await finish("stopped"); return
                    if not html:
                        break
                    cards = parse_search(html, domain, country, pc or "")
                    if not cards:
                        break
                    add = await _save(job_id, cards[:limit - got], seen,
                                      spec[1] if kind == "search_kw" else search_url, total)
                    total += add; got += add; page += 1
                    if add == 0:
                        break
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await finish("stopped" if stopped() else "done")
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow(),
        }})
