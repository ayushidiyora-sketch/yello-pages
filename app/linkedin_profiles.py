"""LinkedIn Profiles Scraper — public-profile data for a list of profile URLs / ids.

Same design as the other live scrapers: every request goes through a proxy IP (a paid PROXY_URL if
set, otherwise the rotating free US pool — NEVER the real IP). LinkedIn aggressively auth-walls
unauthenticated traffic, so on the free pool it usually returns the "Join LinkedIn" login wall and
yields 0 until a paid residential PROXY_URL is set (same reality as the G2 / BBB scrapers).

Data comes from the public profile page's embedded `application/ld+json` `Person` object (the same
structured data Google reads) plus `og:` meta tags as a fallback — not the logged-in DOM. A query
line may be a full linkedin.com/in/<slug> URL or a bare profile slug/id.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

LIP_COLUMNS = ["profile_url", "name", "headline", "location", "current_company", "current_title",
               "about", "education", "followers", "image"]

_LIP_TIMEOUT = 20
# markers of LinkedIn's login/sign-up wall (i.e. NOT real profile content)
_AUTHWALL = ("authwall", "/uas/login", "join linkedin to", "sign in to see",
             "please sign in", "to view this profile")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _profile_url(s: str) -> str:
    """A query may be a full LinkedIn profile URL or a bare slug/id -> canonical /in/<slug> URL."""
    s = (s or "").strip()
    if s.lower().startswith("http"):
        u = urlparse(s)
        return f"https://www.linkedin.com{u.path.rstrip('/')}"
    slug = s.strip("/").split("/")[-1]
    return f"https://www.linkedin.com/in/{slug}"


def _is_real_profile(html: str | None) -> bool:
    """True if the HTML is a real public profile (not the auth/login wall)."""
    if not html:
        return False
    low = html.lower()
    if any(s in low for s in _AUTHWALL):
        # the wall sometimes still embeds a Person ld+json; only treat as blocked if no Person data
        if '"@type":"person"' not in low and 'og:title' not in low:
            return False
    return '"@type":"person"' in low or 'og:title' in low or "/in/" in low


# ---------------- proxy fetch (never the real IP) ----------------

def _get_sync(url: str) -> str | None:
    """Fetch one profile through a proxy with curl_cffi. Paid PROXY_URL if set, else rotate the free
    pool. Returns HTML on a real page, None if blocked/failed. NEVER the real IP."""
    proxy = settings.PROXY_URL.strip()
    if proxy:
        try:
            r = cffi.get(url, impersonate="chrome", headers=_HEADERS,
                         proxies={"http": proxy, "https": proxy},
                         timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
            return r.text if (r is not None and r.status_code == 200) else None
        except Exception:
            return None
    # free pool: rotate a handful of warm proxies until one returns a real profile page
    try:
        yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 8)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
    except Exception:
        warm = []
    for px in warm[:8]:
        try:
            r = cffi.get(url, impersonate="chrome", headers=_HEADERS,
                         proxies={"http": px, "https": px}, timeout=_LIP_TIMEOUT,
                         verify=False, allow_redirects=True)
        except Exception:
            continue
        if r is not None and r.status_code == 200 and _is_real_profile(r.text):
            return r.text
    return None


def _headless_get_sync(url: str, proxy: str | None) -> str | None:
    """Render the profile in headless Chrome (better odds past LinkedIn's JS/login wall), ALWAYS
    routed through `proxy`. PROXY-ONLY: if no proxy is given, return None — the real IP is never used."""
    if not proxy:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    launch_proxy = {"server": proxy if proxy.startswith("http") else "http://" + proxy}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy=launch_proxy,
                                        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(locale="en-US", user_agent=_UA)
            page = ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
            return html
    except Exception:
        return None


async def _fetch_html(url: str) -> str | None:
    """Fetch a profile as HTML — PROXY-ONLY, the real IP is NEVER used. Paid proxy: curl then a
    headless render through that proxy. Free: curl rotation, then a headless render through a couple
    of warm pool proxies. If no proxy passes LinkedIn's wall, return None (needs a residential
    PROXY_URL) — there is no direct/real-IP fallback."""
    paid = settings.PROXY_URL.strip()
    html = await asyncio.to_thread(_get_sync, url)
    if _is_real_profile(html):
        return html
    if paid:
        html = await asyncio.to_thread(_headless_get_sync, url, paid)
        return html if _is_real_profile(html) else None
    # free fallback: headless ONLY through warm pool proxies (never the real IP)
    try:
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
    except Exception:
        warm = []
    for px in warm[:3]:
        html = await asyncio.to_thread(_headless_get_sync, url, px)
        if _is_real_profile(html):
            return html
    return None


# ---------------- parsing ----------------

def _person_jsonld(html: str) -> dict | None:
    """Return the Person object from the page's ld+json (LinkedIn nests it under @graph)."""
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        graph = data.get("@graph") if isinstance(data, dict) else None
        objs = graph if isinstance(graph, list) else (data if isinstance(data, list) else [data])
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Person" in types:
                return obj
    return None


def _clean(s: str) -> str:
    """Fix the common mojibake from a mis-decoded UTF-8 page (e.g. 'Â·' -> '·')."""
    if not s:
        return ""
    if "Â" in s or "â\x80" in s:
        try:
            s = s.encode("latin-1", "ignore").decode("utf-8", "ignore")
        except Exception:
            pass
    return s.strip()


def _name_of(v) -> str:
    if isinstance(v, dict):
        return v.get("name") or ""
    if isinstance(v, list) and v:
        return _name_of(v[0])
    return v or "" if isinstance(v, str) else ""


def _parse(html: str, url: str) -> dict | None:
    row = {c: "" for c in LIP_COLUMNS}
    row["profile_url"] = url
    p = _person_jsonld(html)
    if p:
        row["name"] = p.get("name") or ""
        jt = p.get("jobTitle")
        row["current_title"] = (jt[0] if isinstance(jt, list) and jt else jt) or "" if jt else ""
        row["current_company"] = _name_of(p.get("worksFor"))
        addr = p.get("address")
        if isinstance(addr, dict):
            row["location"] = ", ".join(x for x in [addr.get("addressLocality"),
                                                    addr.get("addressRegion"),
                                                    addr.get("addressCountry")] if x)
        img = p.get("image")
        row["image"] = img.get("contentUrl", "") if isinstance(img, dict) else (img or "")
        row["about"] = p.get("description") or ""
        al = p.get("alumniOf")
        if isinstance(al, list):
            row["education"] = "; ".join(a.get("name", "") for a in al if isinstance(a, dict))
        else:
            row["education"] = _name_of(al)
        ic = p.get("interactionStatistic")
        for s in (ic if isinstance(ic, list) else [ic]):
            if isinstance(s, dict) and "follow" in str(s.get("interactionType", "")).lower():
                row["followers"] = str(s.get("userInteractionCount") or "")
    # og: fallbacks
    soup = BeautifulSoup(html, "lxml")
    if not row["name"]:
        og = soup.select_one('meta[property="og:title"]')
        if og:
            row["name"] = _clean(re.split(r"\s[|\-–]\s", og.get("content") or "")[0])
    if not row["headline"]:
        desc = soup.select_one('meta[property="og:description"]')
        if desc:
            # LinkedIn packs "headline · about · Experience · Education · Location · N connections"
            # into og:description — the real headline is just the first " · "-separated segment.
            cleaned = _clean(desc.get("content") or "")
            row["headline"] = re.split(r"\s+[·•]\s+", cleaned)[0].strip()[:300]
    if not row["headline"]:
        row["headline"] = (row["current_title"] +
                           (f" at {row['current_company']}" if row["current_company"] else "")).strip()
    if not row["image"]:
        ogi = soup.select_one('meta[property="og:image"]')
        if ogi:
            row["image"] = ogi.get("content") or ""
    for k in ("name", "headline", "about", "current_company", "current_title", "education", "location"):
        row[k] = _clean(row[k])
    return row if (row["name"] or row["headline"]) else None


# ---------------- scrape + run loop ----------------

async def scrape(query: str) -> dict | None:
    url = _profile_url(query)
    html = await _fetch_html(url)
    if not html:
        return None
    return _parse(html, url)


async def run_job(job_id: str, queries: list[str]) -> None:
    """Background task: fetch each profile through a proxy IP and store one row per profile."""
    from .db import jobs, linkedin_profiles
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            row = await scrape(q)
            if row:
                row["job_id"] = job_id
                await linkedin_profiles.insert_one(row)
                total += 1
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        done = {"status": "stopped" if stopped else "done", "total_scraped": total,
                "finished_at": datetime.utcnow()}
        if not total and not stopped:
            done["note"] = ("LinkedIn returned 0 profiles — its login/auth wall blocked the free "
                            "proxies. Set a paid residential PROXY_URL in .env for reliable results "
                            "(only public profile fields are available; the real IP is never used).")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
