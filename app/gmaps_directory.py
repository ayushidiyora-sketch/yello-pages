"""Google Maps Directory Places — business listings from a Google Maps search or place (free, no API).

Renders the Google Maps URL (a "category, city" search, a search URL, or a place_id / place URL) in a
headless browser through the proxy, scrolls the results feed, and reads the place cards. It also
captures the internal search RPC (internal-API); the DOM is the reliable extractor for the multi-field
place rows. Same free, no-API, Playwright+proxy approach as the other Google Maps scrapers.

PROXY-ONLY, paid/residential REQUIRED (Google blocks free/datacenter IPs): set PROXY_URL in .env.
The results-feed selectors are obfuscated/locale-dependent and may need one live-tuning pass.
"""
import asyncio
import re
from datetime import datetime

from .config import settings
from .gmaps_reviews import (_run, _proxy_opts, _UA, _accept_consent, _looks_blocked,
                            _place_url, _place_id, _attach_rpc_capture)  # noqa: F401

GMD_COLUMNS = ["query", "name", "category", "address", "rating", "reviews", "place_id", "maps_url"]

_FID_RE = re.compile(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")


# JS run in the page: read every place card in the results feed (or the single place panel).
_EXTRACT_JS = r"""
() => {
  const txt = (el) => el ? (el.textContent || '').trim() : '';
  const rows = [];
  const seen = new Set();
  const links = document.querySelectorAll('a.hfpxzc');
  if (links.length) {
    links.forEach(a => {
      const name = a.getAttribute('aria-label') || '';
      if (!name || seen.has(name)) return;
      seen.add(name);
      const card = a.parentElement || a;
      const rating = txt(card.querySelector('.MW4etd'));
      const reviews = (txt(card.querySelector('.UY7F9')) || '').replace(/[() ]/g, '');
      const w4 = Array.from(card.querySelectorAll('.W4Efsd')).map(e => (e.textContent || '').trim()).filter(Boolean);
      let category = '', address = '';
      if (w4.length) {
        const parts = w4.join(' · ').split('·').map(s => s.trim()).filter(Boolean);
        category = parts[0] || '';
        address = parts.slice(1).join(', ');
      }
      rows.push({name, category, address, rating, reviews, maps_url: a.href || ''});
    });
  } else {
    // single place panel
    const h = document.querySelector('h1.DUwDvf, h1.fontHeadlineLarge');
    if (h) rows.push({
      name: txt(h),
      category: txt(document.querySelector('button[jsaction*="category"]')),
      address: txt(document.querySelector('[data-item-id="address"]')),
      rating: txt(document.querySelector('.MW4etd, .fontDisplayLarge')),
      reviews: (txt(document.querySelector('.UY7F9, [aria-label*="reviews" i]')) || '').replace(/[() ]/g, ''),
      maps_url: location.href,
    });
  }
  return rows;
}
"""


def _scroll_feed(pg, limit):
    cap = 60 if not limit else max(8, (limit // 7) + 8)
    last, stale = 0, 0
    for _ in range(cap):
        try:
            pg.evaluate("""() => {
              const f = document.querySelector('div[role="feed"]') ||
                        (document.querySelector('a.hfpxzc') || {}).closest?.('div[tabindex]');
              if (f) { f.scrollTop = f.scrollHeight; return; }
              window.scrollTo(0, document.body.scrollHeight);
            }""")
        except Exception:
            pass
        pg.wait_for_timeout(1100)
        n = len(pg.query_selector_all('a.hfpxzc'))
        if limit and n >= limit:
            break
        stale = stale + 1 if n <= last else 0
        last = n
        if stale >= 3:
            break


def _scrape_directory(browser, query, limit, language, proxy):
    url = _place_url(query, language)
    kw = {"locale": "en-US", "user_agent": _UA, "viewport": {"width": 1366, "height": 900},
          "proxy": _proxy_opts(proxy)}
    ctx = browser.new_context(**kw)
    try:
        ctx.add_cookies([{"name": "CONSENT", "value": "YES+", "domain": ".google.com", "path": "/"}])
        ctx.set_extra_http_headers({"Accept-Language": (language or "en") + ",en;q=0.9"})
        pg = ctx.new_page()
        _attach_rpc_capture(pg)             # capture the internal search RPC (internal-API)
        pg.goto(url, timeout=45000, wait_until="domcontentloaded")
        pg.wait_for_timeout(3500)
        _accept_consent(pg)
        blocked = _looks_blocked(pg)
        if blocked:
            raise RuntimeError(f"Google blocked this request — {blocked}. Use a cleaner residential "
                               "proxy (datacenter/free IPs are blocked).")
        _scroll_feed(pg, limit)
        raw = pg.evaluate(_EXTRACT_JS) or []
        rows = []
        for it in raw:
            if not it.get("name"):
                continue
            murl = it.get("maps_url") or ""
            fid = _FID_RE.search(murl)
            rows.append({
                "query": query,
                "name": it.get("name") or "",
                "category": it.get("category") or "",
                "address": it.get("address") or "",
                "rating": it.get("rating") or "",
                "reviews": it.get("reviews") or "",
                "place_id": _place_id(query) or (fid.group(1) if fid else ""),
                "maps_url": murl,
            })
        return rows[:limit] if limit else rows
    finally:
        ctx.close()


async def search(query: str, limit: int | None, language: str) -> list[dict]:
    proxy = settings.PROXY_URL.strip()
    if not proxy:
        raise RuntimeError("Google Maps directory places need a paid residential PROXY_URL in .env "
                           "— Google blocks free/datacenter IPs (no real IP is used).")
    return await asyncio.to_thread(_run, lambda b: _scrape_directory(b, query, limit, language, proxy))


async def run_job(job_id: str, queries: list[str], limit: int | None, language: str) -> None:
    from .db import jobs, gmaps_directory
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gmaps_directory.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
