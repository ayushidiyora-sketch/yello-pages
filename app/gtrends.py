"""Google Trends Scraper — interest-by-region for a query via Google Trends' free internal API.

Google Trends (trends.google.com) exposes the same keyless JSON API its own frontend uses:
  1) GET /trends/api/explore                 -> widget tokens (TIMESERIES, GEO_MAP, …)
  2) GET /trends/api/widgetdata/comparedgeo  -> the geo breakdown (interest by region/city/DMA)
Both responses are prefixed with `)]}',` then JSON. A one-time cookie (NID) is needed first, so we
open a curl_cffi Session, hit the Trends home page, then call explore + comparedgeo on that SAME
session + proxy.

PROXY-ONLY: every request goes through a proxy (paid PROXY_URL if set, else the rotating free US
pool; the REAL IP is never used). Google rate-limits (429) free/datacenter IPs, so a paid residential
PROXY_URL is the most reliable; on the free pool we rotate proxies until one is accepted.

Input: a query line (use `term1 | term2` to compare terms). Geo = a country code (empty = Worldwide).
Timeframe = the lookback window. Resolution = COUNTRY | REGION | CITY | DMA granularity of the breakdown.
"""
import asyncio
import json
import random
import threading
from datetime import datetime

from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

_PIN_LOCK = threading.Lock()
_PINNED: str | None = None        # last proxy from PROXY_LIST_FILE that Trends accepted

EXPLORE = "https://trends.google.com/trends/api/explore"
COMPAREDGEO = "https://trends.google.com/trends/api/widgetdata/comparedgeo"
HOME = "https://trends.google.com/trends/explore"

GTRENDS_COLUMNS = ["query", "term", "location", "geo_code", "value", "geo", "timeframe", "resolution"]

# UI timeframe -> Google Trends `time` param
TIMEFRAME = {
    "Past 4 hours": "now 4-H", "Past day": "now 1-d", "Past 7 days": "now 7-d",
    "Past 30 days": "today 1-m", "Past 90 days": "today 3-m", "Past 12 months": "today 12-m",
    "Past 5 years": "today 5-y", "2004 - present": "all",
}
_RESOLUTIONS = {"COUNTRY", "REGION", "CITY", "DMA"}


def _strip_json(text: str):
    """Trends responses start with `)]}',` — drop everything before the first JSON brace."""
    i = (text or "").find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except Exception:
        return None


def _proxy_line_to_url(line: str) -> str | None:
    """One PROXY_LIST_FILE line -> a proxy URL. Accepts IP:PORT:USER:PASS, IP:PORT, or a full URL."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return None


def _load_proxy_file() -> list[str]:
    """Read PROXY_LIST_FILE into a shuffled list of proxy URLs (last-good pinned to the front).
    Shuffling spreads load across the pool so we don't always hammer (and 429) the same first IPs."""
    path = settings.PROXY_LIST_FILE.strip()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            urls = [u for u in (_proxy_line_to_url(ln) for ln in f) if u]
    except OSError:
        return []
    random.shuffle(urls)
    with _PIN_LOCK:
        pinned = _PINNED
    if pinned and pinned in urls:
        urls.remove(pinned)
        urls.insert(0, pinned)
    return urls


def _proxies() -> list[str]:
    """Proxy candidates to try, in order. Precedence: paid PROXY_URL alone -> rotating
    PROXY_LIST_FILE (datacenter IPs, 429'd ones skipped by the caller) -> warm + free-pool list."""
    px = settings.PROXY_URL.strip()
    if px:
        return [px]
    listed = _load_proxy_file()
    if listed:
        return listed
    try:
        yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 4)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        return warm + yp_us._fetch_candidates()
    except Exception:
        return []


def _geo_breakdown(session, proxies, keywords, geo, time, resolution):
    """Run explore -> comparedgeo on one session; return (geoMapData list) or None on any failure."""
    req = {"comparisonItem": [{"keyword": k, "geo": geo, "time": time} for k in keywords],
           "category": 0, "property": ""}
    r = session.get(EXPLORE, params={"hl": "en-US", "tz": "0", "req": json.dumps(req)},
                    proxies=proxies, timeout=8, verify=False)
    if r.status_code != 200:
        return None
    data = _strip_json(r.text)
    widgets = (data or {}).get("widgets") or []
    geo_w = next((w for w in widgets if str(w.get("id")).startswith("GEO_MAP")), None)
    if not geo_w:
        return None
    base = dict(geo_w.get("request") or {})
    token = geo_w.get("token")
    default_res = base.get("resolution")
    # Resolution must match the geo scope, or Google rejects the data call:
    #   • Worldwide (no geo) -> only COUNTRY breakdown exists (CITY/REGION/DMA need a country).
    #   • A specific country -> REGION/CITY/DMA are valid, but COUNTRY is not.
    # When the chosen resolution is incompatible, drop it and use the widget's default (which Google
    # sets correctly for the geo) so we still return data instead of erroring.
    if not geo:
        chosen = "COUNTRY" if resolution == "COUNTRY" else None
    elif resolution == "COUNTRY":
        chosen = None
    else:
        chosen = resolution
    order = []
    for res in (chosen, default_res):                # try the chosen resolution, then the default
        if res and res not in order:
            order.append(res)
    for res in order:
        wr = dict(base)
        wr["resolution"] = res
        r2 = session.get(COMPAREDGEO, params={"hl": "en-US", "tz": "0", "req": json.dumps(wr),
                                              "token": token}, proxies=proxies, timeout=10, verify=False)
        if r2.status_code == 429:
            return None                # rate-limited → let the caller try the next proxy
        if r2.status_code != 200:
            continue                   # invalid combo for this geo (e.g. 400) → try the default res
        gm = ((_strip_json(r2.text) or {}).get("default") or {}).get("geoMapData") or []
        if gm:
            return gm
    return []                          # reached Trends fine, but no geo data for this combo


def search_sync(query: str, geo: str = "", timeframe: str = "Past 12 months",
                resolution: str = "COUNTRY", job_id: str | None = None) -> list[dict]:
    keywords = [k.strip() for k in (query or "").split("|") if k.strip()]
    if not keywords:
        return []
    time = TIMEFRAME.get(timeframe, "today 12-m")
    geo = (geo or "").upper()
    res = (resolution or "COUNTRY").upper()
    if res not in _RESOLUTIONS:
        res = "COUNTRY"

    attempts = 0
    for px in _proxies():
        if job_id and job_id in STOP_REQUESTS:
            break
        if attempts >= 25:                 # don't grind the whole free pool (Trends 429s aggressively)
            break
        attempts += 1
        proxies = {"http": px, "https": px}
        try:
            session = cffi.Session(impersonate="chrome")
            session.get(HOME, params={"geo": geo or "US"}, proxies=proxies, timeout=6, verify=False)
            gm = _geo_breakdown(session, proxies, keywords, geo, time, res)
            if gm is None:
                continue               # this proxy was blocked/429 — try the next
            global _PINNED
            with _PIN_LOCK:            # remember this accepted proxy for the next call
                _PINNED = px
            rows: list[dict] = []
            for g in gm:
                vals = g.get("value") or []
                for i, kw in enumerate(keywords):
                    rows.append({
                        "query": query, "term": kw,
                        "location": g.get("geoName") or "", "geo_code": g.get("geoCode") or "",
                        "value": vals[i] if i < len(vals) else None,
                        "geo": geo, "timeframe": timeframe, "resolution": res,
                    })
            return rows
        except Exception:
            continue
    raise RuntimeError("Google Trends blocked every proxy (429). Set a paid residential PROXY_URL "
                       "for reliable results — the real IP is never used.")


async def search(query: str, geo: str = "", timeframe: str = "Past 12 months",
                 resolution: str = "COUNTRY", job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, geo, timeframe, resolution, job_id)


async def run_job(job_id: str, queries: list[str], geo: str, timeframe: str, resolution: str) -> None:
    from .db import jobs, gtrends_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:               # Stop button pressed
                break
            rows = await search(q, geo, timeframe, resolution, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gtrends_results.insert_many(rows)
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
