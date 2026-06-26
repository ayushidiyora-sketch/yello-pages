"""YouTube Search Scraper — video search results from youtube.com.

Reads YouTube's own embedded `ytInitialData` from the results page (the data its web app consumes)
through a proxy IP (paid PROXY_URL if set, else the rotating free pool — the real IP is never used).
Unlike the transcript endpoint, the search page works on the FREE pool (with a consent cookie).
Pagination beyond the first page uses YouTube's internal `youtubei/v1/search` continuation API.
A query is a plain search phrase; `limit` caps the videos.
"""
import asyncio
import json
import re
import time
from datetime import datetime
from urllib.parse import quote_plus

from curl_cffi import requests as cffi

from .config import settings

YTS_COLUMNS = ["query", "video_id", "title", "channel", "views", "duration", "published",
               "url", "thumbnail"]

_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"   # YouTube's public web INNERTUBE key
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HDR = {"Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+cb", "User-Agent": _UA}
_CTX = {"client": {"clientName": "WEB", "clientVersion": "2.20240726.00.00", "hl": "en", "gl": "US"}}


def _proxies() -> list[str]:
    px = settings.PROXY_URL.strip()
    if px:
        return [px]
    from . import yp_us
    yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "New York, NY", "page": "1"}, 8)
    with yp_us._LOCK:
        return list(yp_us._GOOD)[:8]


def _runs(o) -> str:
    o = o or {}
    return "".join(r.get("text", "") for r in (o.get("runs") or [])) or o.get("simpleText", "")


def _video_row(vr: dict, query: str) -> dict | None:
    vid = vr.get("videoId")
    if not vid:
        return None
    thumbs = (vr.get("thumbnail") or {}).get("thumbnails") or []
    return {
        "query": query, "video_id": vid,
        "title": _runs(vr.get("title")),
        "channel": _runs(vr.get("ownerText") or vr.get("longBylineText")),
        "views": _runs(vr.get("viewCountText")),
        "duration": _runs(vr.get("lengthText")),
        "published": _runs(vr.get("publishedTimeText")),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "thumbnail": thumbs[-1].get("url", "") if thumbs else "",
    }


def _walk_videos(node, out: list):
    if isinstance(node, dict):
        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            out.append(node["videoRenderer"])
        for v in node.values():
            _walk_videos(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_videos(v, out)


def _continuation_tokens(data) -> list[str]:
    """All distinct continuation tokens in a response (search has several chains)."""
    toks: list[str] = []

    def w(n):
        if isinstance(n, dict):
            cc = n.get("continuationCommand")
            if isinstance(cc, dict) and cc.get("token") and cc["token"] not in toks:
                toks.append(cc["token"])
            for v in n.values():
                w(v)
        elif isinstance(n, list):
            for v in n:
                w(v)
    w(data)
    return toks


def _collect(data, query: str, rows: list, seen: set, want: int) -> bool:
    vrs = []
    _walk_videos(data, vrs)
    added = False
    for vr in vrs:
        row = _video_row(vr, query)
        if row and row["video_id"] not in seen:
            seen.add(row["video_id"])
            rows.append(row)
            added = True
            if len(rows) >= want:
                break
    return added


def _post_continuation(token: str, px_list: list[str], tries: int = 4):
    """POST the continuation token, trying up to `tries` proxies until one returns JSON.

    On success the working proxy is moved to the front of px_list (cheap stickiness) so
    the next page reuses it. Bounded to `tries` proxies (timeout 12s each) so a dead pool
    can't stall a page for minutes. Returns the decoded JSON, or None after the tries fail.
    """
    for i, px in enumerate(list(px_list)[:tries]):
        try:
            r = cffi.post(f"https://www.youtube.com/youtubei/v1/search?key={_KEY}",
                          json={"context": _CTX, "continuation": token}, impersonate="chrome",
                          headers={**_HDR, "Content-Type": "application/json"},
                          proxies={"http": px, "https": px}, timeout=12, verify=False)
            jd = r.json()
            if isinstance(jd, dict) and jd:
                if i and px in px_list:                   # promote the proxy that worked
                    px_list.remove(px)
                    px_list.insert(0, px)
                return jd
        except Exception:
            continue
    return None


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    want = limit or 1000
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    html, proxy = None, None
    for px in _proxies():
        try:
            r = cffi.get(url, impersonate="chrome", headers=_HDR,
                         proxies={"http": px, "https": px}, timeout=20, verify=False)
            if r is not None and r.status_code == 200 and len(r.text) > 100000 \
                    and "ytInitialData" in r.text:
                html, proxy = r.text, px
                break
        except Exception:
            continue
    if not html:
        return []
    m = re.search(r"ytInitialData\s*=\s*(\{.+?\})\s*;</script>", html)
    if not m:
        return []
    data = json.loads(m.group(1))
    rows, seen = [], set()
    _collect(data, query, rows, seen, want)

    # Proxy list: the proxy that served the first page, then the rest as fallbacks (deduped).
    px_list = [proxy] + [p for p in _proxies() if p != proxy]

    # Follow every continuation chain (search returns several). A token may yield
    # nothing new yet still hand back the token that *does* — so never stop on one
    # empty page; only stop when the frontier is exhausted, want is met, or page cap.
    # Each token is retried once across proxies before being dropped, so a single flaky
    # free-proxy timeout doesn't truncate the whole result set.
    frontier = _continuation_tokens(data)
    used: set[str] = set()
    retried: set[str] = set()
    pages = 0
    misses = 0                                            # consecutive dead pages → bail (pool down)
    deadline = time.monotonic() + 90                      # hard wall-clock cap per query
    while frontier and len(rows) < want and pages < 120:
        if misses >= 5 or time.monotonic() > deadline:    # circuit breaker
            break
        token = frontier.pop(0)
        if token in used:
            continue
        pages += 1
        jd = _post_continuation(token, px_list)
        if jd is None:
            misses += 1
            if token not in retried:                      # one retry before giving up on the branch
                retried.add(token)
                frontier.append(token)
            else:
                used.add(token)
            continue
        misses = 0
        used.add(token)
        _collect(jd, query, rows, seen, want)
        for t in _continuation_tokens(jd):
            if t not in used and t not in frontier:
                frontier.append(t)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from .db import jobs, yt_search
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await yt_search.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("YouTube returned 0 — the free proxy may have been blocked; try again "
                            "(it rotates proxies). The real IP is never used.")
        elif limit and total < limit:
            done["note"] = (f"YouTube served {total} video(s) for this query — search results are "
                            f"finite per query and vary per run; try a more specific query for more. "
                            f"The real IP is never used.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
