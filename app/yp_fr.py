"""pagesjaunes.fr (FR) scraper — browser-based.

The French Yellow Pages is **pagesjaunes.fr** (not yellowpages.fr). Unlike the US/CA/AU
sites, it sits behind **Cloudflare**: plain curl_cffi requests (and the free proxy pool)
get a 403 challenge page. A real headless browser (Playwright/Chromium) passes the
challenge, so FR is scraped by rendering the page, clicking each "Afficher le N°" button
to reveal the phone, then parsing the resulting HTML.

Because Playwright's sync API is thread-affine, ALL browser work runs on one dedicated
worker thread (a tiny queue); the browser is launched once and reused. This makes FR slow
(a real render per page, ~10-15s) and serial — acceptable for a Cloudflare-walled site.

Search URL: `/annuaire/{location-slug}/{search-slug}` (e.g. `/annuaire/besancon/restaurants`),
`?page=N` for pagination. ~20 listings per page.
"""
import asyncio
import queue
import re
import threading
import unicodedata
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import settings

BASE_FR = "https://www.pagesjaunes.fr"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---- single Playwright worker thread (sync API is bound to its creating thread) ----
_jobs: "queue.Queue" = queue.Queue()
_started = False
_start_lock = threading.Lock()


def _worker():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    # launched WITHOUT a proxy at the browser level; each render builds its own context — with a
    # paid PROXY_URL if set (no real IP), else direct (real IP, the only way past Cloudflare free).
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    while True:
        fn, holder, done = _jobs.get()
        try:
            holder.append(fn(browser))
        except Exception as e:  # hand the exception back to the caller
            holder.append(e)
        finally:
            done.set()


def _run(fn):
    """Run fn(browser) on the dedicated browser thread and return its result (or raise)."""
    global _started
    with _start_lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
    holder, done = [], threading.Event()
    _jobs.put((fn, holder, done))
    done.wait()
    res = holder[0]
    if isinstance(res, Exception):
        raise res
    return res


def _slug(s: str) -> str:
    """'Besançon' -> 'besancon', 'Médical Clinics' -> 'medical-clinics'."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")


def _url(search: str, location: str, page: int) -> str:
    u = f"{BASE_FR}/annuaire/{_slug(location)}/{_slug(search)}"
    return u + (f"?page={page}" if page and page > 1 else "")


def _proxy_opts(px: str) -> dict:
    """'http://user:pass@host:port' or 'http://host:port' -> Playwright proxy dict."""
    u = urlparse(px if "://" in px else "http://" + px)
    opt = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        opt["username"] = u.username
    if u.password:
        opt["password"] = u.password
    return opt


def _proxies() -> list[str]:
    """Proxy to route the browser through. A paid PROXY_URL (no real IP, passes Cloudflare) if
    set; otherwise EMPTY -> connect directly on the real IP, since the free US pool can't pass
    pagesjaunes' Cloudflare and a direct browser can. (User chose: FR works free via real IP.)"""
    paid = settings.PROXY_URL.strip()
    return [paid] if paid else []


def _looks_loaded(html: str) -> bool:
    low = (html or "").lower()
    return ("bi-denomination" in low or 'li class="bi' in low
            or "résultats" in low or "resultats" in low)


def _render_proxied(browser, url: str, reveal: bool) -> str:
    """Render `url`, routing through a paid PROXY_URL if set (no real IP), else directly on the
    real IP (the free pool can't pass pagesjaunes' Cloudflare). Returns the loaded HTML, or the
    last attempt's HTML if blocked."""
    attempts = _proxies()[:6] or [None]   # None -> direct (real IP) when no paid proxy
    last = ""
    for px in attempts:
        kw = {"locale": "fr-FR", "user_agent": _UA, "viewport": {"width": 1366, "height": 900}}
        if px:
            kw["proxy"] = _proxy_opts(px)
        ctx = browser.new_context(**kw)
        try:
            pg = ctx.new_page()
            pg.goto(url, timeout=45000, wait_until="domcontentloaded")
            pg.wait_for_timeout(4500 if reveal else 2500)
            if reveal:
                # reveal phone numbers (each card has an "Afficher le N°" button)
                for btn in pg.query_selector_all("li.bi button.btn_tel, li.bi [class*=btn_tel]"):
                    try:
                        btn.click(timeout=1200)
                    except Exception:
                        pass
                pg.wait_for_timeout(1200)
            html = pg.content()
            if _looks_loaded(html) or (not reveal and len(html) > 8000 and "__cf_chl" not in html.lower()):
                return html
            last = html
        except Exception:
            pass
        finally:
            ctx.close()
    return last  # every proxy was blocked


def _fetch_sync(search: str, location: str, page: int) -> str:
    html = _run(lambda b: _render_proxied(b, _url(search, location, page), reveal=True))
    if _looks_loaded(html):
        return html
    low = (html or "").lower()
    if not html or "__cf_chl" in low or "cloudflare" in low or "request unsuccessful" in low:
        raise RuntimeError(
            "pagesjaunes.fr did not load (Cloudflare challenge / network) — please retry.")
    return ""  # valid page, genuinely no results -> caller finishes done(0)


async def fetch_fr_page(search: str, location: str, page: int) -> str:
    return await asyncio.to_thread(_fetch_sync, search, location, page)


def fetch_detail_sync(url: str) -> str | None:
    """Render a pagesjaunes detail page (through a proxy, never the real IP) to pull the website
    + amenities the SERP hides. Best-effort: a failure or a block just yields None."""
    if not url:
        return None
    try:
        html = _run(lambda b: _render_proxied(b, url, reveal=False))
        low = (html or "").lower()
        if html and len(html) > 8000 and "__cf_chl" not in low and "request unsuccessful" not in low:
            return html
    except Exception:
        pass
    return None


# external links on a detail page that are NOT the business's own site
_NOT_SITE = ("pagesjaunes", "google.", "solocal", "/pros/", "facebook.com/pagesjaunes",
             "apple.com", "doctolib", "/static")


def parse_detail(html: str | None):
    """From a pagesjaunes detail page return (website, amenities). website = the business's
    own external link if present; amenities = the 'Services et prestations' labels joined."""
    if not html:
        return None, None
    soup = BeautifulSoup(html, "lxml")
    website = None
    for a in soup.select("a[href^='http']"):
        h = a.get("href") or ""
        if all(x not in h.lower() for x in _NOT_SITE):
            website = h
            break
    amen = []
    sec = soup.select_one("[class*=prestation]")
    if sec:
        for li in sec.select("li"):
            t = li.get_text(" ", strip=True)
            if t and len(t) < 50 and t not in amen:
                amen.append(t)
    return website, (", ".join(amen[:25]) or None)


def parse_fr_total(html: str) -> int | None:
    """'315 résultats' -> 315 (count and word can sit in separate tags)."""
    html = BeautifulSoup(html or "", "lxml").get_text(" ")
    m = re.search(r"([\d\s  ]+)\s*r.sultats", html or "", re.I)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        return int(digits) if digits else None
    return None


def _txt(node):
    return node.get_text(" ", strip=True) if node else None


_PHONE = re.compile(r"0\d(?:[ .]?\d\d){4}")


def parse_fr_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for c in soup.select("li.bi"):
        name = _txt(c.select_one(".bi-denomination"))
        if not name:
            continue

        # phone revealed into the card text after the click in _render
        pm = _PHONE.search(c.get_text(" "))
        phone = pm.group(0).strip() if pm else None

        # address: "12 faubourg Rivotte 25000 Besançon Voir le plan"
        addr = _txt(c.select_one(".bi-address")) or ""
        addr = re.sub(r"\s*Voir le plan\s*$", "", addr).strip()
        street = city = pincode = None
        zm = re.search(r"\b(\d{5})\b", addr)
        if zm:
            pincode = zm.group(1)
            street = addr[:zm.start()].strip().rstrip(",").strip() or None
            city = addr[zm.end():].strip()
            # cut action-button text the card mixes into the address line
            city = re.split(r"\s*(?:Voir le plan|Site web|Site internet|Itin)", city)[0].strip() or None
        else:
            street = re.split(r"\s*(?:Voir le plan|Site web|Site internet|Itin)", addr)[0].strip() or None

        snippet = _txt(c.select_one(".bi-description, .bi-baseline"))

        cats = [a.get_text(" ", strip=True)
                for a in c.select(".bi-activite a, .activite a, .bi-secteur-activite a, .bi-tags a")]
        cat_list = list(dict.fromkeys([x for x in cats if x]))
        category = ", ".join(cat_list) or None

        img = c.select_one("img")
        image = (img.get("src") or img.get("data-src")) if img else None
        if image and image.startswith("/"):
            image = BASE_FR + image

        link = c.select_one("a[href*='/pros/']")
        href = link.get("href") if link else None
        source_url = (BASE_FR + href) if href and href.startswith("/") else href

        # the "Site web" link carries the real external URL directly (not obfuscated)
        website = None
        for a in c.select("a[href^='http']"):
            href = a.get("href") or ""
            if "pagesjaunes.fr" not in href and "/pros/" not in href:
                website = href
                break

        out.append({
            "name": name,
            "phone": phone,
            "category": category,
            "categories": cat_list,
            "area": street,
            "city": city,
            "state": None,            # France has no 2-letter state
            "pincode": pincode,
            "range": None,
            "rating": None,
            "reviews_count": None,
            "open_status": None,
            "email": None,
            "website": website,
            "directions": None,
            "image": image,
            "years_in_business": None,
            "description": snippet,
            "source_url": source_url,
        })
    return out
