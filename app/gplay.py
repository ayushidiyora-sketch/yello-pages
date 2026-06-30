"""Google Play Reviews Scraper — reviews from Play Store apps via Google Play's internal batchexecute API.

Uses Play Store's own RPC (`UsvDTd` on `/_/PlayStoreUi/data/batchexecute`) — the same internal API the
Play web UI calls. Returns JSON (nested arrays); the field indices are the well-known stable ones used
by google-play-scraper, so this is a precise parser (not best-effort). Works through the proxy pool
(`yp_us` — paid PROXY_URL if set, else a free-pool proxy; the real IP is never used). Unlike Google
Search, Play's batchexecute is reachable on the free pool. Input = an app id (com.foo.bar) or a Play
Store URL with `?id=...`.
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .gsjobs import _balanced_array        # string-aware nested-array JSON parser (reused)
from .scraper import STOP_REQUESTS

BATCH = "https://play.google.com/_/PlayStoreUi/data/batchexecute"

GPL_COLUMNS = ["app_id", "author", "author_image", "rating", "date", "review", "likes",
               "app_version", "reply", "review_id"]

# UI sort -> Play API sort code
_SORT = {"relevant": 1, "most_relevant": 1, "newest": 2, "rating": 3, "helpfulness": 1}

_APPID = re.compile(r"id=([A-Za-z0-9_.]+)")


def _app_id(s: str) -> str:
    s = (s or "").strip()
    m = _APPID.search(s)
    if m:
        return m.group(1)
    return "" if s.startswith("http") else s   # bare id like com.skype.raider


def _proxy_list() -> list[str | None]:
    """Proxies to try, in order: a paid PROXY_URL if set, else the warm free-pool proxies (rotated —
    a single bad free proxy must not zero out the whole job). Never the real IP."""
    px = settings.PROXY_URL.strip()
    if px:
        return [px]
    try:
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        if not warm:
            yp_us.ensure_pool({"search_terms": "Dentists", "geo_location_terms": "New York, NY",
                               "page": "1"}, 6)
            with yp_us._LOCK:
                warm = list(yp_us._GOOD)
        return warm[:8]
    except Exception:
        return []


def _reviews_page(app_id, sort, count, token, lang, country, proxy):
    """One batchexecute call → (list of review dicts, next_page_token | None)."""
    inner = json.dumps([None, None, [2, sort, [count, None, token], None, []], [app_id, 7]])
    freq = json.dumps([[["UsvDTd", inner, None, "generic"]]])
    params = {"rpcids": "UsvDTd", "hl": lang or "en", "gl": (country or "us").upper(),
              "source-path": "/store/apps/details"}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = cffi.post(BATCH + "?" + urlencode(params), data={"f.req": freq}, impersonate="chrome",
                      proxies=proxies, timeout=12, verify=False,
                      headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"})
    except Exception:
        return [], None      # dead/slow proxy -> caller rotates to the next one
    if r is None or r.status_code != 200:
        return [], None
    txt = r.text or ""
    idx = txt.find('[["wrb.fr","UsvDTd"')
    if idx < 0:
        return [], None
    env = _balanced_array(txt, idx)
    if not env or not env[0] or not isinstance(env[0][2], str):
        return [], None
    try:
        payload = json.loads(env[0][2])
    except Exception:
        return [], None
    raw = payload[0] or []
    token2 = payload[1][1] if len(payload) > 1 and payload[1] else None
    rows = []
    for rv in raw:
        try:
            ts = rv[5][0] if rv[5] else 0
            try:
                author_image = rv[1][1][3][2] or ""
            except Exception:
                author_image = ""
            rows.append({
                "app_id": app_id,
                "review_id": rv[0] or "",
                "author": (rv[1] or [""])[0] or "",
                "author_image": author_image,
                "rating": rv[2] or "",
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "",
                "review": rv[4] or "",
                "likes": rv[6] or 0,
                "app_version": rv[10] if len(rv) > 10 and rv[10] else "",
                "reply": rv[7][1] if rv[7] and len(rv[7]) > 1 else "",
            })
        except Exception:
            continue
    return rows, token2


def search_sync(query: str, limit: int | None = None, sort: str = "relevant", language: str = "en",
                country: str = "us", job_id: str | None = None) -> list[dict]:
    app_id = _app_id(query)
    if not app_id:
        raise RuntimeError(f"Could not read an app id from '{query}' (use com.foo.bar or a Play "
                           "Store URL with ?id=...).")
    proxies = _proxy_list()
    if not proxies:
        raise RuntimeError("No proxy available to reach Google Play (wait for the free pool to warm "
                           "up, or set a PROXY_URL). The real IP is never used.")
    import time
    sort_code = _SORT.get((sort or "relevant").lower(), 1)
    rows, token, proxy = [], None, None
    for _page in range(60):                      # hard page cap
        if job_id and job_id in STOP_REQUESTS:
            break
        want = max(1, min(100, (limit - len(rows)) if limit else 100))  # Play is flaky above ~100
        # An empty FIRST page is almost always a transient rate-limit (every app has reviews), so
        # retry the whole rotation a few times; an empty LATER page genuinely means "no more".
        attempts = 4 if token is None else 1
        batch, tok = [], None
        for _try in range(attempts):
            for px in ([proxy] if proxy else []) + proxies:
                if job_id and job_id in STOP_REQUESTS:
                    break
                batch, tok = _reviews_page(app_id, sort_code, want, token, language, country, px)
                if batch:
                    proxy = px                   # lock onto the proxy that works
                    break
            if batch or (job_id and job_id in STOP_REQUESTS):
                break
            proxy = None                         # working proxy went empty -> re-rotate fresh
            time.sleep(1.2)
        if not batch:
            break
        rows += batch
        token = tok
        if (limit and len(rows) >= limit) or not token:
            break
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, sort: str = "relevant", language: str = "en",
                 country: str = "us", job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort, language, country, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str,
                  language: str) -> None:
    from .db import jobs, gplay_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, sort, language, "us", job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gplay_results.insert_many(rows)
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
