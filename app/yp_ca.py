"""yellowpages.ca (CA) scraper.

yp.ca uses a different page layout than the US/AU Yellow Pages sites (BEM-style
`listing__*` classes + schema.org itemprops), so it needs its own parser rather than
reusing `yp_us.parse_us_cards`.

Fetching: yellowpages.ca is reachable directly with a real Chrome TLS fingerprint
(curl_cffi) — not geo-blocked — so no proxy pool is needed. A paid PROXY_URL is honored.

Search URL: `/search/si/{page}/{what}/{where}` (page number lives in the path), e.g.
`/search/si/1/Medical+Clinics/Old+Toronto+Toronto+ON`. ~35 listings per page.
"""
import asyncio
import re

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi
from urllib.parse import quote

from .config import settings

BASE_CA = "https://www.yellowpages.ca"


def _get(url: str):
    proxy = settings.PROXY_URL.strip() or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return cffi.get(url, impersonate="chrome", proxies=proxies,
                    timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)


def _is_block(text: str) -> bool:
    # Only the genuine anti-bot block phrase — NOT "captcha"/"cloudflare", which can appear
    # in normal YP page scripts and would false-positive on valid results pages.
    return "you have been blocked" in text.lower()


def _fetch_sync(search: str, location: str, page: int) -> str:
    url = f"{BASE_CA}/search/si/{page}/{quote(search)}/{quote(location)}"
    r = _get(url)
    if r.status_code == 200 and "jsListingName" in r.text:
        return r.text
    # No listings: a valid SERP with zero results (or a 404 no-such-page). Return an empty
    # page so the caller finishes as done(0); only genuine failures (5xx / network / block) raise.
    if r.status_code == 404:
        return ""
    if r.status_code == 200 and not _is_block(r.text):
        return ""
    raise RuntimeError(
        f"yellowpages.ca returned status {r.status_code} for '{search}' in '{location}' (page {page})."
    )


async def fetch_ca_page(search: str, location: str, page: int) -> str:
    return await asyncio.to_thread(_fetch_sync, search, location, page)


def fetch_detail_sync(url: str) -> str | None:
    """Fetch a yellowpages.ca detail page directly (best-effort, for amenities)."""
    if not url:
        return None
    try:
        r = _get(url)
        if r.status_code == 200 and len(r.text) > 5000 and not _is_block(r.text):
            return r.text
    except Exception:
        pass
    return None


def parse_ca_total(html: str) -> int | None:
    """'1,569 results' -> 1569."""
    m = re.search(r"([\d,]+)\s+results?", html, re.I)
    return int(m.group(1).replace(",", "")) if m else None


def _txt(node):
    return node.get_text(" ", strip=True) if node else None


def parse_ca_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for c in soup.select("div.listing"):
        name_el = c.select_one("a.listing__name--link")
        name = _txt(name_el)
        if not name:
            continue

        phone = None
        ph = c.select_one(".mlr__item--phone") or c.select_one("[itemprop='telephone']")
        if ph:
            m = re.search(r"[\d(][\d\-()\s.]{6,}\d", _txt(ph) or "")
            phone = m.group(0).strip() if m else None

        street = _txt(c.select_one("[itemprop='streetAddress']"))
        city = _txt(c.select_one("[itemprop='addressLocality']"))
        region = _txt(c.select_one("[itemprop='addressRegion']"))
        postal = _txt(c.select_one("[itemprop='postalCode']"))

        cats = [a.get_text(" ", strip=True) for a in c.select(".listing__headings a")]
        cat_list = list(dict.fromkeys([x for x in cats if x]))
        category = ", ".join(cat_list) or None

        href = name_el.get("href") if name_el else None
        source_url = (BASE_CA + href.split("?")[0]) if href else None

        web_el = c.select_one("a.mlr__item--website, a.listing__website--link")
        website = web_el.get("href") if web_el else None

        out.append({
            "name": name,
            "phone": phone,
            "category": category,        # joined, for display
            "categories": cat_list,      # list, for the category filter
            "area": street,
            "city": city,
            "state": region,
            "pincode": postal,
            "range": _txt(c.select_one(".price-range")),  # best-effort; usually absent on yp.ca
            "rating": None,
            "reviews_count": None,
            "open_status": None,
            "email": None,               # not exposed on the CA results page
            "website": website,
            "directions": None,
            "image": None,
            "years_in_business": None,
            "description": None,
            "source_url": source_url,
        })
    return out
