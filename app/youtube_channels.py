"""YouTube Channels Scraper — youtube.com channel details.

Same design as the other live scrapers: every request goes through a proxy IP (a paid PROXY_URL
if set, otherwise the rotating free pool — NEVER the real IP). YouTube channel pages embed all the
data in a `ytInitialData` JSON blob, which we extract (no headless browser needed). YouTube is far
less aggressive than DataDome/PerimeterX sites, so the free pool usually works (with retries).

A query is a channel URL (/@handle, /channel/UC..., /c/Name, /user/Name) or a bare handle/name
(e.g. "outscraper" -> https://www.youtube.com/@outscraper). Each query yields one channel row.
"""
import asyncio
import json
import re
import threading
from datetime import datetime

from curl_cffi import requests as cffi

from .config import settings

_YT_TIMEOUT = 15
_GOOD_PROXY = None          # last proxy that passed youtube.com — reused before re-rotating
_PIN_LOCK = threading.Lock()
_SEED = {"search_terms": "x", "geo_location_terms": "y", "page": "1"}
# CONSENT cookie + hl=en avoid YouTube's EU consent interstitial
_HDRS = {"Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+cb; PREF=hl=en&gl=US"}

# ---------------- youtubei API (YouTube's own JSON API — free, no user key) ----------------
# This is YouTube's public web "innertube" key, embedded in every youtube.com page (same for
# everyone, NOT a personal API key). We POST to youtubei/v1 to get channel data as JSON instead
# of scraping the HTML page.
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_INNERTUBE_CTX = {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                             "hl": "en", "gl": "US"}}


def _api_post(endpoint: str, extra: dict):
    """POST to youtubei/v1/<endpoint> and return the JSON dict. Paid PROXY_URL if set, else direct
    (it's a public API — no bot block / bans at low volume). None on failure."""
    url = f"https://www.youtube.com/youtubei/v1/{endpoint}?key={INNERTUBE_KEY}&prettyPrint=false"
    payload = {"context": _INNERTUBE_CTX, **extra}
    proxy = settings.PROXY_URL.strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = cffi.post(url, data=json.dumps(payload),
                      headers={"Content-Type": "application/json", **_HDRS},
                      impersonate="chrome", proxies=proxies,
                      timeout=settings.REQUEST_TIMEOUT, verify=False)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _resolve_channel_id(query: str) -> str:
    """Resolve a query to a UC channel id. Direct for channel ids / /channel/ URLs; otherwise use
    the youtubei resolve_url API on the @handle / name."""
    q = (query or "").strip()
    m = re.search(r"/channel/(UC[\w-]+)", q) or re.match(r"^(UC[\w-]{20,})$", q)
    if m:
        return m.group(1)
    d = _api_post("navigation/resolve_url", {"url": _channel_url(q).split("?")[0]})
    if not d:
        return ""
    mm = re.search(r'"browseId"\s*:\s*"(UC[\w-]+)"', json.dumps(d))
    return mm.group(1) if mm else ""


def _channel_id_to_row(cid: str, label: str) -> dict | None:
    """Browse a channel id via the youtubei API and parse it into a row (reusing parse_channel)."""
    data = _api_post("browse", {"browseId": cid})
    if not data:
        return None
    # feed the API JSON to the existing parser via the same `ytInitialData = {...};` shape
    blob = "ytInitialData = " + json.dumps(data) + ";"
    return parse_channel(blob, label)


def _api_channel_rows(query: str) -> list[dict]:
    """Single channel: resolve a URL/@handle/id to a channel and return its details. []
    if the API path can't resolve (caller then falls back to the HTML method)."""
    cid = _resolve_channel_id(query)
    if not cid:
        return []
    row = _channel_id_to_row(cid, query)
    return [row] if row else []


def _is_channel_ref(query: str) -> bool:
    """True for a direct channel reference (URL / @handle / UC id); False for a search keyword."""
    q = (query or "").strip()
    return (q.lower().startswith("http") or q.startswith("@")
            or bool(re.match(r"^UC[\w-]{20,}$", q)))


def _search_channel_ids(keyword: str, limit: int = 10) -> list[str]:
    """YouTube search (channels filter) -> top channel ids matching a keyword (in order)."""
    d = _api_post("search", {"query": keyword, "params": "EgIQAg%3D%3D"})  # EgIQAg== = Channels
    if not d:
        return []
    ids = []

    def walk(o):
        if isinstance(o, dict):
            cr = o.get("channelRenderer")
            if isinstance(cr, dict) and cr.get("channelId"):
                ids.append(cr["channelId"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    seen = []
    for i in ids:
        if i not in seen:
            seen.append(i)
    return seen[:limit]


def _api_search_rows(keyword: str, limit: int = 10) -> list[dict]:
    """Keyword -> top matching channels (full details for each)."""
    rows = []
    for cid in _search_channel_ids(keyword, limit):
        r = _channel_id_to_row(cid, keyword)
        if r:
            rows.append(r)
    return rows

# Full Outscraper YouTube-Channels schema (one row per channel)
YOUTUBE_CHANNEL_COLUMNS = [
    "query", "title", "description", "channel_id", "channel_url", "is_family_safe",
    "vanity_channel_url", "keywords", "rss_url", "thumbnail", "primary_links", "secondary_links",
    "videos_count", "videos_count_parsed", "subscribers_count", "subscribers_count_parsed",
    "views_count", "views_count_parsed", "joined", "country",
]


def _blank_row():
    return {c: "" for c in YOUTUBE_CHANNEL_COLUMNS}


# ---------------- proxy fetch (never the real IP) ----------------

def _ok(r) -> bool:
    if r is None or r.status_code != 200:
        return False
    t = r.text or ""
    low = t.lower()
    if "consent.youtube" in low or "before you continue to youtube" in low:
        return False
    return "ytInitialData" in t


def _try(url: str, px: str):
    try:
        r = cffi.get(url, impersonate="chrome", headers=_HDRS, proxies={"http": px, "https": px},
                     timeout=_YT_TIMEOUT, verify=False, allow_redirects=True)
        return r if _ok(r) else None
    except Exception:
        return None


def _proxied_get(url: str):
    global _GOOD_PROXY
    pinned = _GOOD_PROXY
    if pinned:
        r = _try(url, pinned)
        if r is not None:
            return r
    from . import yp_us
    yp_us.ensure_pool(_SEED, 8)
    seen, candidates = {pinned}, []
    for px in list(yp_us._GOOD) + yp_us._fetch_candidates():
        if px not in seen:
            seen.add(px)
            candidates.append(px)
    for px in candidates[:15]:
        r = _try(url, px)
        if r is not None:
            with yp_us._LOCK:
                if px in yp_us._GOOD:
                    yp_us._GOOD.remove(px)
                yp_us._GOOD.insert(0, px)
            with _PIN_LOCK:
                _GOOD_PROXY = px
            return r
    raise RuntimeError("no free proxy passed youtube.com")


def _get_text(url: str) -> str | None:
    proxy = settings.PROXY_URL.strip()
    if proxy:
        try:
            r = cffi.get(url, impersonate="chrome", headers=_HDRS,
                         proxies={"http": proxy, "https": proxy},
                         timeout=settings.REQUEST_TIMEOUT, verify=False, allow_redirects=True)
        except Exception:
            return None
        return r.text if _ok(r) else None
    try:
        return _proxied_get(url).text
    except Exception:
        return None


# ---------------- URL + parsing ----------------

def _channel_url(query: str) -> str:
    q = (query or "").strip()
    if q.lower().startswith("http"):
        url = q
    elif q.startswith("@"):
        url = f"https://www.youtube.com/{q}"
    elif re.match(r"^UC[\w-]{20,}$", q):           # raw channel id
        url = f"https://www.youtube.com/channel/{q}"
    else:
        url = f"https://www.youtube.com/@{q}"
    sep = "&" if "?" in url else "?"
    return url + sep + "hl=en"


def _extract_json(html: str, marker: str):
    """String-aware brace matcher: pull the first {...} object after `marker`."""
    i = html.find(marker)
    if i < 0:
        return None
    i = html.find("{", i)
    if i < 0:
        return None
    depth, instr, esc = 0, False, False
    for j in range(i, len(html)):
        c = html[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[i:j + 1])
                    except Exception:
                        return None
    return None


def _meta(html: str, prop: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name|itemprop)="' + re.escape(prop) + r'"[^>]+content="([^"]*)"', html)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name|itemprop)="' + re.escape(prop) + r'"', html)
    return (m.group(1) if m else "").strip()


def _count(html: str, unit: str) -> str:
    m = re.search(r'"([\d.,]+\s*[KMB]?)\s+' + unit + r's?"', html)
    return m.group(1).strip() if m else ""


def _to_int(s: str):
    """"29.9M" -> 29900000, "8.7K" -> 8700, "1,234" -> 1234. '' if unparseable."""
    s = (s or "").replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KMB]?)", s)
    if not m:
        return ""
    try:
        return int(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])
    except Exception:
        return ""


def _links(html: str, key: str) -> str:
    """Best-effort: header link titles+urls from a channelHeaderLinks/aboutChannel block."""
    blk = re.search('"' + key + r'":\[(.*?)\]', html)
    if not blk:
        return ""
    out = []
    for m in re.finditer(r'"title":"([^"]*)".*?"url":"([^"]+)"', blk.group(1)):
        url = m.group(2).replace("\\/", "/")
        out.append(f"{m.group(1)}: {url}" if m.group(1) else url)
    return " | ".join(out)


def parse_channel(html: str, query: str) -> dict | None:
    data = _extract_json(html, "ytInitialData = ") or {}
    cmr = (((data.get("metadata") or {}).get("channelMetadataRenderer")) or {})
    row = _blank_row()
    row["query"] = query

    row["title"] = cmr.get("title") or _meta(html, "og:title")
    row["description"] = cmr.get("description") or _meta(html, "og:description")
    row["channel_id"] = cmr.get("externalId") or ""
    if not row["channel_id"]:
        cm = re.search(r"/channel/(UC[\w-]+)", html)
        row["channel_id"] = cm.group(1) if cm else ""
    vanity = cmr.get("vanityChannelUrl") or ""
    row["vanity_channel_url"] = vanity
    row["channel_url"] = cmr.get("channelUrl") or vanity or _meta(html, "og:url")
    fs = cmr.get("isFamilySafe")
    row["is_family_safe"] = "Yes" if fs else ("No" if fs is False else "")
    kw = cmr.get("keywords")
    row["keywords"] = kw if isinstance(kw, str) else (", ".join(kw) if isinstance(kw, list) else "")
    row["rss_url"] = cmr.get("rssUrl") or (
        f"https://www.youtube.com/feeds/videos.xml?channel_id={row['channel_id']}"
        if row["channel_id"] else "")
    av = (cmr.get("avatar") or {}).get("thumbnails") or []
    row["thumbnail"] = (av[-1].get("url") if av else "") or _meta(html, "og:image")

    # subscribers + videos: the channel's OWN header shows them together ("X subscribers · Y videos").
    # Prefer that adjacent pair — robust for both the API JSON and the HTML page. A related/featured
    # channel lists only a subscriber count (no adjacent video count), so it won't be picked.
    pair = re.search(r"([\d.,]+\s*[KMB]?)\s+subscribers.{0,300}?([\d.,]+\s*[KMB]?)\s+videos", html, re.S)
    if pair:
        subs, vc = pair.group(1).strip(), pair.group(2).strip()
        row["videos_count"] = vc
        row["videos_count_parsed"] = _to_int(vc)
    else:
        subs = _count(html, "subscriber")
        subs_i = html.find(subs) if subs else -1
        vids = [(m.start(), m.group(1).strip())
                for m in re.finditer(r'"([\d.,]+\s*[KMB]?)\s+videos?"', html)]
        if vids:
            vc = (min(vids, key=lambda mv: abs(mv[0] - subs_i))[1] if subs_i >= 0 else vids[0][1])
            row["videos_count"] = vc
            row["videos_count_parsed"] = _to_int(vc)
    row["subscribers_count"] = subs
    row["subscribers_count_parsed"] = _to_int(subs)

    # views + joined (best-effort — present in the About block when YouTube preloads it)
    vm = re.search(r'"([\d,]+)\s+views"', html)
    if vm:
        row["views_count"] = vm.group(1) + " views"
        row["views_count_parsed"] = _to_int(vm.group(1))
    jm = re.search(r'"Joined ([^"]+)"', html)
    if jm:
        row["joined"] = jm.group(1).strip()

    cc = cmr.get("availableCountryCodes") or []
    if isinstance(cc, list) and len(cc) == 1:
        row["country"] = cc[0]
    else:
        cm2 = re.search(r'"country":"([^"]+)"', html)
        row["country"] = cm2.group(1) if cm2 else ""

    row["primary_links"] = _links(html, "primaryLinks")
    row["secondary_links"] = _links(html, "secondaryLinks")

    return row if (row["title"] or row["channel_id"]) else None


# ---------------- scrape + run loop ----------------

def search_sync(query: str) -> list[dict]:
    # A plain keyword (not a URL/@handle/UC-id) -> SEARCH YouTube and return the top matching
    # channels; a direct reference -> that one channel.
    if not _is_channel_ref(query):
        rows = _api_search_rows(query, 10)
        if rows:
            return rows
    # 1) PRIMARY: YouTube's own youtubei JSON API (free, no user key) — no HTML scraping.
    rows = _api_channel_rows(query)
    if rows:
        return rows
    # 2) FALLBACK: the HTML page (kept so nothing breaks if the API path can't resolve).
    html = _get_text(_channel_url(query))
    if html is None:
        return []
    # If the input was a VIDEO/watch URL (not a channel), it has no channelMetadataRenderer —
    # resolve to the uploader's channel and re-fetch the real channel page for accurate data.
    if "channelMetadataRenderer" not in html:
        m = re.search(r'"channelId":"(UC[\w-]+)"', html)
        if m:
            ch = _get_text(f"https://www.youtube.com/channel/{m.group(1)}?hl=en")
            if ch:
                html = ch
    row = parse_channel(html, query)
    return [row] if row else []


async def search(query: str) -> list[dict]:
    return await asyncio.to_thread(search_sync, query)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in YOUTUBE_CHANNEL_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each YouTube channel and store one row per channel."""
    from .db import jobs, youtube_channels
    total = 0
    try:
        mode = "youtube-free-pool" if not settings.PROXY_URL.strip() else "youtube-paid-proxy"
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": mode}})

        for q in queries:
            rows = await search(q)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await youtube_channels.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
