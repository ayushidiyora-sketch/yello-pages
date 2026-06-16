"""yellowpages.com.au (AU) scraper.

The AU site is the same Yellow Pages (Thryv) codebase as the US one, so the result-card
HTML is nearly identical — we reuse the US parser (`yp_us.parse_us_cards` /
`parse_us_total`) and only fix the two AU differences: the link domain (.com.au) and the
address format (4-digit postcode as `City, STATE 1234`).

Fetching differs from the US: yellowpages.com.au is reachable directly (curl_cffi with a
real Chrome TLS fingerprint) — it is NOT geo-blocked the way the US site is, so no proxy
pool is needed. A paid PROXY_URL is still honored if set.

Search URL: the canonical results page is `/{location-slug}/{search-slug}` with `?page=N`
for pagination. The `/find/{clue}/{location}` endpoint is slug-forgiving and 302-redirects
to the canonical page — we use it as a page-1 fallback when our slug guess misses.
"""
import asyncio
import re
from urllib.parse import quote

from curl_cffi import requests as cffi

from .config import settings
from . import yp_us

BASE_AU = "https://www.yellowpages.com.au"


def _slug(s: str) -> str:
    """'Melbourne, VIC' -> 'melbourne-vic', 'Restaurants' -> 'restaurants'."""
    return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")


def _canonical_url(search: str, location: str, page: int) -> str:
    url = f"{BASE_AU}/{_slug(location)}/{_slug(search)}"
    return url + (f"?page={page}" if page and page > 1 else "")


def _get(url: str):
    proxy = settings.PROXY_URL.strip()
    if proxy:
        return cffi.get(url, impersonate="chrome", proxies={"http": proxy, "https": proxy},
                        timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
    # no paid proxy -> route through the free pool so AU traffic never uses the real IP
    from . import yp_us
    r = yp_us.pooled_get(url, timeout=settings.REQUEST_TIMEOUT)
    if r is None:
        raise RuntimeError(f"no free proxy delivered {url}")
    return r


def _is_block(text: str) -> bool:
    # Only the genuine anti-bot block phrase — NOT "captcha"/"cloudflare", which appear in
    # normal YP page scripts and would false-positive on valid results pages.
    return "you have been blocked" in text.lower()


def _fetch_sync(search: str, location: str, page: int) -> str:
    r = _get(_canonical_url(search, location, page))
    if r.status_code == 200 and "business-name" in r.text:
        return r.text
    # page-1 fallback: the /find/ endpoint tolerates loose slugs and redirects to canonical
    if page <= 1:
        r2 = _get(f"{BASE_AU}/find/{quote(search)}/{quote(location)}")
        if r2.status_code == 200 and "business-name" in r2.text:
            return r2.text
    # No listings: a valid-but-empty results page. AU usually 404s a no-match (a 404 is not
    # a block — it just means no such results page), and an empty 200 SERP is also fine.
    # Return an empty page so the caller finishes as done(0); only genuine failures
    # (5xx / network / block) raise.
    if r.status_code == 404:
        return ""
    if r.status_code == 200 and not _is_block(r.text):
        return ""
    raise RuntimeError(
        f"yellowpages.com.au returned status {r.status_code} for '{search}' in '{location}' (page {page})."
    )


async def fetch_au_page(search: str, location: str, page: int) -> str:
    return await asyncio.to_thread(_fetch_sync, search, location, page)


def fetch_detail_sync(url: str) -> str | None:
    """Fetch a yellowpages.com.au detail page directly (best-effort, for amenities)."""
    if not url:
        return None
    try:
        r = _get(url)
        if r.status_code == 200 and len(r.text) > 5000 and not _is_block(r.text):
            return r.text
    except Exception:
        pass
    return None


def parse_au_total(html: str) -> int | None:
    """Reuse the US 'Showing 1-30 of N' parser — the AU page uses the same markup."""
    return yp_us.parse_us_total(html)


# AU locality is "Suburb, STATE 1234" (4-digit postcode), e.g. "Carnegie, VIC 3163".
_AU_LOCALITY = re.compile(r"^(.*),\s*([A-Za-z]{2,3})\s+(\d{3,4})$")


def parse_au_cards(html: str) -> list[dict]:
    """Parse with the shared US card parser, then apply the two AU-specific fixups:
    correct the link domain to .com.au and split the AU postcode/state out of the locality."""
    cards = yp_us.parse_us_cards(html)
    for c in cards:
        # the US parser hardcodes the .com domain when resolving relative links
        for k in ("source_url", "directions"):
            if c.get(k):
                c[k] = c[k].replace("https://www.yellowpages.com/", BASE_AU + "/")
        # re-split the locality the US parser left whole (its 5-digit ZIP regex misses AU)
        loc = c.get("city")
        if loc:
            m = _AU_LOCALITY.match(loc)
            if m:
                c["city"] = m.group(1).strip()
                c["state"] = m.group(2).upper()
                c["pincode"] = m.group(3)
    return cards
