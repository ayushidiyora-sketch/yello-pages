"""Website enrichment — visit a business's website and pull the extra Outscraper-style
columns we can actually source ourselves: social-media profile links, website meta
(title/description/keywords/generator), tracking-pixel flags, and any emails on the site.

Everything here is best-effort: a site that times out, blocks us, or has no website yields
empty values, never an error. The other Outscraper columns (email validation, whitepages
phone enrichment, company insights, amenities/range) need paid third-party data and stay
empty — there is no free source for them.
"""
import asyncio
import re
import threading

import dns.resolver
import phonenumbers
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from .config import settings

# social platform -> a domain fragment that identifies its profile links
SOCIAL_DOMAINS = {
    "facebook": ["facebook.com", "fb.com"],
    "instagram": ["instagram.com"],
    "linkedin": ["linkedin.com"],
    "tiktok": ["tiktok.com"],
    "medium": ["medium.com"],
    "reddit": ["reddit.com"],
    "skype": ["skype.com", "join.skype.com"],
    "snapchat": ["snapchat.com"],
    "telegram": ["t.me", "telegram.me"],
    "whatsapp": ["wa.me", "whatsapp.com"],
    "twitter": ["twitter.com", "x.com"],
    "vimeo": ["vimeo.com"],
    "youtube": ["youtube.com", "youtu.be"],
    "github": ["github.com"],
    "crunchbase": ["crunchbase.com"],
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_BAD_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".wixpress.com")
# a phone with 7+ digits, optional country code / separators (kept loose; validated later)
PHONE_RE = re.compile(r"\+?\d[\d().\-\s]{6,}\d")
_ROLE_LOCALS = {"info", "sales", "support", "admin", "contact", "office", "help",
                "team", "hello", "enquiries", "inquiries", "noreply", "no-reply", "mail"}
_DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
               "trashmail.com", "yopmail.com", "throwawaymail.com", "getnada.com"}
_REGION_CC = {"us": "US", "au": "AU", "ca": "CA"}

# the keys this module contributes to each business record (always present, even if empty)
ENRICH_KEYS = list(SOCIAL_DOMAINS) + [
    "emails", "emails_status", "website_title", "website_description", "website_keywords",
    "website_generator", "website_has_fb_pixel", "website_has_google_tag",
    "phones_extra", "phones_extra_types", "phone_type", "contact_name", "contact_title",
    "is_public",
]


def empty() -> dict:
    d = {k: None for k in SOCIAL_DOMAINS}
    d.update(emails=[], emails_status=[], website_title=None, website_description=None,
             website_keywords=None, website_generator=None, website_has_fb_pixel=False,
             website_has_google_tag=False, phones_extra=[], phones_extra_types=[],
             phone_type=None, contact_name=None, contact_title=None, is_public=False)
    return d


# ---- free email validation: domain-level (MX) + role/disposable flags (NOT per-mailbox) ----
_mx_cache: dict[str, bool] = {}
_mx_lock = threading.Lock()


def _has_mx(domain: str) -> bool:
    with _mx_lock:
        if domain in _mx_cache:
            return _mx_cache[domain]
    try:
        ok = len(dns.resolver.resolve(domain, "MX", lifetime=5)) > 0
    except Exception:
        ok = False
    with _mx_lock:
        _mx_cache[domain] = ok
    return ok


def validate_email(addr: str) -> tuple[str, str]:
    """Return (status, details). status: invalid_format | disposable | no_mx |
    deliverable_domain. Checks the domain can receive mail + flags role/disposable; does NOT
    confirm the mailbox exists (per-mailbox SMTP probing is unreliable and can blacklist us)."""
    if not addr or not EMAIL_RE.fullmatch(addr):
        return ("invalid_format", "")
    local, _, domain = addr.partition("@")
    domain = domain.lower()
    if domain in _DISPOSABLE:
        return ("disposable", domain)
    if not _has_mx(domain):
        return ("no_mx", domain)
    return ("deliverable_domain", "role_account" if local.lower() in _ROLE_LOCALS else "personal")


_PHONE_TYPES = {
    phonenumbers.PhoneNumberType.FIXED_LINE: "fixed_line",
    phonenumbers.PhoneNumberType.MOBILE: "mobile",
    phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile",
    phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
    phonenumbers.PhoneNumberType.VOIP: "voip",
}


def _phone_type(num: str, region: str | None) -> str | None:
    """Line type (mobile / fixed_line / voip / ...) via libphonenumber, best-effort."""
    if not num:
        return None
    try:
        p = phonenumbers.parse(num, _REGION_CC.get(region or ""))
        if not phonenumbers.is_valid_number(p):
            return None
        return _PHONE_TYPES.get(phonenumbers.number_type(p))
    except Exception:
        return None


# ---- is_public via SEC EDGAR's free public-company list (cached once per process) ----
_PUBLIC_TITLES: set[str] | None = None
_PUBLIC_LOCK = threading.Lock()
_CO_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|llc|llp|lp|ltd|plc|holdings|group|"
    r"the|sa|nv|ag|se|trust)\b\.?", re.I)


def _norm_company(name: str | None) -> str:
    n = _CO_SUFFIX.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def _load_public_titles() -> set[str]:
    """Download SEC's full list of public companies once (normalized titles). SEC requires a
    User-Agent header — without it the request 403s. Any failure leaves the set empty."""
    global _PUBLIC_TITLES
    with _PUBLIC_LOCK:
        if _PUBLIC_TITLES is not None:
            return _PUBLIC_TITLES
    titles: set[str] = set()
    try:
        r = cffi.get("https://www.sec.gov/files/company_tickers.json",
                     headers={"User-Agent": "yellowpages-scraper contact@example.com"},
                     timeout=15, verify=False)
        if r.status_code == 200:
            for v in r.json().values():
                t = _norm_company(v.get("title"))
                if t:
                    titles.add(t)
    except Exception:
        pass
    with _PUBLIC_LOCK:
        _PUBLIC_TITLES = titles
    return titles


def is_public_company(name: str | None) -> bool:
    """True if the business name matches a SEC-registered public company (best-effort; most
    scraped small businesses are not public). Matches the full normalized name or a leading
    whole-word prefix of it (so "Starbucks Coffee" matches the SEC title "Starbucks")."""
    norm = _norm_company(name)
    if not norm:
        return False
    titles = _load_public_titles()
    words = norm.split()
    return any(" ".join(words[:k]) in titles for k in range(len(words), 0, -1))


def _meta(soup, name=None, prop=None):
    el = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
    c = el.get("content") if el else None
    return c.strip() if c else None


def _extract(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out = empty()

    t = soup.find("title")
    out["website_title"] = t.get_text(" ", strip=True) if t else None
    out["website_description"] = _meta(soup, name="description") or _meta(soup, prop="og:description")
    out["website_keywords"] = _meta(soup, name="keywords")
    out["website_generator"] = _meta(soup, name="generator")

    low = html.lower()
    out["website_has_fb_pixel"] = "connect.facebook.net" in low and "fbq(" in low
    out["website_has_google_tag"] = any(s in low for s in (
        "googletagmanager.com/gtag", "googletagmanager.com/gtm", "gtag(", "google-analytics.com/analytics"))

    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]

    # social profile links (first match per platform)
    for key, domains in SOCIAL_DOMAINS.items():
        for h in hrefs:
            hl = h.lower()
            if any(d in hl for d in domains) and h.startswith("http"):
                out[key] = h
                break

    # emails: mailto: links first (most reliable), then any plausible address in the HTML
    emails: list[str] = []
    for h in hrefs:
        if h.lower().startswith("mailto:"):
            e = h[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(e) and e not in emails:
                emails.append(e)
    for e in EMAIL_RE.findall(html):
        el = e.lower()
        if e not in emails and not el.endswith(_BAD_EMAIL_EXT):
            emails.append(e)
    out["emails"] = emails[:3]
    out["emails_status"] = [list(validate_email(e)) for e in out["emails"]]  # domain-level (MX)

    # extra phone numbers: tel: links first, then plausible numbers in the text
    phones: list[str] = []
    for h in hrefs:
        if h.lower().startswith("tel:"):
            t = re.sub(r"[^\d+]", "", h[4:])
            if len(re.sub(r"\D", "", t)) >= 7 and t not in phones:
                phones.append(t)
    for m in PHONE_RE.findall(soup.get_text(" ", strip=True)):
        t = m.strip()
        if 7 <= len(re.sub(r"\D", "", t)) <= 15 and t not in phones:
            phones.append(t)
    out["phones_extra"] = phones[:3]

    # best-effort contact person from the homepage (schema.org Person / author meta)
    person = soup.select_one("[itemtype$='Person'] [itemprop='name'], [itemprop='name'][itemscope]")
    out["contact_name"] = (person.get_text(" ", strip=True) if person else _meta(soup, name="author")) or None
    title_el = soup.select_one("[itemprop='jobTitle']")
    out["contact_title"] = title_el.get_text(" ", strip=True) if title_el else None
    return out


def _fetch_sync(url: str) -> dict:
    if not url or not url.startswith("http"):
        return empty()
    try:
        r = cffi.get(url, impersonate="chrome", timeout=settings.ENRICH_TIMEOUT,
                     verify=False, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return _extract(r.text)
    except Exception:
        pass
    return empty()


def parse_amenities(html: str | None) -> str | None:
    """Pull the Amenities from a YP detail page. US markup is a `.amenities` section with
    `.amenities-info` labels (e.g. 'Wheelchair accessible'); fall back to the section text."""
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    vals = []
    for el in soup.select(".amenities-info"):
        t = el.get_text(" ", strip=True)
        if t and t not in vals:
            vals.append(t)
    if not vals:
        sec = soup.select_one(".amenities")
        if sec:
            t = re.sub(r"^\s*Amenities\s*:?\s*", "", sec.get_text(" ", strip=True)).strip()
            if t:
                vals.append(t)
    return ", ".join(vals) or None


async def enrich_amenities(cards: list[dict], fetch_detail) -> list[dict]:
    """Set each card's `amenities` by fetching its YP detail page (source_url) and parsing
    the amenities section. `fetch_detail` is the region's async detail fetcher. Best-effort
    and bounded; disabled via ENRICH_AMENITIES."""
    for c in cards:
        c.setdefault("amenities", None)
    if not cards or not fetch_detail or not settings.ENRICH_AMENITIES:
        return cards

    sem = asyncio.Semaphore(settings.ENRICH_CONCURRENCY)

    async def one(card: dict):
        url = card.get("source_url")
        if not url:
            return
        async with sem:
            html = await fetch_detail(url)
        card["amenities"] = parse_amenities(html)

    await asyncio.gather(*[one(c) for c in cards])
    return cards


def _type_phones(card: dict, region: str | None):
    """Set line-type for the main phone and each extra phone (libphonenumber)."""
    card["phone_type"] = _phone_type(card.get("phone") or "", region)
    card["phones_extra_types"] = [_phone_type(p, region) for p in (card.get("phones_extra") or [])]


async def enrich_cards(cards: list[dict], region: str | None = None) -> list[dict]:
    """Merge website-enrichment fields into each card in place (concurrently, bounded).
    Cards with no website still get the (empty) keys so every record has the same columns."""
    if not cards:
        return cards
    await asyncio.to_thread(_load_public_titles)  # warm the SEC list once (cached process-wide)
    if not settings.ENRICH:
        for c in cards:
            c.update(empty())
            _type_phones(c, region)
            c["is_public"] = is_public_company(c.get("name"))
        return cards

    sem = asyncio.Semaphore(settings.ENRICH_CONCURRENCY)

    async def one(card: dict):
        async with sem:
            data = await asyncio.to_thread(_fetch_sync, card.get("website") or "")
        card.update(data)
        _type_phones(card, region)
        card["is_public"] = is_public_company(card.get("name"))

    await asyncio.gather(*[one(c) for c in cards])
    return cards
