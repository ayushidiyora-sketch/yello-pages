"""Google Maps Photos Scraper — all photos from a Google Maps place (free, no API).

Renders the place in a headless browser, opens the Photos gallery, scrolls it, and collects the
googleusercontent.com image URLs from the DOM — then normalizes each to a high-resolution version.
Same free, no-API approach as the Reviews scraper; reuses its Playwright worker + proxy + helpers.

PROXY-ONLY, paid/residential REQUIRED (Google blocks free/datacenter IPs): set PROXY_URL in .env.
The gallery open/scroll selectors are obfuscated/locale-dependent and may need one live-tuning pass;
the googleusercontent URL collection itself is robust.
"""
import asyncio
import re
from datetime import datetime

from .config import settings
from .gmaps_reviews import (_run, _proxy_opts, _UA, _accept_consent, _looks_blocked,
                            _place_url, _place_id, categories)  # noqa: F401 (categories re-exported)

GMP_COLUMNS = ["query", "place_name", "place_id", "photo_url"]

# turn a thumbnail URL (…=w203-h152-k-no / …=s120) into a high-res one
_SIZE_RE = re.compile(r"=(?:w\d+-h\d+|s\d+)(?:-[a-z\-]+)?$", re.I)
# skip avatar/profile/icon thumbs (small square crops)
_AVATAR_RE = re.compile(r"=s(?:16|24|32|36|40|48|64|72|96)(?:-c)?\b", re.I)


def _hires(url: str) -> str:
    return _SIZE_RE.sub("=s1600", url) if _SIZE_RE.search(url) else url


# ---------- internal photo-API interception (Option B) ----------
# When the gallery opens/scrolls, Google fetches photos from its own RPC; the photo URLs are plain
# strings inside that RPC JSON. We capture those response bodies and pull the URLs straight out —
# i.e. the internal API's data, not the rendered HTML. (DOM stays as a fallback.)
_PHOTO_RPC = ("/maps/preview", "/photometa", "listentit", "/maps/rpc", "/_/", "/maps/photo")
_PHOTO_URL_RE = re.compile(r"https?://(?:lh\d+\.googleusercontent\.com|[a-z0-9]+\.ggpht\.com)/[^\"\\\s]+")


def _attach_photo_capture(pg):
    """Collect the raw bodies of Google Maps' internal photo-RPC responses for this page."""
    bodies: list[str] = []

    def _on_response(resp):
        try:
            u = (resp.url or "").lower()
            if "google.com/maps" in u and any(k in u for k in _PHOTO_RPC):
                bodies.append(resp.text())
        except Exception:
            pass

    pg.on("response", _on_response)
    return bodies


def _photo_urls_from_bodies(bodies: list[str], limit: int | None) -> list[str]:
    """Regex googleusercontent/ggpht photo URLs out of the captured internal-API JSON."""
    out, seen = [], set()
    for b in bodies or []:
        for m in _PHOTO_URL_RE.findall(b or ""):
            url = m.rstrip("\\")
            if _AVATAR_RE.search(url):
                continue
            hu = _hires(url)
            if hu in seen:
                continue
            seen.add(hu)
            out.append(hu)
            if limit and len(out) >= limit:
                return out
    return out


# JS run in the page: gather every photo URL (img src + CSS background) from googleusercontent/ggpht.
_COLLECT_JS = r"""
() => {
  const urls = new Set();
  document.querySelectorAll('img').forEach(im => {
    const s = im.src || im.getAttribute('data-src') || '';
    if (s.includes('googleusercontent.com') || s.includes('ggpht.com')) urls.add(s);
  });
  document.querySelectorAll('[style*="googleusercontent"], [style*="ggpht"]').forEach(el => {
    const m = (el.getAttribute('style') || '').match(/url\((['"]?)(https?:[^'")]+)\1\)/);
    if (m) urls.add(m[2]);
  });
  return Array.from(urls);
}
"""


def _open_photos(pg) -> None:
    # a text-search landed on a results list -> open the first place
    try:
        if "/maps/search/" in (pg.url or ""):
            pg.wait_for_selector("a.hfpxzc", timeout=9000)
            el = pg.query_selector("a.hfpxzc")
            if el:
                el.click()
                pg.wait_for_timeout(2800)
    except Exception:
        pass
    # open the Photos gallery (hero image / "Photos" button)
    for sel in ['button[aria-label*="Photo of" i]', 'button[aria-label*="All photos" i]',
                'button[jsaction*="hero"]', 'button.aoRNLd', '.aoRNLd',
                'button[aria-label*="Photos" i]', 'button:has-text("Photos")']:
        try:
            el = pg.query_selector(sel)
            if el:
                el.click()
                pg.wait_for_timeout(2500)
                break
        except Exception:
            pass


def _scroll_photos(pg, limit: int | None) -> None:
    cap = 80 if not limit else max(10, (limit // 10) + 10)
    last, stale = 0, 0
    for _ in range(cap):
        try:
            pg.evaluate("""() => {
              const imgs = document.querySelectorAll('img[src*="googleusercontent"], img[src*="ggpht"]');
              const c = imgs[imgs.length - 1];
              let el = c && c.parentElement;
              while (el) {
                const o = getComputedStyle(el).overflowY;
                if ((o==='auto'||o==='scroll') && el.scrollHeight > el.clientHeight + 40) {
                  el.scrollTop = el.scrollHeight; return;
                }
                el = el.parentElement;
              }
              window.scrollTo(0, document.body.scrollHeight);
            }""")
        except Exception:
            pass
        pg.wait_for_timeout(1100)
        n = len(pg.query_selector_all('img[src*="googleusercontent"], img[src*="ggpht"]'))
        if limit and n >= limit + 6:           # a few extra (avatars get filtered out)
            break
        stale = stale + 1 if n <= last else 0
        last = n
        if stale >= 3:
            break


def _scrape_photos(browser, query: str, limit: int | None, language: str, proxy: str) -> list[dict]:
    url = _place_url(query, language)
    kw = {"locale": "en-US", "user_agent": _UA, "viewport": {"width": 1366, "height": 900},
          "proxy": _proxy_opts(proxy)}
    ctx = browser.new_context(**kw)
    try:
        ctx.add_cookies([{"name": "CONSENT", "value": "YES+", "domain": ".google.com", "path": "/"}])
        ctx.set_extra_http_headers({"Accept-Language": (language or "en") + ",en;q=0.9"})
        pg = ctx.new_page()
        photo_bodies = _attach_photo_capture(pg)   # Option B: capture the internal photo API
        pg.goto(url, timeout=45000, wait_until="domcontentloaded")
        pg.wait_for_timeout(3000)
        _accept_consent(pg)
        blocked = _looks_blocked(pg)
        if blocked:
            raise RuntimeError(f"Google blocked this request — {blocked}. Use a cleaner residential "
                               "proxy (datacenter/free IPs are blocked).")
        place_name = ""
        try:
            h = pg.query_selector("h1.DUwDvf, h1.fontHeadlineLarge")
            place_name = (h.text_content() or "").strip() if h else ""
        except Exception:
            pass
        _open_photos(pg)
        _scroll_photos(pg, limit)
        place_id = _place_id(query) or ""
        # PRIMARY: photo URLs straight from the captured internal API (JSON), not the HTML DOM
        urls = _photo_urls_from_bodies(photo_bodies, limit)
        if not urls:
            # FALLBACK: collect from the rendered DOM (img src + CSS backgrounds)
            raw = pg.evaluate(_COLLECT_JS) or []
            seen = set()
            for u in raw:
                if not u or _AVATAR_RE.search(u):
                    continue
                hu = _hires(u)
                if hu not in seen:
                    seen.add(hu)
                    urls.append(hu)
        rows = [{"query": query, "place_name": place_name, "place_id": place_id, "photo_url": u}
                for u in urls]
        return rows[:limit] if limit else rows
    finally:
        ctx.close()


async def search(query: str, limit: int | None, language: str) -> list[dict]:
    proxy = settings.PROXY_URL.strip()
    if not proxy:
        raise RuntimeError("Google Maps photos scraping needs a paid residential PROXY_URL in .env "
                           "— Google blocks free/datacenter IPs (no real IP is used).")
    return await asyncio.to_thread(_run, lambda b: _scrape_photos(b, query, limit, language, proxy))


async def run_job(job_id: str, queries: list[str], limit: int | None, language: str) -> None:
    from .db import jobs, gmaps_photos
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gmaps_photos.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
