"""Google Maps Contributor Reviews Scraper — every review a contributor (Local Guide) has left.

Input is a Google Maps contributor ID (e.g. 116992800507045820329) or a profile URL
(https://www.google.com/maps/contrib/<id>/reviews). We render that contributor's reviews page in a
headless browser, scroll the panel to load all their reviews, and read the cards from the DOM — same
free, no-API approach as the place Reviews scraper. Each row is one review the contributor left for a
place (place name, rating, date, text, owner reply, likes).

PROXY-ONLY, paid/residential REQUIRED (Google blocks free/datacenter IPs): set PROXY_URL in .env.
Reuses the Playwright worker + proxy + scroll helpers from gmaps_reviews. The review markup is
obfuscated/locale-dependent — selectors may need one live-tuning pass against a real proxied page.
"""
import asyncio
import re
from datetime import datetime

from .config import settings
from .gmaps_reviews import (_run, _proxy_opts, _UA, _accept_consent, _looks_blocked,
                            _scroll_reviews, _attach_rpc_capture, _parse_rpc_reviews)

GMCR_COLUMNS = ["contributor", "place_name", "rating", "date", "review",
                "owner_response", "likes", "language"]

_CID = re.compile(r"/contrib/(\d+)")
_NUM = re.compile(r"^\d{10,}$")


def _contrib_id(inp: str) -> str:
    m = _CID.search(inp or "")
    if m:
        return m.group(1)
    s = (inp or "").strip()
    return s if _NUM.match(s) else ""


def _contrib_url(inp: str, language: str) -> str:
    hl = language or "en"
    s = (inp or "").strip()
    if s.lower().startswith("http"):
        url = s.split("?")[0]
        if "/reviews" not in url:
            url = url.rstrip("/") + "/reviews"
        return f"{url}?hl={hl}"
    cid = _contrib_id(s)
    return f"https://www.google.com/maps/contrib/{cid}/reviews?hl={hl}"


# JS run inside the page: read every loaded contributor-review card into plain objects.
_EXTRACT_JS = r"""
() => {
  const pick = (el, sels) => { for (const s of sels) { const e = el.querySelector(s); if (e) return e; } return null; };
  const cards = Array.from(document.querySelectorAll('div.jftiEf, div[data-review-id]'));
  return cards.map(c => {
    const placeEl = pick(c, ['.d4r55', '.WNxzHc', 'a[href*="/maps/place"]', '.WMbnJf']);
    const starEl = pick(c, ['span[role="img"][aria-label*="star" i]', '.kvMYJc[aria-label]', 'span[aria-label*="star" i]']);
    const aria = starEl ? (starEl.getAttribute('aria-label') || '') : '';
    const rm = aria.match(/([0-9]+(?:[.,][0-9])?)/);
    const textEl = pick(c, ['.wiI7pd', '.MyEned']);
    const dateEl = pick(c, ['.rsqaWe', '.xRkPPb']);
    const respWrap = pick(c, ['.CDe7pd']);
    const respEl = respWrap ? (pick(respWrap, ['.wiI7pd']) || respWrap) : null;
    const likeEl = pick(c, ['.pkWtMe', '[aria-label*="helpful" i]']);
    const t = (e) => e ? (e.textContent || '').trim() : '';
    return {
      place_name: t(placeEl),
      rating: rm ? rm[1].replace(',', '.') : '',
      date: t(dateEl),
      review: t(textEl),
      owner_response: respEl ? (respEl.textContent || '').replace(/^Response from the owner/i, '').trim() : '',
      likes: likeEl ? ((likeEl.textContent || '').replace(/\D/g, '')) : '',
    };
  });
}
"""


def _scrape_contrib(browser, inp: str, limit: int | None, language: str, proxy: str) -> list[dict]:
    url = _contrib_url(inp, language)
    contributor = _contrib_id(inp) or inp.strip()
    kw = {"locale": "en-US", "user_agent": _UA, "viewport": {"width": 1366, "height": 900},
          "proxy": _proxy_opts(proxy)}
    ctx = browser.new_context(**kw)
    try:
        ctx.add_cookies([{"name": "CONSENT", "value": "YES+", "domain": ".google.com", "path": "/"}])
        ctx.set_extra_http_headers({"Accept-Language": (language or "en") + ",en;q=0.9"})
        pg = ctx.new_page()
        rpc_bodies = _attach_rpc_capture(pg)     # Option B: capture the internal review API
        pg.goto(url, timeout=45000, wait_until="domcontentloaded")
        pg.wait_for_timeout(3000)
        _accept_consent(pg)
        blocked = _looks_blocked(pg)
        if blocked:
            raise RuntimeError(f"Google blocked this request — {blocked}. Use a cleaner residential "
                               "proxy (datacenter/free IPs are blocked).")
        _scroll_reviews(pg, limit)
        # PRIMARY: reviews straight from the captured internal review API (JSON, not HTML).
        # On a contributor page the RPC's "author" string is the reviewed PLACE name.
        api = _parse_rpc_reviews(rpc_bodies)
        if api:
            rows = [{
                "contributor": contributor,
                "place_name": a.get("author") or "",
                "rating": str(a.get("rating") or ""),
                "date": a.get("date") or "",
                "review": a.get("text") or "",
                "owner_response": "", "likes": "", "language": language or "",
            } for a in api]
            return rows[:limit] if limit else rows
        # FALLBACK: read the rendered review cards from the DOM
        try:
            for b in pg.query_selector_all('button[aria-label="See more" i], button.w8nwRe'):
                try:
                    b.click()
                except Exception:
                    pass
            pg.wait_for_timeout(500)
        except Exception:
            pass
        raw = pg.evaluate(_EXTRACT_JS) or []
        rows = []
        for rv in raw:
            if not (rv.get("place_name") or rv.get("review") or rv.get("rating")):
                continue
            rows.append({
                "contributor": contributor,
                "place_name": rv.get("place_name") or "",
                "rating": str(rv.get("rating") or ""),
                "date": rv.get("date") or "",
                "review": rv.get("review") or "",
                "owner_response": rv.get("owner_response") or "",
                "likes": rv.get("likes") or "",
                "language": language or "",
            })
        return rows[:limit] if limit else rows
    finally:
        ctx.close()


async def search(query: str, limit: int | None, language: str) -> list[dict]:
    proxy = settings.PROXY_URL.strip()
    if not proxy:
        raise RuntimeError("Google Maps contributor reviews need a paid residential PROXY_URL in "
                           ".env — Google blocks free/datacenter IPs (no real IP is used).")
    return await asyncio.to_thread(_run, lambda b: _scrape_contrib(b, query, limit, language, proxy))


async def run_job(job_id: str, queries: list[str], limit: int | None, language: str) -> None:
    from .db import jobs, gmaps_contrib_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gmaps_contrib_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
