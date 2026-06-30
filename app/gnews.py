"""Google Search News Scraper — news articles for a query via Google News RSS (proxy-only).

Fetches Google News' public RSS search feed through the proxy pool (`yp_us.pooled_get` — the paid
PROXY_URL if set, else a warm free-pool proxy; the REAL IP is never used). No API key. Each query
maps to news.google.com/rss/search?q=<query>; country + language set hl/gl/ceid, and the date range
maps to Google News' `when:` operator. Returns title, source, date, link, snippet per article.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

# date-range window -> how far back to keep articles (hard post-filter on pubDate)
_RANGE_DELTA = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7),
                "month": timedelta(days=31), "year": timedelta(days=366)}

RSS = "https://news.google.com/rss/search"

GNEWS_COLUMNS = ["query", "title", "source", "date", "link", "snippet", "image"]

# UI date range -> Google News `when:` operator (empty = any time)
DATE_MAP = {"": "", "any": "", "hour": "when:1h", "day": "when:1d", "week": "when:7d",
            "month": "when:1m", "year": "when:1y"}

_TAG = re.compile(r"<[^>]+>")
_OG_RE = re.compile(r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I)
_OG_RE2 = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image', re.I)


def _params(query: str, date_range: str, country: str, language: str) -> dict:
    op = DATE_MAP.get((date_range or "any").lower(), "")
    gl = (country or "us").upper()
    lang = (language or "en").lower()
    return {"q": f"{query} {op}".strip(), "hl": f"{lang}-{gl}", "gl": gl, "ceid": f"{gl}:{lang}"}


def _strip(html_txt: str) -> str:
    return unescape(_TAG.sub("", html_txt or "")).strip()


def _in_range(pubdate: str, date_range: str, now: datetime) -> bool:
    """True if the article's pubDate falls within the selected window (or no window selected)."""
    delta = _RANGE_DELTA.get((date_range or "").lower())
    if not delta:
        return True
    try:
        dt = parsedate_to_datetime(pubdate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= now - delta
    except Exception:
        return True   # unparseable date -> don't drop it


def search_sync(query: str, limit: int | None = None, date_range: str = "", country: str = "us",
                language: str = "en") -> list[dict]:
    r = yp_us.pooled_get(RSS, _params(query, date_range, country, language), timeout=20)
    if r is None:
        raise RuntimeError("No proxy available to reach Google News (set a PROXY_URL, or wait for "
                           "the free pool to warm up). The real IP is never used.")
    rows: list[dict] = []
    try:
        root = ET.fromstring(r.text)
    except Exception:
        return rows
    now = datetime.now(timezone.utc)
    cap = limit or 100000
    for item in root.findall(".//item"):
        def g(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        pubdate = g("pubDate")
        if not _in_range(pubdate, date_range, now):   # hard date-range filter
            continue
        title = g("title")
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
        # Google News titles end with " - <source>"; trim it for a clean headline
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()
        rows.append({
            "query": query,
            "title": title,
            "source": source,
            "date": pubdate,
            "link": g("link"),
            "snippet": _strip(g("description")),
            "image": "",
        })
        if len(rows) >= cap:
            break
    return rows


_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+")


def _proxy() -> str | None:
    """A proxy to route through: paid PROXY_URL if set, else a warm free-pool proxy (never real IP)."""
    px = settings.PROXY_URL.strip()
    if px:
        return px
    try:
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        if not warm:
            yp_us.ensure_pool({"search_terms": "Dentists", "geo_location_terms": "New York, NY",
                               "page": "1"}, 4)
            with yp_us._LOCK:
                warm = list(yp_us._GOOD)
        return warm[0] if warm else None
    except Exception:
        return None


def _b64_url(art_id: str) -> str:
    """Fast path: older AU_yqL ids base64-decode to bytes containing the target URL as a substring."""
    try:
        import base64
        raw = base64.urlsafe_b64decode(art_id + "===").decode("latin1", "ignore")
        for m in _URL_RE.finditer(raw):
            u = m.group(0)
            if "google.com" not in u and "gstatic" not in u and len(u) > 12:
                return u
    except Exception:
        pass
    return ""


def _decode_real_url(link: str, proxies) -> str:
    """Decode a Google News article link to the real publisher URL. Modern (CBMi…) ids are opaque,
    so we read the article page's signature+timestamp and POST Google's batchexecute decode API."""
    art_id = link.split("/articles/")[-1].split("?")[0].rstrip("/")
    if not art_id:
        return ""
    fast = _b64_url(art_id)
    if fast:
        return fast
    try:
        r = cffi.get(f"https://news.google.com/rss/articles/{art_id}", impersonate="chrome",
                     proxies=proxies, timeout=10, verify=False)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text or "")
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text or "")
        if not (sg and ts):
            return ""
        inner = ["garturlreq",
                 [["en-US", "US", ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"], None, None, 1, 1,
                   "US:en", None, 180, None, None, None, None, None, 0, None, None,
                   [1608992183, 723341000]],
                  "en-US", "US", 1, [2, 3, 4, 8], 1, 0, "655000234", 0, 0, None, 0],
                 art_id, int(ts.group(1)), sg.group(1)]
        freq = json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])
        r2 = cffi.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                       data={"f.req": freq}, impersonate="chrome", proxies=proxies,
                       timeout=10, verify=False)
        t2 = r2.text or ""
        i = t2.find("[")
        arr = json.loads(t2[i:]) if i >= 0 else []
        for row in arr:
            if isinstance(row, list) and len(row) > 2 and row[1] == "Fbv4je" and isinstance(row[2], str):
                got = json.loads(row[2])
                if isinstance(got, list) and len(got) > 1 and isinstance(got[1], str) \
                        and got[1].startswith("http"):
                    return got[1]
    except Exception:
        pass
    return ""


def _og_image_sync(link: str) -> str:
    """Article thumbnail: decode the Google News link to the real article, read its og:image (proxy).
    Returns '' (not the Google News logo) when it can't be decoded/fetched."""
    px = _proxy()
    if not px:
        return ""
    proxies = {"http": px, "https": px}
    real = _decode_real_url(link, proxies)
    if not real:
        return ""
    try:
        r = cffi.get(real, impersonate="chrome", proxies=proxies, timeout=8, verify=False,
                     allow_redirects=True)
        if r is None or r.status_code >= 400:
            return ""
        m = _OG_RE.search(r.text or "") or _OG_RE2.search(r.text or "")
        if not m:
            return ""
        img = unescape(m.group(1)).strip()
        if not img or "news.google" in img or "/gnews" in img:
            return ""
        return img
    except Exception:
        return ""


async def _fill_images(rows: list[dict]) -> None:
    """Populate each row's `image` (og:image) concurrently — best-effort and TIME-BOXED so the job
    never hangs on slow/blocking publisher sites (success is much higher with a paid PROXY_URL)."""
    sem = asyncio.Semaphore(16)

    async def _one(r):
        async with sem:
            try:
                r["image"] = await asyncio.to_thread(_og_image_sync, r.get("link") or "")
            except Exception:
                pass
    try:
        await asyncio.wait_for(
            asyncio.gather(*[_one(r) for r in rows], return_exceptions=True), timeout=45)
    except asyncio.TimeoutError:
        pass    # proceed with whatever images were captured; the rest stay ""


async def search(query: str, limit: int | None = None, date_range: str = "", country: str = "us",
                 language: str = "en") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, date_range, country, language)


async def run_job(job_id: str, queries: list[str], limit: int | None, date_range: str,
                  country: str, language: str) -> None:
    from .db import jobs, gnews_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:               # Stop button pressed
                break
            rows = await search(q, limit, date_range, country, language)
            if job_id not in STOP_REQUESTS:           # skip slow image enrichment if stopping
                await _fill_images(rows)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gnews_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "stopped" if stopped else "done", "total_scraped": total,
            "finished_at": datetime.utcnow()}})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
