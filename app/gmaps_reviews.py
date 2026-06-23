"""Google Maps Reviews Scraper — ALL reviews by rendering Google Maps ourselves (no paid API).

We open the place on google.com/maps in a real (headless) browser, switch to the Reviews tab, scroll
the reviews panel until every review has loaded, and read the cards straight out of the DOM. No
SerpApi / Places API and no per-review cost — only a proxy.

PROXY-ONLY, paid/residential REQUIRED: Google hard-blocks datacenter/free IPs (CAPTCHA / "unusual
traffic"), and this project's free pool already can't reach Maps (see app/gmaps.py). So set a paid
residential `PROXY_URL` in .env; with none set this errors with a clear message (the real IP is
never used for Maps). NOTE: Google Maps' review markup is obfuscated + locale-dependent — the CSS
selectors below are the current known classes and may need one live-tuning pass against a real
proxied response.
"""
import asyncio
import json
import queue
import re
import threading
from datetime import datetime
from urllib.parse import quote, urlparse

from .config import settings

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

GMR_COLUMNS = ["query", "place_name", "place_id", "reviewer", "rating", "date",
               "review", "owner_response", "likes", "language"]

# our sort -> the label shown in Google Maps' "Sort" menu
_SORT = {"newest": "Newest", "relevant": "Most relevant", "most_relevant": "Most relevant",
         "highest": "Highest", "lowest": "Lowest"}

_FID = re.compile(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
_PID = re.compile(r"(ChIJ[A-Za-z0-9_\-]{10,})")

_CATEGORIES = None


def categories() -> list[str]:
    """The Google Maps category list (from app/categories.xlsx), cached, popularity-ordered."""
    global _CATEGORIES
    if _CATEGORIES is None:
        import os
        import openpyxl
        path = os.path.join(os.path.dirname(__file__), "categories.xlsx")
        out = []
        try:
            ws = openpyxl.load_workbook(path, read_only=True).active
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                if row and row[0] and str(row[0]).strip():
                    out.append(str(row[0]).strip())
        except Exception:
            out = []
        _CATEGORIES = out
    return _CATEGORIES


def _feature_id(q: str) -> str | None:
    m = _FID.search(q or "")
    return m.group(1) if m else None


def _place_id(q: str) -> str | None:
    m = _PID.search(q or "")
    return m.group(1) if m else None


# ---- single Playwright worker thread (sync API is thread-affine) ----
_jobs: "queue.Queue" = queue.Queue()
_started = False
_start_lock = threading.Lock()


def _worker():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True,
                                 args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    while True:
        fn, holder, done = _jobs.get()
        try:
            holder.append(fn(browser))
        except Exception as e:
            holder.append(e)
        finally:
            done.set()


def _run(fn):
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


def _proxy_opts(px: str) -> dict:
    u = urlparse(px if "://" in px else "http://" + px)
    o = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        o["username"] = u.username
    if u.password:
        o["password"] = u.password
    return o


def _place_url(query: str, language: str) -> str:
    hl = language or "en"
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q
    pid = _place_id(q)
    if pid:
        return f"https://www.google.com/maps/place/?q=place_id:{pid}&hl={hl}"
    fid = _feature_id(q)
    if fid:
        return f"https://www.google.com/maps/place/?q={fid}&hl={hl}"
    return f"https://www.google.com/maps/search/{quote(q)}?hl={hl}"


# JS run inside the page to read every loaded review card into plain objects.
_EXTRACT_JS = r"""
() => {
  const pick = (el, sels) => { for (const s of sels) { const e = el.querySelector(s); if (e) return e; } return null; };
  const cards = Array.from(document.querySelectorAll('div.jftiEf, div[data-review-id]'));
  return cards.map(c => {
    const nameEl = pick(c, ['.d4r55', '.TSUbDb', '.WNxzHc']);
    const starEl = pick(c, ['span[role="img"][aria-label*="star" i]', '.kvMYJc[aria-label]', 'span[aria-label*="star" i]']);
    const aria = starEl ? (starEl.getAttribute('aria-label') || '') : '';
    const rm = aria.match(/([0-9]+(?:[.,][0-9])?)/);
    const textEl = pick(c, ['.wiI7pd', '.MyEned', '.review-full-text']);
    const dateEl = pick(c, ['.rsqaWe', '.xRkPPb', '.DU9Pgb']);
    const respWrap = pick(c, ['.CDe7pd', '.wiI7pd.CDe7pd']);
    const respEl = respWrap ? pick(respWrap, ['.wiI7pd', '.review-full-text']) || respWrap : null;
    const likeEl = pick(c, ['.pkWtMe', '[aria-label*="helpful" i]', '.GBkF3d']);
    const t = (e) => e ? (e.textContent || '').trim() : '';
    return {
      reviewer: t(nameEl),
      rating: rm ? rm[1].replace(',', '.') : '',
      date: t(dateEl),
      review: t(textEl),
      owner_response: respEl ? (respEl.textContent || '').replace(/^Response from the owner/i, '').trim() : '',
      likes: likeEl ? ((likeEl.textContent || '').replace(/\D/g, '')) : '',
    };
  });
}
"""


def _looks_blocked(pg) -> str | None:
    """Return a reason string if Google served a consent/CAPTCHA/blocked page, else None."""
    u = (pg.url or "").lower()
    if "consent.google" in u or "/sorry/" in u:
        return "Google consent/sorry interstitial"
    try:
        low = (pg.content() or "").lower()
    except Exception:
        return None
    if "unusual traffic" in low or "captcha" in low or "recaptcha" in low:
        return "Google bot-check (CAPTCHA / unusual traffic)"
    return None


def _accept_consent(pg) -> None:
    for sel in ['button[aria-label*="Accept all" i]', 'button[aria-label*="Reject all" i]',
                'form[action*="consent"] button', 'button:has-text("Accept all")']:
        try:
            el = pg.query_selector(sel)
            if el:
                el.click()
                pg.wait_for_timeout(1500)
                return
        except Exception:
            pass


def _open_reviews(pg, sort: str) -> None:
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
    # switch to the Reviews tab
    for sel in ['button[aria-label*="Reviews for" i]', 'button[jsaction*="reviewChart"]',
                'button[role="tab"]:has-text("Reviews")', 'button:has-text("Reviews")',
                '[aria-label*="More reviews" i]']:
        try:
            el = pg.query_selector(sel)
            if el:
                el.click()
                pg.wait_for_timeout(2200)
                break
        except Exception:
            pass
    # apply the sort order via the Sort menu
    want = _SORT.get((sort or "newest").lower())
    if want:
        try:
            btn = pg.query_selector('button[aria-label*="Sort" i], button[data-value="Sort"]')
            if btn:
                btn.click()
                pg.wait_for_timeout(900)
                for it in pg.query_selector_all('[role="menuitemradio"], [role="menuitem"]'):
                    if want.lower() in (it.text_content() or "").lower():
                        it.click()
                        pg.wait_for_timeout(1800)
                        break
        except Exception:
            pass


def _scroll_reviews(pg, limit: int | None) -> None:
    cap = 80 if not limit else max(10, (limit // 8) + 10)
    last, stale = 0, 0
    for _ in range(cap):
        try:
            pg.evaluate("""() => {
              const c = document.querySelector('div.jftiEf, div[data-review-id]');
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
        pg.wait_for_timeout(1200)
        n = len(pg.query_selector_all('div.jftiEf, div[data-review-id]'))
        if limit and n >= limit:
            break
        stale = stale + 1 if n <= last else 0
        last = n
        if stale >= 3:
            break


# ---------- internal review-API interception (Option B) ----------
# As the page loads/scrolls, Google fetches reviews from its own RPC (listentitiesreviews /
# listugcposts). The browser builds the (un-buildable-by-hand) protobuf; we just capture the
# RESPONSE bodies and read the reviews out of them — i.e. the internal API's JSON, not the HTML DOM.
_REVIEW_RPC = ("listentitiesreviews", "listugcposts", "/preview/review", "listentityreviews")
# Google review dates are relative ("2 weeks ago", "a month ago") or contain a year. We avoid a
# month-name pattern on purpose — it false-matches names like "Jane" (Jan) / "Marcus" (Mar).
_DATE_RE = re.compile(r"\bago\b|\b20\d{2}\b", re.I)


def _attach_rpc_capture(pg):
    """Collect the raw bodies of Google's internal review-RPC responses for this page."""
    bodies: list[str] = []

    def _on_response(resp):
        try:
            u = (resp.url or "").lower()
            if any(k in u for k in _REVIEW_RPC):
                bodies.append(resp.text())
        except Exception:
            pass

    pg.on("response", _on_response)
    return bodies


def _rpc_json(body: str):
    """Strip Google's `)]}'` XSSI guard and parse the response body to a Python list."""
    if not body:
        return None
    s = body.lstrip()
    if s.startswith(")]}'"):
        s = s[4:]
    i = s.find("[")
    if i < 0:
        return None
    try:
        return json.loads(s[i:])
    except Exception:
        return None


def _flat_strings(node, out, depth=0):
    if depth > 22:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            _flat_strings(x, out, depth + 1)


def _collect_rpc_reviews(node, rows, seen, depth=0):
    """Walk the nested-array RPC payload and pull out review entries. A review entry is a list with
    a DIRECT 1–5 rating whose (small) subtree also holds a long review text — Google nests the text
    a level or two below the rating. Author + date are grabbed best-effort from the same subtree."""
    if depth > 45 or not isinstance(node, list):
        return
    direct_ratings = [x for x in node if isinstance(x, int) and 1 <= x <= 5]
    if direct_ratings:
        allstr: list[str] = []
        _flat_strings(node, allstr)
        texts = [x for x in allstr if 12 <= len(x) <= 4000 and " " in x and not x.startswith("http")]
        if texts and len(allstr) <= 80:          # small subtree => a single review entry
            text = max(texts, key=len)
            key = text[:120]
            if key not in seen:
                seen.add(key)
                cands = [x for x in allstr if 1 < len(x) <= 60 and x is not text
                         and not x.startswith("http") and not _DATE_RE.search(x)
                         and not x.isdigit()]
                # author/place names usually have a space; prefer those over id-like tokens
                author = next((x for x in cands if " " in x), cands[0] if cands else "")
                date = next((x for x in allstr if _DATE_RE.search(x) and len(x) <= 40), "")
                rows.append({"author": author, "rating": str(direct_ratings[0]),
                             "date": date, "text": text})
            return                                # matched — don't double-count inside it
    for x in node:
        _collect_rpc_reviews(x, rows, seen, depth + 1)


def _parse_rpc_reviews(bodies: list[str]) -> list[dict]:
    rows, seen = [], set()
    for b in bodies:
        data = _rpc_json(b)
        if data is not None:
            _collect_rpc_reviews(data, rows, seen)
    return rows


def _scrape_place(browser, query: str, sort: str, limit: int | None, language: str,
                  proxy: str) -> list[dict]:
    url = _place_url(query, language)
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
        _open_reviews(pg, sort)
        _scroll_reviews(pg, limit)
        place_id = _place_id(query) or ""
        place_name = ""
        try:
            h = pg.query_selector("h1.DUwDvf, h1.fontHeadlineLarge")
            place_name = (h.text_content() or "").strip() if h else ""
        except Exception:
            pass
        # PRIMARY: reviews straight from the captured internal review API (JSON, not HTML)
        api = _parse_rpc_reviews(rpc_bodies)
        if api:
            rows = [{
                "query": query, "place_name": place_name, "place_id": place_id,
                "reviewer": a.get("author") or "", "rating": str(a.get("rating") or ""),
                "date": a.get("date") or "", "review": a.get("text") or "",
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
            if not (rv.get("reviewer") or rv.get("review") or rv.get("rating")):
                continue
            rows.append({
                "query": query,
                "place_name": place_name,
                "place_id": place_id,
                "reviewer": rv.get("reviewer") or "",
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


async def search(query: str, sort: str, limit: int | None, language: str) -> list[dict]:
    proxy = settings.PROXY_URL.strip()
    if not proxy:
        raise RuntimeError("Google Maps reviews scraping needs a paid residential PROXY_URL in .env "
                           "— Google blocks free/datacenter IPs (no real IP is used).")
    return await asyncio.to_thread(_run, lambda b: _scrape_place(b, query, sort, limit, language, proxy))


async def run_job(job_id: str, queries: list[str], sort: str, limit: int | None,
                  language: str) -> None:
    from .db import jobs, gmaps_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, sort, limit, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gmaps_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
