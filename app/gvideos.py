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
from .scraper import STOP_REQUESTS

BING_V = "https://www.bing.com/videos/search"
GOOGLE_S = "https://www.google.com/search"

GVID_COLUMNS = ["query", "title", "duration", "source", "video_url", "thumbnail"]

_VRHM = re.compile(r'vrhm="([^"]+)"')
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "tiktok.com",
                "facebook.com", "twitch.tv", "rumble.com", "bilibili.com")
_DUR_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")
_BLOCK = ("unusual traffic", "/sorry/", "captcha", "recaptcha", "before you continue",
          "enablejs", "not a robot")


def _host(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").replace("www.", "")
    except Exception:
        return ""


# ---------------- Google Videos (residential-proxy required; Google blocks free/datacenter IPs) ----------------

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
        soup = BeautifulSoup(r.text, "lxml")
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
            block_txt = a.find_parent(["div"]).get_text(" ", strip=True) if a.find_parent("div") else ""
            dm = _DUR_RE.search(block_txt)
            rows.append({"query": query, "title": title, "duration": dm.group(1) if dm else "",
                         "source": host, "video_url": href, "thumbnail": ""})
            if limit and len(rows) >= limit:
                return rows
        if not added:
            break
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
                "thumbnail": m.get("smturl") or "",
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
