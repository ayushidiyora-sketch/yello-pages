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

# the keys this module contributes to each business record (always present, even if empty)
ENRICH_KEYS = list(SOCIAL_DOMAINS) + [
    "emails", "website_title", "website_description", "website_keywords",
    "website_generator", "website_has_fb_pixel", "website_has_google_tag",
]


def empty() -> dict:
    d = {k: None for k in SOCIAL_DOMAINS}
    d.update(emails=[], website_title=None, website_description=None, website_keywords=None,
             website_generator=None, website_has_fb_pixel=False, website_has_google_tag=False)
    return d


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


async def enrich_cards(cards: list[dict]) -> list[dict]:
    """Merge website-enrichment fields into each card in place (concurrently, bounded).
    Cards with no website still get the (empty) keys so every record has the same columns."""
    if not cards:
        return cards
    if not settings.ENRICH:
        for c in cards:
            c.update(empty())
        return cards

    sem = asyncio.Semaphore(settings.ENRICH_CONCURRENCY)

    async def one(card: dict):
        async with sem:
            data = await asyncio.to_thread(_fetch_sync, card.get("website") or "")
        card.update(data)

    await asyncio.gather(*[one(c) for c in cards])
    return cards
