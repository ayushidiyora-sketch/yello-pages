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
from urllib.parse import urljoin

import phonenumbers
from bs4 import BeautifulSoup

from .config import settings
from . import whitepages, yp_us

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
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com",
                        "email.com", "yourdomain.com", "yourcompany.com", "sentry.io",
                        "sentry-next.wixpress.com", "test.com", "company.com"}


def _real_email(e: str) -> bool:
    el = e.lower()
    if el.endswith(_BAD_EMAIL_EXT):
        return False
    dom = el.split("@")[-1]
    return dom not in _PLACEHOLDER_DOMAINS and not el.startswith(("your", "name@", "email@"))


def _emails_from(html: str, hrefs: list) -> list:
    """mailto: links first (most reliable), then plausible addresses in the HTML; placeholders
    and asset filenames filtered out."""
    out = []
    for h in hrefs:
        if h.lower().startswith("mailto:"):
            e = h[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(e) and _real_email(e) and e not in out:
                out.append(e)
    for e in EMAIL_RE.findall(html):
        if e not in out and _real_email(e):
            out.append(e)
    return out
# a phone with 7+ digits, optional country code / separators (kept loose; validated later)
PHONE_RE = re.compile(r"\+?\d[\d().\-\s]{6,}\d")
_ROLE_LOCALS = {"info", "sales", "support", "admin", "contact", "office", "help",
                "team", "hello", "enquiries", "inquiries", "noreply", "no-reply", "mail",
                "booking", "bookings", "reservations", "events", "billing", "accounts",
                "jobs", "careers", "hr", "marketing", "press", "media", "webmaster", "service"}
# job titles to look for near a person's name on about/team pages
_TITLES = ["Executive Chef", "Chef", "Owner", "Co-Owner", "Founder", "Co-Founder",
           "President", "Vice President", "CEO", "CFO", "COO", "CTO", "Managing Director",
           "Director", "General Manager", "Manager", "Partner", "Principal", "Proprietor",
           "Head Chef", "Sommelier"]
_TITLE_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in _TITLES) + r")\b", re.I)
# employee count: "team of 20", "50 employees", "over 100 staff", "25 team members"
_EMP_RE = re.compile(
    r"(?:team of|staff of|over|more than|about|approximately|employs|employing)?\s*"
    r"([\d,]{1,7})\+?\s*(?:full-time\s+)?(?:employees|staff members|staff|team members|"
    r"people on (?:our|the) team)", re.I)
# pages likely to list staff + contact details
_TEAM_HINTS = ("about", "team", "staff", "our-people", "our-team", "leadership", "contact",
               "meet", "people", "management")
_DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
               "trashmail.com", "yopmail.com", "throwawaymail.com", "getnada.com"}
_REGION_CC = {"us": "US", "au": "AU", "ca": "CA"}

# the keys this module contributes to each business record (always present, even if empty)
ENRICH_KEYS = list(SOCIAL_DOMAINS) + [
    "emails", "emails_status", "website_title", "website_description", "website_keywords",
    "website_generator", "website_has_fb_pixel", "website_has_google_tag",
    "phones_extra", "phones_extra_types", "phone_type", "contact_name", "contact_title",
    "is_public", "wp_name", "wp_address", "phones_extra_wp", "emails_persons", "employees",
]


def empty() -> dict:
    d = {k: None for k in SOCIAL_DOMAINS}
    d.update(emails=[], emails_status=[], website_title=None, website_description=None,
             website_keywords=None, website_generator=None, website_has_fb_pixel=False,
             website_has_google_tag=False, phones_extra=[], phones_extra_types=[],
             phone_type=None, contact_name=None, contact_title=None, is_public=False,
             wp_name=None, wp_address=None, phones_extra_wp=[], emails_persons=[], employees=None)
    return d


# ---- free email validation: domain-level (MX) + role/disposable flags (NOT per-mailbox) ----
_mx_cache: dict[str, bool] = {}
_mx_lock = threading.Lock()


def _has_mx(domain: str) -> bool:
    with _mx_lock:
        if domain in _mx_cache:
            return _mx_cache[domain]
    ok = False
    try:
        # DNS-over-HTTPS through the proxy, so the lookup never uses the real IP
        r = yp_us.pooled_get(f"https://dns.google/resolve?name={domain}&type=MX", timeout=8)
        if r is not None and r.status_code == 200:
            ok = any(a.get("type") == 15 for a in (r.json().get("Answer") or []))  # 15 = MX
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
        r = yp_us.pooled_get("https://www.sec.gov/files/company_tickers.json", timeout=15)
        if r is not None and r.status_code == 200:
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


def _name_from_email(email: str) -> tuple:
    """Derive (full, first, last) from an email's local-part. 'greg.azzollini@x' ->
    ('Greg Azzollini','Greg','Azzollini'); role accounts (info/sales/...) -> (None,None,None)."""
    local = (email or "").split("@")[0]
    if not local or local.lower() in _ROLE_LOCALS:
        return (None, None, None)
    parts = [p for p in re.split(r"[._\-]+", local) if p and not p.isdigit() and p.isalpha()]
    if len(parts) >= 2:
        first, last = parts[0].title(), " ".join(p.title() for p in parts[1:])
        return (f"{first} {last}", first, last)
    if len(parts) == 1 and len(parts[0]) >= 3:
        return (parts[0].title(), parts[0].title(), None)  # first name only
    return (None, None, None)


def _persons_from_emails(emails: list, contact_name, contact_title) -> list:
    """One person dict per email (aligned). Name from the email local-part; for email_1 fall
    back to the homepage schema.org contact when the address itself has no name."""
    persons = []
    for i, e in enumerate(emails):
        full, first, last = _name_from_email(e)
        if i == 0 and not full and contact_name:
            full = contact_name
            bits = contact_name.split()
            first, last = bits[0], " ".join(bits[1:]) or None
        persons.append({"full_name": full, "first_name": first, "last_name": last,
                        "title": (contact_title if i == 0 else None), "phone": None})
    return persons


def _deep_team(soup, base_url: str, data: dict):
    """Fetch up to 2 about/team/contact pages; fill employee count + per-person title/phone."""
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        h = a["href"]
        blob = (h + " " + a.get_text(" ", strip=True)).lower()
        if any(k in blob for k in _TEAM_HINTS):
            full = urljoin(base_url, h)
            if full.startswith("http") and full not in seen:
                seen.add(full); links.append(full)
        if len(links) >= 2:
            break

    text = ""
    for url in links:
        try:
            r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
            if r is not None and r.status_code == 200 and r.text:
                psoup = BeautifulSoup(r.text, "lxml")
                text += " " + psoup.get_text(" ", strip=True)
                # contact/about pages often hold the personal emails -> add new ones (cap 3)
                phrefs = [a.get("href", "") for a in psoup.find_all("a", href=True)]
                for e in _emails_from(r.text, phrefs):
                    if e not in data["emails"] and len(data["emails"]) < 3:
                        data["emails"].append(e)
        except Exception:
            pass
    if not text:
        return

    # refresh validation + per-email persons now that we may have found more emails
    data["emails_status"] = [list(validate_email(e)) for e in data["emails"]]
    data["emails_persons"] = _persons_from_emails(
        data["emails"], data.get("contact_name"), data.get("contact_title"))

    m = _EMP_RE.search(text)
    if m:
        try:
            data["employees"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    low = text.lower()
    for person in data.get("emails_persons", []):
        nm = person.get("full_name")
        if not nm:
            continue
        idx = low.find(nm.lower())
        if idx >= 0:
            window = text[max(0, idx - 60): idx + len(nm) + 80]
            if not person.get("title"):
                tm = _TITLE_RE.search(window)
                if tm:
                    person["title"] = tm.group(1).title()
            if not person.get("phone"):
                pm = PHONE_RE.search(window)
                if pm and 7 <= len(re.sub(r"\D", "", pm.group(0))) <= 15:
                    person["phone"] = pm.group(0).strip()


def _meta(soup, name=None, prop=None):
    el = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
    c = el.get("content") if el else None
    return c.strip() if c else None


def _extract(html: str, base_url: str = "") -> dict:
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
    out["emails"] = _emails_from(html, hrefs)[:3]
    out["emails_status"] = [list(validate_email(e)) for e in out["emails"]]  # domain-level (MX)

    # extra phone numbers: tel: links first, then plausible numbers in the text.
    # Deduped by DIGITS so "(212)-475-9540" and "12124759540" aren't kept as two entries.
    phones: list[str] = []
    seen_digits: set[str] = set()

    def _add_phone(raw: str):
        d = re.sub(r"\D", "", raw or "")
        if 7 <= len(d) <= 15 and d not in seen_digits:
            seen_digits.add(d)
            phones.append(raw.strip())

    for h in hrefs:
        if h.lower().startswith("tel:"):
            _add_phone(re.sub(r"[^\d+]", "", h[4:]))
    for m in PHONE_RE.findall(soup.get_text(" ", strip=True)):
        _add_phone(m)
    out["phones_extra"] = phones[:3]

    # best-effort contact person from the homepage (schema.org Person / author meta)
    person = soup.select_one("[itemtype$='Person'] [itemprop='name'], [itemprop='name'][itemscope]")
    out["contact_name"] = (person.get_text(" ", strip=True) if person else _meta(soup, name="author")) or None
    title_el = soup.select_one("[itemprop='jobTitle']")
    out["contact_title"] = title_el.get_text(" ", strip=True) if title_el else None

    # one person per email (name derived from the email's local-part)
    out["emails_persons"] = _persons_from_emails(out["emails"], out["contact_name"], out["contact_title"])

    # deep pass: about/team/contact pages -> employee count + per-person title/phone
    if settings.ENRICH_TEAM and base_url:
        try:
            _deep_team(soup, base_url, out)
        except Exception:
            pass
    return out


def _fetch_sync(url: str) -> dict:
    if not url or not url.startswith("http"):
        return empty()
    try:
        r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
        if r is not None and r.status_code == 200 and r.text:
            return _extract(r.text, str(getattr(r, "url", "") or url))
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


def _clean_phones(raws: list, region: str | None) -> list:
    """Validate website-scraped numbers with libphonenumber: drop junk (e.g. '50.000 100'),
    normalize, and dedupe by E.164 (so +1-prefixed and bare forms collapse). Returns up to 3
    nicely-formatted national numbers."""
    out, seen = [], set()
    cc = _REGION_CC.get(region or "")
    for raw in raws:
        try:
            p = phonenumbers.parse(raw, cc)
            if not phonenumbers.is_valid_number(p):
                continue
            key = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL))
    return out[:3]


def _type_phones(card: dict, region: str | None):
    """Clean/validate the extra phones, then set line-type for the main + each extra phone."""
    card["phones_extra"] = _clean_phones(card.get("phones_extra") or [], region)
    card["phone_type"] = _phone_type(card.get("phone") or "", region)
    card["phones_extra_types"] = [_phone_type(p, region) for p in card["phones_extra"]]


async def _phone_owner(card: dict):
    """Fill reverse-phone owner name/address (free, thatsthem) for the main phone, and — if
    PHONE_OWNER_ALL — each extra phone. Best-effort; only US-style numbers resolve."""
    if not settings.ENRICH_PHONE_OWNER:
        return
    if card.get("phone"):
        wp = await whitepages.lookup(card["phone"])
        card["wp_name"], card["wp_address"] = wp.get("name"), wp.get("address")
    if settings.PHONE_OWNER_ALL and card.get("phones_extra"):
        card["phones_extra_wp"] = [await whitepages.lookup(p) for p in card["phones_extra"]]


async def enrich_cards(cards: list[dict], region: str | None = None) -> list[dict]:
    """Merge website-enrichment fields into each card in place (concurrently, bounded).
    Cards with no website still get the (empty) keys so every record has the same columns."""
    if not cards:
        return cards
    await asyncio.to_thread(_load_public_titles)  # warm the SEC list once (cached process-wide)
    if not settings.ENRICH:
        async def bare(card):
            card.update(empty())
            _type_phones(card, region)
            card["is_public"] = is_public_company(card.get("name"))
            await _phone_owner(card)
        await asyncio.gather(*[bare(c) for c in cards])
        return cards

    sem = asyncio.Semaphore(settings.ENRICH_CONCURRENCY)

    async def one(card: dict):
        async with sem:
            data = await asyncio.to_thread(_fetch_sync, card.get("website") or "")
            card.update(data)
            _type_phones(card, region)
            card["is_public"] = is_public_company(card.get("name"))
            await _phone_owner(card)

    await asyncio.gather(*[one(c) for c in cards])
    return cards
