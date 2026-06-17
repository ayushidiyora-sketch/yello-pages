"""Amazon Products scraper.

Mirrors the Yellow Pages design: synchronous curl_cffi fetches (real Chrome TLS
fingerprint) wrapped in asyncio.to_thread, every request routed through a proxy IP —
the paid PROXY_URL from .env if set, otherwise the rotating free US-proxy pool that
`yp_us` already warms and maintains. The real machine IP is never used for Amazon.

Input lines may be: a raw ASIN ("B00K0QKBM6"), a product URL (.../dp/ASIN), a search
URL (.../s?k=...), or a plain search keyword. ASIN/product lines yield one product;
search lines yield up to `limit` products (paging as needed).

Each scraped product is shaped to the fixed Outscraper "Amazon Products" column schema
(EXPORT_COLUMNS) so the export/download matches that layout exactly. Detail-table and
"product overview" rows are flattened into details_<key> / overview_<key> columns;
the five largest images become image_1..image_5. Review *content* is never scraped
(only the numeric review count).
"""
import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape as html_unescape
from urllib.parse import quote_plus, urljoin, urlparse, urlencode, parse_qsl

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings
from .db import jobs, products
from . import yp_us

MAX_SEARCH_PAGES = 7            # safety cap on pages fetched per search query
PROBE_TIMEOUT = 12             # per-proxy timeout while trying the pool
SEED_PARAMS = {"search_terms": "Dentists", "geo_location_terms": "New York, NY", "page": "1"}

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_IN_PATH = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})")

# ---- Fixed Outscraper "Amazon Products" column schema (exact order). Every scraped
# row carries all of these keys (empty when the page doesn't expose one). ----
EXPORT_COLUMNS = [
    "name", "asin", "price", "price_parsed", "old_price", "old_price_parsed",
    "strike_price", "price_saving", "currency", "availability", "shipping", "prime",
    "merchant_info", "rating", "reviews", "answered_questions", "categories", "about",
    "description", "coupon_save", "bage", "summary_bage", "short_url", "url", "ref_url",
    "store_title", "store_url",
    "details_assembly_required", "details_best_sellers_rank", "details_brand",
    "details_brand_name", "details_color", "details_connectivity_technology",
    "details_customer_reviews", "details_date_first_available", "details_deck_length",
    "details_deck_width", "details_display_type", "details_folded_size",
    "details_frame_material", "details_grip_type", "details_included_components",
    "details_item_dimensions_lxwxh", "details_item_package_dimensions_l_x_w_x_h",
    "details_item_weight", "details_manufacturer", "details_material",
    "details_maximum_horsepower", "details_maximum_incline_percentage",
    "details_maximum_speed", "details_maximum_weight_recommendation", "details_meter_type",
    "details_model_name", "details_model_year", "details_number_of_items",
    "details_number_of_programs", "details_package_weight", "details_part_number",
    "details_power_source", "details_product_benefits", "details_product_dimensions",
    "details_product_grade", "details_recommended_uses_for_product", "details_screen_size",
    "details_size", "details_special_feature", "details_speed_rating", "details_sport_type",
    "details_style", "details_suggested_users", "details_target_audience",
    "details_warranty_description",
    "image_1", "image_2", "image_3", "image_4", "image_5",
    "overview_assembly_required", "overview_brand", "overview_color",
    "overview_display_type", "overview_included_components", "overview_item_weight",
    "overview_material", "overview_maximum_horsepower", "overview_maximum_incline_percentage",
    "overview_maximum_speed", "overview_power_source", "overview_product_dimensions",
    "overview_product_grade", "overview_recommended_uses_for_product",
    "overview_special_feature", "overview_target_audience",
    "position", "query",
]
_COLSET = set(EXPORT_COLUMNS)

_CUR = {"US$": "USD", "$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY",
        "R$": "BRL", "C$": "CAD", "A$": "AUD", "CA$": "CAD", "AU$": "AUD",
        "zł": "PLN", "kr": "SEK"}

# symbol to render a converted price in the chosen currency
_SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€", "INR": "₹", "JPY": "¥", "BRL": "R$",
           "CAD": "C$", "AUD": "A$", "MXN": "MX$", "AED": "AED ", "SAR": "SAR ",
           "SEK": "SEK ", "PLN": "PLN ", "ZAR": "ZAR "}

# each marketplace's native currency — authoritative (the site fixes it), so it resolves
# the ambiguous "$" (US/CA/AU/MX all use $) and tells the fetcher which currency to expect.
DOMAIN_CURRENCY = {
    "amazon.com": "USD", "amazon.co.uk": "GBP", "amazon.ca": "CAD", "amazon.de": "EUR",
    "amazon.es": "EUR", "amazon.fr": "EUR", "amazon.it": "EUR", "amazon.in": "INR",
    "amazon.nl": "EUR", "amazon.se": "SEK", "amazon.sa": "SAR", "amazon.com.mx": "MXN",
    "amazon.com.br": "BRL", "amazon.co.jp": "JPY", "amazon.pl": "PLN",
    "amazon.com.au": "AUD", "amazon.ae": "AED",
}


def _market_cur(domain: str, parsed: str) -> str:
    """Authoritative currency for a marketplace. amazon.com can be served in a foreign
    currency (proxy geo), so trust the parsed symbol there; every other domain is fixed."""
    if domain == "amazon.com":
        return parsed
    return DOMAIN_CURRENCY.get(domain, parsed)

# approximate USD-based FX rates (fallback if the live fetch fails); refreshed once per run
_FX = {"rates": None}
_FX_FALLBACK = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "INR": 83.0, "JPY": 150.0,
                "BRL": 5.0, "MXN": 17.0, "AUD": 1.5, "CAD": 1.36, "AED": 3.67,
                "SAR": 3.75, "SEK": 10.5, "PLN": 4.0, "ZAR": 18.5}


def _fx_rates() -> dict:
    """USD-based exchange rates, fetched once (through the proxy) and cached; falls back
    to a built-in static table if the live fetch is unavailable."""
    if _FX["rates"]:
        return _FX["rates"]
    rates = dict(_FX_FALLBACK)
    try:
        r = yp_us.pooled_get("https://open.er-api.com/v6/latest/USD", timeout=10)
        if r is not None and r.status_code == 200:
            data = r.json()
            if isinstance(data.get("rates"), dict):
                rates.update({k: v for k, v in data["rates"].items() if isinstance(v, (int, float))})
    except Exception:
        pass
    _FX["rates"] = rates
    return rates


def convert_amount(value, src: str, dst: str):
    """Convert a numeric price string/number from `src` to `dst` currency (best-effort)."""
    if value in ("", None) or not src or not dst or src == dst:
        return value
    rates = _fx_rates()
    if src not in rates or dst not in rates:
        return value
    try:
        return round(float(value) / rates[src] * rates[dst], 2)
    except (ValueError, TypeError, ZeroDivisionError):
        return value


def apply_currency(row: dict, target: str) -> dict:
    """Re-express a row's prices in the user-selected `target` currency (converts the
    numeric value and re-formats the display string). No-op if the source currency of the
    scraped page is unknown or already the target."""
    src = row.get("currency") or ""
    if not target or not src:
        return row
    sym = _SYMBOL.get(target, target + " ")
    for raw_key, num_key in (("price", "price_parsed"), ("old_price", "old_price_parsed")):
        v = convert_amount(row.get(num_key), src, target)
        if v not in ("", None):
            row[num_key] = v
            row[raw_key] = f"{sym}{v}"
    row["strike_price"] = row.get("old_price", "")
    row["currency"] = target
    return row


# ---------------- HTTP through a proxy IP ----------------

def _lang_header(code: str) -> str:
    """'en_US' -> 'en-US,en;q=0.9,en;q=0.8' — an Accept-Language Amazon will honour."""
    bcp = code.replace("_", "-")
    primary = bcp.split("-")[0]
    return f"{bcp},{primary};q=0.9,en;q=0.8"


def _amz_get(url: str, proxy: str | None, timeout: int, lang: str | None = None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = cookies = None
    if lang:
        headers = {"Accept-Language": _lang_header(lang)}
        cookies = {"lc-main": lang}     # Amazon's language-preference cookie
    return cffi.get(url, impersonate="chrome", proxies=proxies, timeout=timeout,
                    verify=False, allow_redirects=True, headers=headers, cookies=cookies)


def _is_blocked(html: str) -> bool:
    """True if Amazon served a robot-check / CAPTCHA page instead of real content."""
    if not html:
        return True
    low = html.lower()
    return ("enter the characters you see below" in low
            or "to discuss automated access" in low
            or "/errors/validatecaptcha" in low
            or "robot check" in low)


def _is_amazon_page(html: str) -> bool:
    """True only for a genuine Amazon page (not an ISP block / junk page from a bad proxy)."""
    if not html or _is_blocked(html):
        return False
    low = html.lower()
    return ('id="producttitle"' in low or 'data-component-type="s-search-result"' in low
            or "nav-logo" in low or "amazon.com" in low[:3000])


def _probe_amazon(px: str, url: str, lang: str | None):
    """Fetch one Amazon URL through a single candidate proxy. Returns (px, html|None)."""
    try:
        r = _amz_get(url, px, PROBE_TIMEOUT, lang)
        if r.status_code < 500 and _is_amazon_page(r.text):
            return px, r.text
    except Exception:
        pass
    return px, None


def _probe_amazon_batch(proxies: list[str], url: str, lang: str | None, stop=None) -> str | None:
    """Probe many proxies CONCURRENTLY against the real URL; return the first that delivers
    a genuine Amazon page. Abandons the rest as soon as one succeeds — or when `stop()` is true."""
    if not proxies:
        return None
    ex = ThreadPoolExecutor(max_workers=20)
    try:
        futs = [ex.submit(_probe_amazon, px, url, lang) for px in proxies]
        for fut in as_completed(futs):
            if stop and stop():
                return None
            px, html = fut.result()
            if html:
                yp_us._mark_good(px)
                return html
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return None


def _fetch_sync(url: str, lang: str | None = None, stop=None, domain: str = "amazon.com") -> str | None:
    """Fetch one Amazon URL through a proxy IP. Paid PROXY_URL first; otherwise the warm
    free US pool (seeded via yp_us if empty). Returns HTML, or None if every proxy was
    blocked/dead, or as soon as `stop()` becomes true. Never falls back to the real IP."""
    if stop and stop():
        return None
    expect = DOMAIN_CURRENCY.get(domain)   # marketplace's native currency (None = no preference)
    paid = settings.PROXY_URL.strip()
    if paid:
        try:
            r = _amz_get(url, paid, settings.REQUEST_TIMEOUT, lang)
            if not _is_blocked(r.text):
                return r.text
        except Exception:
            pass
        return None

    # free pool: reuse the proxies yp_us has already validated as live US exits
    with yp_us._LOCK:
        warm = list(yp_us._GOOD)
    if len(warm) < 4:
        yp_us.ensure_pool(SEED_PARAMS, 8)   # warm a few US proxies first
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)

    # 1) warm pool — prefer a proxy whose exit IP gives Amazon the US marketplace (USD).
    # Some free proxies pass the US check yet still serve a foreign marketplace (ZAR/EUR) —
    # keep the first such page only as a fallback, so we return data but favour US.
    fallback = None
    for px in warm[:12]:
        if stop and stop():
            return None
        try:
            r = _amz_get(url, px, PROBE_TIMEOUT, lang)
            if not _is_amazon_page(r.text) or r.status_code >= 500:
                continue
            # accept a page in the marketplace's native currency (or unknown). For amazon.com
            # this means USD; for .co.uk GBP, .de EUR, etc. A page in some OTHER currency
            # (e.g. .com served as ZAR via proxy geo) is kept only as a fallback.
            if expect is None or _page_currency(r.text) in ("", expect):
                yp_us._mark_good(px)        # right marketplace — keep this proxy hot
                return r.text
            if fallback is None:
                fallback = (px, r.text)     # wrong marketplace — use only if nothing better
        except Exception:
            pass

    if stop and stop():
        return None
    # 2) warm pool exhausted/blocked — probe FRESH free proxies straight against this URL,
    # concurrently, until one delivers a real Amazon page (mirrors the Yellow Pages scraper).
    candidates = [p for p in yp_us._fetch_candidates() if p not in yp_us._GOOD][:150]
    html = _probe_amazon_batch(candidates, url, lang, stop)
    if html:
        return html

    # 3) nothing fresh worked either — use the foreign-marketplace page if we got one.
    if fallback:
        yp_us._mark_good(fallback[0])
        return fallback[1]
    return None


def _page_currency(html: str) -> str:
    """Currency code of the first price on the page ('USD', 'ZAR', ...), or '' if unknown.
    Used to prefer US-marketplace proxy responses."""
    m = re.search(r'class="a-offscreen">([^<]{1,24})<', html or "")
    if m:
        return _money(m.group(1))[2]
    return ""


# ---------------- delivery-location (zip) via Amazon's "glow" flow ----------------

def _glow_token(html: str) -> str | None:
    """Amazon embeds the anti-CSRF token for location changes in the page (HTML-escaped
    inside the location widget's data-a-modal). Pull it out so we can set the delivery zip."""
    if not html:
        return None
    m = re.search(r'"anti-csrftoken-a2z":"([^"]+)"', html_unescape(html))
    return m.group(1) if m else None


def _fetch_zip_sync(url: str, lang: str | None, zipcode: str, domain: str, stop=None) -> str | None:
    """Like _fetch_sync, but first sets the delivery location to `zipcode` on a curl_cffi
    session (so prices/availability/delivery reflect that location), then re-fetches. 3
    requests over ONE proxy (cookies must persist). Best-effort: if the location can't be
    set, returns the normal page so scraping still proceeds. Bails out as soon as `stop()`
    is true. Never uses the real IP."""
    url_base = f"https://www.{domain}"
    h = {"Accept-Language": _lang_header(lang)} if lang else None
    ck = {"lc-main": lang} if lang else None

    def via(px):
        proxies = {"http": px, "https": px} if px else None
        s = cffi.Session(impersonate="chrome")
        r = s.get(url, headers=h, cookies=ck, proxies=proxies, timeout=PROBE_TIMEOUT, verify=False)
        if not _is_amazon_page(r.text):
            return None
        token = _glow_token(r.text)
        if token:
            try:
                # _set_zip must reuse the session's proxy + cookies
                hdr = {"anti-csrftoken-a2z": token, "x-requested-with": "XMLHttpRequest",
                       "content-type": "application/x-www-form-urlencoded;charset=UTF-8"}
                if lang:
                    hdr["Accept-Language"] = _lang_header(lang)
                s.post(f"{url_base}/portal-migration/hz/glow/address-change?actionSource=glow",
                       headers=hdr, proxies=proxies, timeout=PROBE_TIMEOUT, verify=False,
                       data={"locationType": "LOCATION_INPUT", "zipCode": zipcode,
                             "storeContext": "generic", "deviceType": "web",
                             "pageType": "Detail", "actionSource": "glow"})
                r2 = s.get(url, headers=h, proxies=proxies, timeout=PROBE_TIMEOUT, verify=False)
                if _is_amazon_page(r2.text):
                    return r2.text
            except Exception:
                pass
        return r.text   # zip not applied, but we still have a valid page

    if stop and stop():
        return None
    paid = settings.PROXY_URL.strip()
    if paid:
        try:
            html = via(paid)
            if html:
                return html
        except Exception:
            pass
        return None

    with yp_us._LOCK:
        warm = list(yp_us._GOOD)
    if len(warm) < 4:
        yp_us.ensure_pool(SEED_PARAMS, 8)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
    candidates = warm[:8] + [p for p in yp_us._fetch_candidates() if p not in yp_us._GOOD][:60]
    for px in candidates:
        if stop and stop():
            return None
        try:
            html = via(px)
            if html:
                yp_us._mark_good(px)
                return html
        except Exception:
            pass
    return None


async def _fetch(url: str, lang: str | None = None, zipcode: str | None = None,
                 domain: str = "amazon.com", stop=None) -> str | None:
    if zipcode:
        return await asyncio.to_thread(_fetch_zip_sync, url, lang, zipcode, domain, stop)
    return await asyncio.to_thread(_fetch_sync, url, lang, stop, domain)


# ---------------- input classification + URL building ----------------

def classify(line: str, domain: str):
    """Return a spec tuple describing one input line, or None if blank.
    ('asin', asin) | ('product', asin, url) | ('product_url', url) |
    ('search', url) | ('search_kw', keyword)"""
    s = (line or "").strip()
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        m = _ASIN_IN_PATH.search(u.path)
        if m:
            return ("product", m.group(1).upper(), s)
        q = dict(parse_qsl(u.query))
        if u.path.rstrip("/").endswith("/s") or u.path == "/s" or "k" in q:
            return ("search", s)
        return ("product_url", s)
    if ASIN_RE.match(s.upper()):
        return ("asin", s.upper())
    return ("search_kw", s)


def extract_query(value) -> str | None:
    """A single cell/line -> a usable query (URL or ASIN), or None if it's not one
    (skips headers like 'Link'/'no.' and row numbers)."""
    s = str(value).strip() if value is not None else ""
    if not s:
        return None
    low = s.lower()
    if low.startswith(("http://", "https://")) or "amazon." in low:
        return s
    if ASIN_RE.match(s.upper()) and any(c.isdigit() for c in s):
        return s.upper()
    return None


def parse_upload(filename: str, data: bytes) -> list[str]:
    """Extract Amazon product URLs / ASINs from an uploaded CSV / XLSX / TXT / Parquet file.
    Returns a de-duplicated, order-preserving list of query lines."""
    import io
    name = (filename or "").lower()
    values: list = []
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                values.extend(row)
    elif name.endswith(".csv"):
        import csv
        for row in csv.reader(io.StringIO(data.decode("utf-8", "ignore"))):
            values.extend(row)
    elif name.endswith(".parquet"):
        try:
            import pyarrow.parquet as pq
        except ImportError as e:
            raise RuntimeError("Parquet support needs 'pyarrow' installed on the server.") from e
        tbl = pq.read_table(io.BytesIO(data))
        for col in tbl.columns:
            values.extend(col.to_pylist())
    else:  # .txt or unknown -> one query per line
        values.extend(data.decode("utf-8", "ignore").splitlines())

    out, seen = [], set()
    for v in values:
        q = extract_query(v)
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _label(spec) -> str:
    """Human-readable query label stored on each product row."""
    return {"asin": spec[1] if spec[0] == "asin" else "",
            "product": spec[1] if spec[0] == "product" else "",
            }.get(spec[0]) or (spec[-1] if spec[0] in ("product_url", "search") else
                               (spec[1] if spec[0] == "search_kw" else ""))


def _with_page(search_url: str, page: int) -> str:
    if page <= 1:
        return search_url
    u = urlparse(search_url)
    q = dict(parse_qsl(u.query))
    q["page"] = str(page)
    return u._replace(query=urlencode(q)).geturl()


# ---------------- small parse helpers ----------------

def _txt(node):
    return node.get_text(" ", strip=True) if node else None


def _first_text(soup, selectors):
    for sel in selectors:
        t = _txt(soup.select_one(sel))
        if t:
            return t
    return None


def _blank_row() -> dict:
    return {c: "" for c in EXPORT_COLUMNS}


def _money(text):
    """('ZAR274.86' | 'US$14.99' | '$19.99') -> (raw, numeric_value, currency_code)."""
    if not text:
        return "", "", ""
    t = re.sub(r"\s+", " ", text).strip()
    cur = ""
    m = re.match(r"^([A-Z]{3})(?![A-Za-z])", t)  # explicit 3-letter code: USD, ZAR, EUR...
    if m:
        cur = m.group(1)
    else:                                        # currency symbol / short prefix, longest first
        for tok in ("US$", "CA$", "AU$", "R$", "C$", "A$", "$", "£", "€", "₹", "¥", "zł", "kr"):
            if t.startswith(tok):
                cur = _CUR.get(tok, "")
                break
    val = ""
    vm = re.search(r"\d[\d,]*\.?\d*", t)
    if vm:
        val = vm.group(0).replace(",", "")
    return t, val, cur


def _snake(label: str) -> str:
    s = (label or "").replace("‎", "").replace("‏", "").strip().strip(":").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _hires(src: str) -> str:
    """Strip Amazon's size token so a thumbnail URL points at the full-size image."""
    if not src:
        return src
    return re.sub(r"\._[A-Za-z0-9,_-]+_\.", ".", src)


def _detail_pairs(soup) -> dict:
    """Every key/value from Amazon's detail/spec table layouts -> {snake_key: value}."""
    out = {}
    for tbl in soup.select("#productDetails_techSpec_section_1, #productDetails_techSpec_section_2, "
                           "#productDetails_detailBullets_sections1, table.prodDetTable, table.a-keyvalue"):
        for tr in tbl.select("tr"):
            k, v = tr.select_one("th"), tr.select_one("td")
            if k and v:
                key, val = _snake(_txt(k)), re.sub(r"\s+", " ", _txt(v) or "").strip()
                if key and val:
                    out.setdefault(key, val)
    for li in soup.select("#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li"):
        spans = li.select("span.a-list-item > span")
        if len(spans) >= 2:
            key, val = _snake(spans[0].get_text()), re.sub(r"\s+", " ", spans[1].get_text()).strip()
            if key and val:
                out.setdefault(key, val)
    return out


def _overview_pairs(soup) -> dict:
    """The "Product overview" table near the title -> {snake_key: value}."""
    out = {}
    for tr in soup.select("#productOverview_feature_div tr"):
        cells = tr.select("td")
        if len(cells) >= 2:
            key, val = _snake(_txt(cells[0])), re.sub(r"\s+", " ", _txt(cells[1]) or "").strip()
            if key and val:
                out.setdefault(key, val)
    return out


# ---------------- parsers ----------------

def parse_search_cards(html: str, domain: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    base = f"https://www.{domain}"
    out = []
    for c in soup.select('div[data-component-type="s-search-result"]'):
        asin = c.get("data-asin") or None
        title = _txt(c.select_one("h2"))
        if not asin or not title:
            continue
        row = _blank_row()
        a = (c.select_one("h2 a") or c.select_one("a.a-link-normal.s-no-outline")
             or c.select_one("a.a-link-normal"))
        href = a.get("href") if a else None
        ref_url = urljoin(base, href) if href else f"{base}/dp/{asin}"
        row["name"] = title
        row["asin"] = asin
        row["short_url"] = f"{base}/dp/{asin}"
        row["url"] = f"{base}/dp/{asin}"
        row["ref_url"] = ref_url

        row["price"], row["price_parsed"], row["currency"] = _money(_txt(c.select_one(".a-price .a-offscreen")))
        row["currency"] = _market_cur(domain, row["currency"])
        lp = c.select_one(".a-price.a-text-price .a-offscreen") or c.select_one("span[data-a-strike] .a-offscreen")
        row["old_price"], row["old_price_parsed"], _ = _money(_txt(lp))
        row["strike_price"] = row["old_price"]

        ri = c.select_one("span.a-icon-alt")
        if ri:
            m = re.search(r"([\d.]+)\s+out of 5", ri.get_text())
            if m:
                row["rating"] = m.group(1)
        rv = (c.select_one('span[aria-label][class*="s-underline-text"]')
              or c.select_one('.a-size-base.s-underline-text')
              or c.select_one('a[href*="customerReviews"] span'))
        if rv:
            m = re.search(r"[\d,]+", rv.get_text())
            if m:
                row["reviews"] = m.group(0).replace(",", "")

        img = c.select_one("img.s-image")
        if img and img.get("src"):
            row["image_1"] = _hires(img["src"])

        low = str(c).lower()
        row["prime"] = bool(c.select_one("i.a-icon-prime")) or "prime" in low
        if "amazon's choice" in low:
            row["bage"] = "Amazon's Choice"
        elif "best seller" in low:
            row["bage"] = "Best Seller"
        cp = c.select_one(".s-coupon-unclipped, [class*=coupon]")
        row["coupon_save"] = _txt(cp) or ""
        out.append(row)
    return out


def parse_product(html: str, asin: str | None, domain: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    low = html.lower()
    base = f"https://www.{domain}"
    row = _blank_row()

    if not asin:
        m = _ASIN_IN_PATH.search(url)
        asin = m.group(1).upper() if m else ""
    row["asin"] = asin or ""
    row["name"] = _txt(soup.select_one("#productTitle")) or ""
    row["short_url"] = f"{base}/dp/{asin}" if asin else url
    row["url"] = f"{base}/dp/{asin}" if asin else url
    row["ref_url"] = url

    # ---- price / old price / saving / currency ----
    price_raw = _first_text(soup, [
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-offscreen", ".priceToPay .a-offscreen",
        ".a-price .a-offscreen", "#priceblock_ourprice", "#priceblock_dealprice",
        "#priceblock_saleprice",
    ])
    row["price"], row["price_parsed"], row["currency"] = _money(price_raw)
    row["currency"] = _market_cur(domain, row["currency"])
    old_raw = _first_text(soup, [
        ".basisPrice .a-offscreen", "span[data-a-strike='true'] .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen",
        ".a-price.a-text-price .a-offscreen", "#priceblock_listprice",
    ])
    row["old_price"], row["old_price_parsed"], _ = _money(old_raw)
    row["strike_price"] = row["old_price"]
    row["price_saving"] = _first_text(soup, [
        ".savingsPercentage", "#promoPriceBlockMessage .a-color-price",
        ".savingPriceOverride", "#dealBadge_feature_div",
    ]) or ""
    row["coupon_save"] = _first_text(soup, [
        "#couponBadgeRegularVpc", ".couponLabelText", "#promoPriceBlockMessage label",
        "#vpc-coupon-section .a-color-success",
    ]) or ""

    # ---- availability / shipping ----
    row["availability"] = _txt(soup.select_one("#availability")) or ""
    row["shipping"] = _first_text(soup, [
        "#deliveryBlockMessage", "#mir-layout-DELIVERY_BLOCK", "#price-shipping-message",
        "#fast-track-message", "#amazonGlobal_feature_div",
    ]) or ""

    # ---- rating + review count (count only; no review text) + answered questions ----
    rel = soup.select_one("#acrPopover")
    if rel and rel.get("title"):
        m = re.search(r"([\d.]+)", rel["title"])
        if m:
            row["rating"] = m.group(1)
    if not row["rating"]:
        alt = _first_text(soup, ["#averageCustomerReviews span.a-icon-alt", "i.a-icon-star span.a-icon-alt"])
        if alt:
            m = re.search(r"([\d.]+)\s+out of 5", alt)
            if m:
                row["rating"] = m.group(1)
    rc = _txt(soup.select_one("#acrCustomerReviewText"))
    if rc:
        m = re.search(r"[\d,]+", rc)
        row["reviews"] = m.group(0).replace(",", "") if m else ""
    aq = _txt(soup.select_one("#askATFLink, a#askATFLink .a-size-base"))
    if aq:
        m = re.search(r"[\d,]+", aq)
        row["answered_questions"] = m.group(0).replace(",", "") if m else ""

    # ---- prime / badges ----
    row["prime"] = bool(soup.select_one("i.a-icon-prime, #primeBadge_feature_div, "
                                        "#priceBadging_feature_div .a-icon-prime"))
    badges = []
    if soup.select_one(".ac-badge-wrapper, #acBadge_feature_div") or "amazon's choice" in low:
        badges.append("Amazon's Choice")
    if soup.select_one("#zeitgeistBadge_feature_div") or "#1 best seller" in low:
        badges.append("#1 Best Seller")
    if soup.select_one("#climatePledgeFriendly, .climate-pledge-friendly"):
        badges.append("Climate Pledge Friendly")
    row["bage"] = badges[0] if badges else ""
    row["summary_bage"] = badges[1] if len(badges) > 1 else ""

    # ---- merchant / store ----
    seller = _txt(soup.select_one("#sellerProfileTriggerId")) or ""
    ships_from = sold_by = ""
    bb = soup.select_one("#tabular-buybox") or soup.select_one("#buybox") or soup.select_one("#merchant-info")
    if bb:
        btxt = bb.get_text(" ", strip=True)
        ms = re.search(r"Ships from\s+(.+?)\s+Sold by", btxt)
        if ms:
            ships_from = ms.group(1).strip()
        md = re.search(r"Sold by\s+(.+?)(?:\s{2,}|Ships|Returns|$)", btxt)
        if md:
            sold_by = md.group(1).strip()
    row["merchant_info"] = " ".join(p for p in (
        (f"Ships from {ships_from}" if ships_from else ""),
        (f"Sold by {sold_by or seller}" if (sold_by or seller) else ""),
    ) if p) or seller
    by = soup.select_one("#bylineInfo")
    if by:
        row["store_title"] = _txt(by) or ""
        href = by.get("href")
        row["store_url"] = urljoin(base, href) if href else ""

    # ---- categories / about / description ----
    cats = [a.get_text(strip=True) for a in soup.select(
        "#wayfinding-breadcrumbs_feature_div ul li a") if a.get_text(strip=True)]
    row["categories"] = " > ".join(cats)
    about = [t for t in (_txt(li) for li in soup.select(
        "#feature-bullets li, #featurebullets_feature_div li")) if t]
    row["about"] = "\n".join(about[:20])
    row["description"] = (_txt(soup.select_one("#productDescription"))
                          or _txt(soup.select_one("#bookDescription_feature_div")) or "")

    # ---- images -> image_1..image_5 ----
    imgs = []
    main = (soup.select_one("#landingImage") or soup.select_one("#imgTagWrapperId img")
            or soup.select_one("#main-image") or soup.select_one("#imgBlkFront"))
    if main:
        m = main.get("data-old-hires") or main.get("src") or ""
        if not m and main.get("data-a-dynamic-image"):
            try:
                m = next(iter(json.loads(main["data-a-dynamic-image"]).keys()), "")
            except (ValueError, KeyError):
                m = ""
        if m:
            imgs.append(_hires(m))
    for im in soup.select("#altImages li.item img, #imageBlockThumbs img, #ivThumbs img"):
        src = _hires(im.get("src") or "")
        if src and "sprite" not in src and src not in imgs:
            imgs.append(src)
    for i, src in enumerate(imgs[:5], start=1):
        row[f"image_{i}"] = src

    # ---- detail tables -> details_<key> (only columns in the fixed schema) ----
    for key, val in _detail_pairs(soup).items():
        col = "details_" + key
        if col in _COLSET and not row.get(col):
            row[col] = val
    if not row["details_best_sellers_rank"]:
        sr = soup.select_one("#SalesRank")
        if sr:
            row["details_best_sellers_rank"] = re.sub(r"\s+", " ", sr.get_text(" ", strip=True)).strip()

    # ---- product overview -> overview_<key> ----
    for key, val in _overview_pairs(soup).items():
        col = "overview_" + key
        if col in _COLSET and not row.get(col):
            row[col] = val

    return row


# ---------------- persistence ----------------

async def _save(job_id: str, items: list[dict], seen: set, query: str = "",
                position_start: int = 0) -> int:
    added = 0
    for it in items:
        key = it.get("asin") or it.get("url") or it.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        it["query"] = query
        it["position"] = position_start + added + 1
        it["job_id"] = job_id            # stored for lookups; excluded from the export shape
        it["scraped_at"] = datetime.utcnow()
        try:
            await products.insert_one(it)
            added += 1
        except Exception:
            pass
    return added


def to_export(doc: dict) -> dict:
    """Return the doc as the fixed Outscraper column schema, in order (drops internals)."""
    return {c: doc.get(c, "") for c in EXPORT_COLUMNS}


async def export_products(job_id: str) -> str:
    os.makedirs("exports", exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("exports", f"amazon_{job_id[:8]}_{ts}.json")
    rows = []
    async for doc in products.find({"job_id": job_id}, {"_id": 0}):
        rows.append(to_export(doc))
    rows.sort(key=lambda r: r.get("position") or 0)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    return path


# ---------------- run loop ----------------

async def run_amazon_scrape(job_id: str, queries: list[str], domain: str,
                            postcode: str | None, language: str | None,
                            currency: str | None, limit: int = 1):
    """Background task: scrape each input line through a proxy IP, inserting products live.
    Honors `limit` per search query and a cooperative Stop request."""
    from .scraper import STOP_REQUESTS  # shared cooperative-cancel set

    total = 0
    seen: set = set()
    domain = (domain or "amazon.com").strip().lstrip(".") or "amazon.com"
    lang = (language or "").strip() or None        # Accept-Language / lc-main for localized content
    target_cur = (currency or "").strip() or None  # convert scraped prices into this currency
    zipc = (postcode or "").strip() or None        # set Amazon delivery location to this zip

    def stopped() -> bool:
        return job_id in STOP_REQUESTS

    async def finish(status: str):
        STOP_REQUESTS.discard(job_id)
        export_path = await export_products(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": status, "total_scraped": total,
            "finished_at": datetime.utcnow(), "export_path": export_path,
        }})

    try:
        specs = [s for s in (classify(q, domain) for q in queries) if s]
        if not specs:
            await finish("done"); return

        # so all traffic uses a proxy IP, ensure the free pool has a few warm US exits
        if not settings.PROXY_URL.strip():
            await asyncio.to_thread(yp_us.ensure_pool, SEED_PARAMS, 8)
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "amazon-free-pool"}})
        else:
            await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": "amazon-paid-proxy"}})

        # warm the FX table once up front so per-product conversion needs no network
        if target_cur:
            await asyncio.to_thread(_fx_rates)

        for spec in specs:
            if stopped():
                await finish("stopped"); return
            kind = spec[0]
            label = _label(spec)

            if kind in ("asin", "product", "product_url"):
                if kind == "asin":
                    asin, url = spec[1], f"https://www.{domain}/dp/{spec[1]}"
                elif kind == "product":
                    asin, url = spec[1], spec[2]
                else:
                    asin, url = None, spec[1]
                html = await _fetch(url, lang, zipc, domain, stopped)
                if stopped():                       # Stop pressed during the fetch — don't save it
                    await finish("stopped"); return
                if html:
                    p = parse_product(html, asin, domain, url)
                    if p.get("name"):
                        apply_currency(p, target_cur)
                        total += await _save(job_id, [p], seen, query=label, position_start=total)
            else:  # search / search_kw
                search_url = spec[1] if kind == "search" else f"https://www.{domain}/s?k={quote_plus(spec[1])}"
                got = 0
                page = 1
                while got < limit and page <= MAX_SEARCH_PAGES:
                    if stopped():
                        await finish("stopped"); return
                    html = await _fetch(_with_page(search_url, page), lang, zipc, domain, stopped)
                    if stopped():                   # Stop pressed during the fetch — don't save it
                        await finish("stopped"); return
                    if not html:
                        break
                    cards = parse_search_cards(html, domain)
                    if not cards:
                        break
                    for card in cards:
                        apply_currency(card, target_cur)
                    add = await _save(job_id, cards[:limit - got], seen,
                                      query=label, position_start=total)
                    total += add
                    got += add
                    page += 1
                    if add == 0:
                        break

            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await finish("stopped" if stopped() else "done")
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow(),
        }})
