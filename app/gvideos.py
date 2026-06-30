"""Google Search Videos Scraper — video results for a query via Bing's video search (proxy-only).

Free, no key: fetches Bing's video-search page through the proxy pool (`yp_us.pooled_get` — paid
PROXY_URL if set, else a free-pool proxy; the REAL IP is never used) and reads the JSON embedded in
each result cell (the `vrhm="{…}"` attribute). DuckDuckGo's video API is blocked and Google Videos
is anti-bot; Bing is the reliable free source. Each query → video rows: title, duration, source,
video URL, thumbnail.
"""
import asyncio
import html
import json
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from . import yp_us
from .gsjobs import _balanced_array          # string-aware nested-array JSON parser (reused)
from .scraper import STOP_REQUESTS

BING_V = "https://www.bing.com/videos/search"
GOOGLE_S = "https://www.google.com/search"

GVID_COLUMNS = ["query", "title", "duration", "source", "video_url", "thumbnail"]

_VRHM = re.compile(r'vrhm="([^"]+)"')
# Google embeds the video results (its internal API payload) in AF_initDataCallback({...}).
_AF_DATA = re.compile(r"AF_initDataCallback\(\{[^{}]*?data:\s*(\[)", re.DOTALL)
_URLISH = re.compile(r"^https?://")
_DUR_TS = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_THUMB_HOST = ("ytimg.com", "googleusercontent.com", "ggpht.com", "i.ytimg")
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "tiktok.com",
                "facebook.com", "twitch.tv", "rumble.com", "bilibili.com")
_DUR_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")
_BLOCK = ("unusual traffic", "/sorry/", "captcha", "recaptcha", "before you continue",
          "enablejs", "not a robot")


_YT_ID = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/|/watch\?.*?v=)([A-Za-z0-9_-]{11})")


def _host(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").replace("www.", "")
    except Exception:
        return ""


def _yt_thumb(url: str) -> str:
    """YouTube thumbnails are reliably derivable from the video id (i.ytimg.com never hotlink-blocks)."""
    m = _YT_ID.search(url or "")
    return f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg" if m else ""


# ---------------- Google Videos (residential-proxy required; Google blocks free/datacenter IPs) ----------------

def _flat_strings(node, out, depth=0):
    if depth > 8:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            _flat_strings(x, out, depth + 1)


def _collect_videos(node, rows, seen, query, depth=0):
    """Walk Google's internal video payload. A video ENTRY is a list with >=2 direct string children
    (title/duration/url) whose subtree holds a video-host URL — so each entry matches individually,
    not its list-of-lists parent. Best-effort — finalize indices on a real proxied response."""
    if depth > 60 or not isinstance(node, list):
        return
    direct = [x for x in node if isinstance(x, str)]
    if len(direct) >= 2:
        sub = []
        _flat_strings(node, sub)
        vurl = next((x for x in sub if _URLISH.match(x) and any(v in x for v in _VIDEO_HOSTS)), "")
        if vurl and vurl not in seen:
            titles = [x for x in direct if 3 <= len(x) <= 200 and not _URLISH.match(x) and " " in x]
            if titles:
                seen.add(vurl)
                dur = next((x for x in direct if _DUR_TS.match(x)), "")
                thumb = next((x for x in sub if _URLISH.match(x)
                              and any(t in x for t in _THUMB_HOST)), "")
                rows.append({"query": query, "title": max(titles, key=len), "duration": dur,
                             "source": _host(vurl), "video_url": vurl,
                             "thumbnail": _yt_thumb(vurl) or thumb})
                return
    for x in node:
        _collect_videos(x, rows, seen, query, depth + 1)


def _google_internal_videos(html_text: str, query: str) -> list[dict]:
    """PRIMARY: read videos straight from Google's embedded internal data (AF_initDataCallback)."""
    rows, seen = [], set()
    for m in _AF_DATA.finditer(html_text):
        arr = _balanced_array(html_text, m.start(1))
        if arr is not None:
            _collect_videos(arr, rows, seen, query)
    return rows


def _google_dom_videos(soup, query: str, seen: set, rows: list) -> int:
    """Fallback: read video links from the rendered Google results HTML."""
    added = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/url?"):
            href = (parse_qs(urlparse(href).query).get("q") or [""])[0]
        if not href.startswith("http"):
            continue
        host = _host(href)
        if not any(v in host for v in _VIDEO_HOSTS) or href in seen:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            h = a.find(["h3", "div"], role="heading") or a.find("h3")
            title = h.get_text(" ", strip=True) if h else ""
        if not title or len(title) < 3:
            continue
        seen.add(href)
        added += 1
        block_txt = a.find_parent("div").get_text(" ", strip=True) if a.find_parent("div") else ""
        dm = _DUR_RE.search(block_txt)
        rows.append({"query": query, "title": title, "duration": dm.group(1) if dm else "",
                     "source": host, "video_url": href, "thumbnail": ""})
    return added


def _google_videos(query: str, limit: int | None, country: str, language: str) -> list[dict] | None:
    """Scrape Google's video search (tbm=vid) through the proxy. Returns rows, or None when Google
    blocked the request / returned nothing parseable → caller falls back to Bing.
    NOTE: Google's video HTML is obfuscated; this best-effort parser may need one live-tuning pass
    against a real residential-proxy response."""
    headers = {"Accept-Language": f"{(language or 'en')}-{(country or 'us').upper()},"
                                  f"{(language or 'en')};q=0.9"}
    rows, seen = [], set()
    for start in range(0, 60, 20):
        params = {"q": query, "tbm": "vid", "num": "20", "start": str(start),
                  "hl": language or "en", "gl": (country or "us").lower()}
        r = yp_us.pooled_get(GOOGLE_S, params, timeout=20, headers=headers)
        if r is None or r.status_code != 200:
            return None if not rows else rows
        low = (r.text or "").lower()
        if any(b in low for b in _BLOCK):       # Google blocked this IP -> fall back to Bing
            return None if not rows else rows
        # PRIMARY: Google's internal video data (AF_initDataCallback); fallback: rendered DOM links
        before = len(rows)
        for v in _google_internal_videos(r.text, query):
            if v["video_url"] not in seen:
                seen.add(v["video_url"])
                rows.append(v)
                if limit and len(rows) >= limit:
                    return rows
        if len(rows) == before:                 # internal data empty -> DOM fallback
            _google_dom_videos(BeautifulSoup(r.text, "lxml"), query, seen, rows)
        if len(rows) <= before:
            break
        if limit and len(rows) >= limit:
            return rows[:limit]
    return rows or None


def _bing_videos(query: str, limit: int | None, country: str,
                 language: str, job_id: str | None) -> list[dict]:
    headers = {"Accept-Language": f"{(language or 'en')}-{(country or 'us').upper()},"
                                  f"{(language or 'en')};q=0.9"}
    rows: list[dict] = []
    seen, first = set(), 1
    for _page in range(15):                       # video pages, hard cap
        if job_id and job_id in STOP_REQUESTS:    # Stop button pressed mid-pagination
            break
        params = {"q": query, "first": str(first), "count": "35"}
        r = yp_us.pooled_get(BING_V, params, timeout=20, headers=headers)
        if r is None or r.status_code != 200:
            if rows:
                break
            raise RuntimeError("Could not reach Bing video search through a proxy (set a PROXY_URL, "
                               "or wait for the free pool to warm up). The real IP is never used.")
        cells = _VRHM.findall(r.text)
        if not cells:
            break
        added = 0
        for c in cells:
            try:
                m = json.loads(html.unescape(c))
            except Exception:
                continue
            vurl = m.get("murl") or m.get("pgurl") or ""
            if not vurl or vurl in seen:
                continue
            seen.add(vurl)
            added += 1
            rows.append({
                "query": query,
                "title": m.get("vt") or "",
                "duration": m.get("du") or "",
                "source": _host(m.get("pgurl") or vurl),
                "video_url": vurl,
                "thumbnail": _yt_thumb(vurl) or m.get("smturl") or "",
            })
            if limit and len(rows) >= limit:
                return rows
        if not added:
            break
        first += 35
    return rows


def search_sync(query: str, limit: int | None = None, country: str = "us",
                language: str = "en", job_id: str | None = None) -> list[dict]:
    """Google video search first (needs a residential PROXY_URL — Google blocks free/datacenter IPs);
    fall back to Bing video search (works on the free pool) when Google is blocked/empty."""
    try:
        g = _google_videos(query, limit, country, language)
    except Exception:
        g = None
    if g:
        return g[:limit] if limit else g
    return _bing_videos(query, limit, country, language, job_id)


async def search(query: str, limit: int | None = None, country: str = "us",
                 language: str = "en", job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, country, language, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None, country: str,
                  language: str) -> None:
    from .db import jobs, gvideos_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:               # Stop button pressed
                break
            rows = await search(q, limit, country, language, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gvideos_results.insert_many(rows)
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
