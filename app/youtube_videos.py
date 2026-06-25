"""YouTube Video Scraper — a channel's videos or shorts (youtubei JSON API, proxy-aware).

Same design as the YouTube Channels scraper: we use YouTube's own public web "innertube" JSON API
(youtubei/v1) instead of scraping HTML. A query is a channel URL (/@handle, /channel/UC..., /c/Name,
/user/Name), a channel's /videos or /shorts URL, or a bare handle/name. We resolve it to a channel id,
open the requested tab (Videos or Shorts), and page through the grid with continuation tokens until the
per-query limit is reached. Each video/short is one row.

PROXY-AWARE: the youtubei API call goes through a paid PROXY_URL when one is set; the public API isn't
bot-walled at low volume, so the free path works otherwise. The real IP is never used for a paid proxy.
"""
import asyncio
import re
from datetime import datetime

from .config import settings
# reuse the channel scraper's innertube POST, channel-id resolver and count parser
from .youtube_channels import _api_post, _resolve_channel_id, _to_int

# Outscraper-style YouTube video schema (one row per video / short)
YOUTUBE_VIDEO_COLUMNS = [
    "query", "video_id", "title", "url", "channel_title", "channel_id",
    "views", "views_parsed", "published", "length", "video_type", "thumbnail",
]


# ---------------- small JSON helpers ----------------

def _txt(node) -> str:
    """Pull text from a {simpleText} / {runs:[{text}]} / {content} node (or a bare string)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node.get("simpleText") or ""
        if "content" in node:
            return node.get("content") or ""
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return ""


def _views_text(*nodes) -> str:
    """First non-empty view-count text from a list of candidate nodes (e.g. '1.2M views')."""
    for n in nodes:
        t = _txt(n)
        if t:
            return t
    return ""


def _last_thumb(node) -> str:
    """Largest thumbnail URL found anywhere under `node` (handles {thumbnails:[...]},
    {sources:[...]} and the nested thumbnailViewModel shapes YouTube uses for shorts)."""
    def walk(o):
        if isinstance(o, dict):
            arr = o.get("thumbnails") or o.get("sources")
            if isinstance(arr, list) and arr and isinstance(arr[-1], dict) and arr[-1].get("url"):
                return arr[-1]["url"]
            for v in o.values():
                u = walk(v)
                if u:
                    return u
        elif isinstance(o, list):
            for v in o:
                u = walk(v)
                if u:
                    return u
        return ""
    return (walk(node) or "").split("?")[0]


# ---------------- item parsing ----------------

def _video_row(vr: dict, query: str, ch_title: str, ch_id: str) -> dict | None:
    """A regular `videoRenderer` -> row."""
    vid = vr.get("videoId")
    if not vid:
        return None
    views = _views_text(vr.get("viewCountText"), vr.get("shortViewCountText"))
    if not ch_title:                                  # search results carry the channel in the byline
        byline = vr.get("ownerText") or vr.get("longBylineText") or {}
        ch_title = _txt(byline)
        for r in (byline.get("runs") or []):
            bid = (((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId"))
            if bid:
                ch_id = ch_id or bid
                break
    return {
        "query": query,
        "video_id": vid,
        "title": _txt(vr.get("title")),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "channel_title": ch_title,
        "channel_id": ch_id,
        "views": views,
        "views_parsed": _to_int(re.sub(r"\s*views?", "", views, flags=re.I)),
        "published": _txt(vr.get("publishedTimeText")),
        "length": _txt(vr.get("lengthText")),
        "video_type": "video",
        "thumbnail": _last_thumb(vr.get("thumbnail")),
    }


def _short_row(node: dict, query: str, ch_title: str, ch_id: str) -> dict | None:
    """A short — `shortsLockupViewModel` (new) or `reelItemRenderer` (old) -> row."""
    # --- new shortsLockupViewModel ---
    if "entityId" in node or "onTap" in node or "overlayMetadata" in node:
        vid = (((node.get("onTap") or {}).get("innertubeCommand") or {})
               .get("reelWatchEndpoint") or {}).get("videoId")
        if not vid:
            m = re.search(r"([\w-]{11})$", node.get("entityId") or "")
            vid = m.group(1) if m else ""
        om = node.get("overlayMetadata") or {}
        title = _txt(om.get("primaryText")) or (node.get("accessibilityText") or "").split(",")[0]
        views = _txt(om.get("secondaryText"))
        thumb = _last_thumb(node)
    else:
        # --- old reelItemRenderer ---
        vid = node.get("videoId")
        title = _txt(node.get("headline"))
        views = _views_text(node.get("viewCountText"))
        thumb = _last_thumb(node.get("thumbnail"))
    if not vid:
        return None
    return {
        "query": query,
        "video_id": vid,
        "title": title,
        "url": f"https://www.youtube.com/shorts/{vid}",
        "channel_title": ch_title,
        "channel_id": ch_id,
        "views": views,
        "views_parsed": _to_int(re.sub(r"\s*views?", "", views, flags=re.I)),
        "published": "",
        "length": "",
        "video_type": "short",
        "thumbnail": thumb,
    }


def _lockup_row(lvm: dict, query: str, ch_title: str, ch_id: str, want_short: bool) -> dict | None:
    """The current `lockupViewModel` (used by the Videos tab, and some Shorts) -> row."""
    vid = lvm.get("contentId")
    if not vid:
        return None
    meta = (lvm.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = _txt(meta.get("title"))
    parts = []
    for r in (((meta.get("metadata") or {}).get("contentMetadataViewModel") or {})
              .get("metadataRows")) or []:
        for p in (r.get("metadataParts") or []):
            t = _txt(p.get("text"))
            if t:
                parts.append(t)
    views = next((p for p in parts if "view" in p.lower()), "")
    published = next((p for p in parts if "ago" in p.lower()), "")
    tvm = ((lvm.get("contentImage") or {}).get("thumbnailViewModel")) or {}
    srcs = (tvm.get("image") or {}).get("sources") or []
    thumb = ((srcs[-1].get("url") if srcs and isinstance(srcs[-1], dict) else "") or "").split("?")[0]
    length = ""
    for ov in (tvm.get("overlays") or []):
        for b in (ov.get("thumbnailBottomOverlayViewModel") or {}).get("badges") or []:
            bt = (b.get("thumbnailBadgeViewModel") or {}).get("text") or ""
            if re.match(r"^\d+:\d", bt):
                length = bt
                break
        if length:
            break
    is_short = want_short or (lvm.get("contentType") == "LOCKUP_CONTENT_TYPE_SHORTS")
    return {
        "query": query,
        "video_id": vid,
        "title": title,
        "url": f"https://www.youtube.com/{'shorts/' if is_short else 'watch?v='}{vid}",
        "channel_title": ch_title,
        "channel_id": ch_id,
        "views": views,
        "views_parsed": _to_int(re.sub(r"\s*views?", "", views, flags=re.I)),
        "published": published,
        "length": length,
        "video_type": "short" if is_short else "video",
        "thumbnail": thumb,
    }


def _collect(items: list, query: str, ch_title: str, ch_id: str, want_short: bool):
    """From a list of grid items, return (rows, continuation_token)."""
    rows, token = [], None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cir = it.get("continuationItemRenderer")
        if cir:
            token = (((cir.get("continuationEndpoint") or {}).get("continuationCommand") or {})
                     .get("token"))
            continue
        content = (it.get("richItemRenderer") or {}).get("content") or it
        row = None
        if want_short:
            node = content.get("shortsLockupViewModel") or content.get("reelItemRenderer")
            if node:
                row = _short_row(node, query, ch_title, ch_id)
            elif content.get("lockupViewModel"):
                row = _lockup_row(content["lockupViewModel"], query, ch_title, ch_id, True)
        elif content.get("videoRenderer"):
            row = _video_row(content["videoRenderer"], query, ch_title, ch_id)
        elif content.get("lockupViewModel"):
            row = _lockup_row(content["lockupViewModel"], query, ch_title, ch_id, False)
        if row:
            rows.append(row)
    return rows, token


# ---------------- tab discovery ----------------

def _channel_meta(data: dict):
    cmr = ((data.get("metadata") or {}).get("channelMetadataRenderer")) or {}
    return cmr.get("title") or "", cmr.get("externalId") or ""


def _tab(data: dict, want_short: bool):
    """Find (params, grid_contents) for the Videos/Shorts tab in a channel browse response.
    params is used to (re)open the tab; grid_contents is non-empty only if that tab is already
    the selected one in this response."""
    want = "shorts" if want_short else "videos"
    tabs = (((data.get("contents") or {}).get("twoColumnBrowseResultsRenderer") or {})
            .get("tabs")) or []
    for t in tabs:
        tr = t.get("tabRenderer") if isinstance(t, dict) else None
        if not tr:
            continue
        title = (tr.get("title") or "").strip().lower()
        ep = ((tr.get("endpoint") or {}).get("browseEndpoint") or {})
        url = (((tr.get("endpoint") or {}).get("commandMetadata") or {})
               .get("webCommandMetadata") or {}).get("url") or ""
        if title == want or url.rstrip("/").endswith("/" + want):
            grid = (((tr.get("content") or {}).get("richGridRenderer") or {}).get("contents")) or []
            return ep.get("params"), grid
    return None, []


# ---------------- playlist ----------------

def _playlist_id(query: str) -> str:
    """A playlist id from a ?list=... URL (also a bare PL.../UU.../OLAK... id). '' if none."""
    q = (query or "").strip()
    m = re.search(r"[?&]list=([\w-]+)", q)
    if m:
        return m.group(1)
    if re.match(r"^(PL|UU|OLAK|FL|RD|LL)[\w-]+$", q):
        return q
    return ""


def _playlist_row(pvr: dict, query: str) -> dict | None:
    vid = pvr.get("videoId")
    if not vid:
        return None
    byline = pvr.get("shortBylineText") or {}
    ch_title = _txt(byline)
    ch_id = ""
    for r in (byline.get("runs") or []):
        bid = (((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId"))
        if bid:
            ch_id = bid
            break
    # views/published sometimes live in videoInfo runs ("1.2M views • 2 years ago")
    info = " ".join(_txt(pvr.get(k)) for k in ("videoInfo",) if pvr.get(k))
    vm = re.search(r"([\d.,]+\s*[KMB]?)\s+views?", info, re.I)
    views = (vm.group(0) if vm else "")
    pm = re.search(r"([\w ]+ ago)", info)
    return {
        "query": query,
        "video_id": vid,
        "title": _txt(pvr.get("title")),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "channel_title": ch_title,
        "channel_id": ch_id,
        "views": views,
        "views_parsed": _to_int(re.sub(r"\s*views?", "", views, flags=re.I)),
        "published": (pm.group(1).strip() if pm else ""),
        "length": _txt(pvr.get("lengthText")),
        "video_type": "video",
        "thumbnail": _last_thumb(pvr.get("thumbnail")),
    }


def _collect_playlist(items: list, query: str):
    """From playlist items, return (rows, continuation_token)."""
    rows, token = [], None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cir = it.get("continuationItemRenderer")
        if cir:
            token = (((cir.get("continuationEndpoint") or {}).get("continuationCommand") or {})
                     .get("token"))
            continue
        pvr = it.get("playlistVideoRenderer")
        if pvr:
            r = _playlist_row(pvr, query)
            if r:
                rows.append(r)
    return rows, token


def _playlist_videos(plid: str, query: str, limit: int | None) -> list[dict]:
    data = _api_post("browse", {"browseId": "VL" + plid})
    if not data:
        raise RuntimeError("YouTube API did not respond (proxy may be rate-limited — try again).")
    ch_title, ch_id = _channel_meta(data)
    # current format: legacy playlistVideoRenderer OR the new lockupViewModel grid
    plv = _find_key(data, "playlistVideoListRenderer")
    items = (plv.get("contents") if isinstance(plv, dict) else None)
    if items:
        rows, token = _collect_playlist(items, query)
        collect = _collect_playlist
    else:                                              # new lockupViewModel items in an itemSectionRenderer
        isr = _find_key(data, "itemSectionRenderer")
        items = (isr.get("contents") if isinstance(isr, dict) else None) or []
        rows, token = _collect(items, query, ch_title, ch_id, False)
        collect = None
    for _ in range(200):                               # safety cap
        if (limit and len(rows) >= limit) or not token:
            break
        d = _api_post("browse", {"continuation": token})
        if not d:
            break
        cont = (((d.get("onResponseReceivedActions") or [{}])[0]
                 .get("appendContinuationItemsAction") or {}).get("continuationItems")) or []
        if collect:
            new, token = collect(cont, query)
        else:
            new, token = _collect(cont, query, ch_title, ch_id, False)
        if not new and not token:
            break
        rows.extend(new)
    return rows[:limit] if limit else rows


def _find_key(data, key):
    """First value stored under `key` anywhere in a nested dict/list, else None."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(data, list):
        for v in data:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


# ---------------- keyword search ----------------

_VIDEO_FILTER = "EgIQAQ%3D%3D"          # YouTube search filter: Videos only (regular videos)


def _is_channel_ref(query: str) -> bool:
    """True for a direct channel/video reference (URL / @handle / UC id); False for a keyword."""
    q = (query or "").strip()
    return (q.lower().startswith("http") or q.startswith("@")
            or bool(re.match(r"^UC[\w-]{20,}$", q)))


def _harvest_search(data: dict, want_short: bool):
    """Collect (renderer nodes, continuation token) from a search response."""
    nodes, token = [], None

    def walk(o):
        nonlocal token
        if isinstance(o, dict):
            if want_short and "shortsLockupViewModel" in o:
                nodes.append(o["shortsLockupViewModel"])
            elif (not want_short) and "videoRenderer" in o:
                nodes.append(o["videoRenderer"])
            cir = o.get("continuationItemRenderer")
            if cir:
                token = (((cir.get("continuationEndpoint") or {}).get("continuationCommand") or {})
                         .get("token")) or token
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return nodes, token


def _search_videos(keyword: str, limit: int | None, want_short: bool) -> list[dict]:
    """Keyword -> matching videos (regular) or shorts via YouTube's search API."""
    body = {"query": keyword}
    if not want_short:                               # regular: restrict to the Videos filter
        body["params"] = _VIDEO_FILTER
    d = _api_post("search", body)
    if not d:
        raise RuntimeError("YouTube API did not respond (proxy may be rate-limited — try again).")
    rows, seen = [], set()

    def add(nodes):
        n = 0
        for node in nodes:
            row = _short_row(node, keyword, "", "") if want_short else _video_row(node, keyword, "", "")
            if row and row["video_id"] not in seen:
                seen.add(row["video_id"])
                rows.append(row)
                n += 1
        return n

    nodes, token = _harvest_search(d, want_short)
    add(nodes)
    for _ in range(200):                             # safety cap
        if (limit and len(rows) >= limit) or not token:
            break
        d = _api_post("search", {"continuation": token})
        if not d:
            break
        nodes, token = _harvest_search(d, want_short)
        if not add(nodes) and not token:
            break
    return rows[:limit] if limit else rows


# ---------------- channel ----------------

def _channel_videos(query: str, limit: int | None, want_short: bool) -> list[dict]:
    cid = _resolve_channel_id(query)
    if not cid:
        raise RuntimeError(f"Could not resolve a YouTube channel from '{query}'. Use a channel URL, "
                           "@handle, channel id, playlist URL, or a search keyword.")
    root = _api_post("browse", {"browseId": cid})
    if not root:
        raise RuntimeError("YouTube API did not respond (proxy may be rate-limited — try again).")
    ch_title, ch_id = _channel_meta(root)
    ch_id = ch_id or cid
    params, grid = _tab(root, want_short)
    if not grid and params:                       # tab not selected in root -> open it explicitly
        d = _api_post("browse", {"browseId": cid, "params": params})
        if d:
            _, grid = _tab(d, want_short)
            if not grid:                           # tab is now selected — grid sits at the top level
                grid = _first_grid(d)

    rows, token = _collect(grid, query, ch_title, ch_id, want_short)

    # page through continuations
    for _ in range(200):                           # safety cap
        if (limit and len(rows) >= limit) or not token:
            break
        d = _api_post("browse", {"continuation": token})
        if not d:
            break
        items = (((d.get("onResponseReceivedActions") or [{}])[0]
                  .get("appendContinuationItemsAction") or {}).get("continuationItems")) or []
        new, token = _collect(items, query, ch_title, ch_id, want_short)
        if not new and not token:
            break
        rows.extend(new)

    return rows[:limit] if limit else rows


# ---------------- scrape (dispatch) ----------------

def search_sync(query: str, limit: int | None = None, video_type: str = "video") -> list[dict]:
    want_short = (video_type or "").lower().startswith("short")
    plid = _playlist_id(query)
    if plid:                                           # a ?list=… playlist URL / bare playlist id
        return _playlist_videos(plid, query, limit)
    if _is_channel_ref(query):                         # channel URL / @handle / UC id -> that channel
        return _channel_videos(query, limit, want_short)
    return _search_videos(query, limit, want_short)    # plain text -> keyword search


def _first_grid(data: dict) -> list:
    """Find the first richGridRenderer.contents anywhere in a browse response (selected tab)."""
    found = []

    def walk(o):
        if found:
            return
        if isinstance(o, dict):
            g = o.get("richGridRenderer")
            if isinstance(g, dict) and g.get("contents"):
                found.extend(g["contents"])
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return found


async def search(query: str, limit: int | None = None, video_type: str = "video") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, video_type)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in YOUTUBE_VIDEO_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None,
                  video_type: str = "video") -> None:
    """Background task: scrape each channel's videos/shorts, one row per video."""
    from .db import jobs, youtube_videos_results
    from .scraper import STOP_REQUESTS
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            try:
                rows = await search(q, limit, video_type)
            except Exception as e:
                rows = []
                await jobs.update_one({"job_id": job_id}, {"$set": {"last_error": str(e)}})
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await youtube_videos_results.insert_many(rows)
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
