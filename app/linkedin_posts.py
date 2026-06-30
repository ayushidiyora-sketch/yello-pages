"""LinkedIn Posts Scraper — recent posts from a company profile (auth + proxy-only).

Company posts sit behind LinkedIn's login wall — there is NO free/public source for them (the
logged-out /posts/ page is an authwall, and individual public post URLs aren't discoverable without
auth). So we read them through LinkedIn's own authenticated voyager API, using the account cookie set
in `settings.LINKEDIN_COOKIE` (your `li_at` value). Every request goes through the proxy pool (paid
PROXY_URL if set, else a free-pool proxy); the REAL IP is never used.

Flow: open a session with the li_at cookie -> hit a LinkedIn page to pick up the JSESSIONID (used as
the csrf-token) -> call /voyager/api/feed/updatesV2?companyUniversalName=<slug> and read the posts
(commentary text, author, reactions/comments/reposts, date from the activity urn).

Without LINKEDIN_COOKIE set the job returns 0 with a clear "login required" note. NOTE: the voyager
endpoint/shape is LinkedIn-internal and may need a live tuning pass once a real cookie + residential
proxy are configured.
"""
import asyncio
import re
from datetime import datetime, timezone

from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

VOYAGER = "https://www.linkedin.com/voyager/api/feed/updatesV2"

LINKEDIN_POSTS_COLUMNS = ["query", "author", "text", "posted", "reactions", "comments", "reposts",
                          "post_url", "post_urn"]

_ACTIVITY = re.compile(r"urn:li:activity:(\d+)")


def _slug(q: str) -> str:
    """Company universalName (slug) from a URL, a bare slug, or a numeric id."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        m = re.search(r"/company/([^/?#]+)", q)
        return m.group(1) if m else q
    return q.strip("/")


def _proxy() -> str | None:
    px = settings.PROXY_URL.strip()
    if px:
        return px
    try:
        yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 3)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        return warm[0] if warm else (yp_us._fetch_candidates() or [None])[0]
    except Exception:
        return None


def _post_date(urn: str) -> str:
    """LinkedIn activity ids are snowflakes — the high 41 bits are the epoch-ms timestamp."""
    m = _ACTIVITY.search(urn or "")
    if not m:
        return ""
    try:
        ms = int(m.group(1)) >> 22
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        return ""


def _counts_by_urn(included: list) -> dict:
    """Map an update/social urn -> {reactions, comments, reposts} from the SocialActivityCounts items."""
    out = {}
    for it in included:
        if not isinstance(it, dict):
            continue
        if "numComments" in it or "numLikes" in it or "numShares" in it:
            urn = it.get("urn") or it.get("entityUrn") or ""
            out[urn] = {
                "reactions": it.get("numLikes") or (it.get("reactionTypeCounts") and
                             sum(c.get("count", 0) for c in it["reactionTypeCounts"])) or 0,
                "comments": it.get("numComments", 0),
                "reposts": it.get("numShares", 0),
            }
    return out


def _commentary_text(commentary) -> str:
    if isinstance(commentary, dict):
        txt = commentary.get("text")
        if isinstance(txt, dict):
            return (txt.get("text") or "").strip()
        if isinstance(txt, str):
            return txt.strip()
    return ""


def _parse_posts(query: str, data: dict, limit: int | None) -> list[dict]:
    included = data.get("included") if isinstance(data, dict) else None
    if not isinstance(included, list):
        return []
    counts = _counts_by_urn(included)
    rows: list[dict] = []
    seen = set()
    for it in included:
        if not isinstance(it, dict) or "commentary" not in it:
            continue
        text = _commentary_text(it.get("commentary"))
        if not text:
            continue
        meta = it.get("updateMetadata") or {}
        urn = meta.get("urn") or it.get("*socialDetail") or it.get("entityUrn") or ""
        if urn in seen:
            continue
        seen.add(urn)
        actor = it.get("actor") or {}
        name = actor.get("name") or {}
        author = name.get("text") if isinstance(name, dict) else (name if isinstance(name, str) else "")
        c = counts.get(urn) or counts.get(it.get("*socialDetail") or "") or {}
        rows.append({
            "query": query,
            "author": (author or "").strip(),
            "text": text,
            "posted": _post_date(urn),
            "reactions": c.get("reactions", ""),
            "comments": c.get("comments", ""),
            "reposts": c.get("reposts", ""),
            "post_url": f"https://www.linkedin.com/feed/update/{urn}" if urn.startswith("urn:") else "",
            "post_urn": urn,
        })
        if limit and len(rows) >= limit:
            break
    return rows


def search_sync(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    cookie = settings.LINKEDIN_COOKIE.strip()
    if not cookie:
        raise RuntimeError("LinkedIn posts are behind the login wall — set LINKEDIN_COOKIE (your "
                           "li_at cookie) in .env to enable this scraper. No public source exists.")
    px = _proxy()
    if not px:
        raise RuntimeError("No proxy available to reach LinkedIn (set a PROXY_URL). Real IP unused.")
    proxies = {"http": px, "https": px}
    slug = _slug(query)
    li_at = cookie.split("li_at=")[-1].split(";")[0].strip() if "li_at=" in cookie else cookie

    session = cffi.Session(impersonate="chrome")
    session.cookies.set("li_at", li_at, domain=".linkedin.com")
    # pick up JSESSIONID (LinkedIn's csrf token) by touching a page first
    try:
        session.get("https://www.linkedin.com/feed/", proxies=proxies, timeout=15, verify=False)
    except Exception:
        pass
    jsid = (session.cookies.get("JSESSIONID") or "ajax:0000").strip('"')
    headers = {
        "csrf-token": jsid,
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "referer": f"https://www.linkedin.com/company/{slug}/posts/",
    }

    rows: list[dict] = []
    seen = set()
    start, page = 0, 20
    for _ in range(50):
        if job_id and job_id in STOP_REQUESTS:
            break
        params = {"companyUniversalName": slug, "q": "companyFeedByUniversalName",
                  "count": str(page), "start": str(start)}
        try:
            r = session.get(VOYAGER, params=params, headers=headers, proxies=proxies,
                            timeout=20, verify=False)
        except Exception:
            break
        if r.status_code != 200:
            if start == 0:
                raise RuntimeError(f"LinkedIn voyager returned {r.status_code} — the cookie may be "
                                   "expired or the IP/endpoint blocked. Use a residential PROXY_URL "
                                   "and a fresh li_at cookie.")
            break
        try:
            batch = _parse_posts(query, r.json(), None)
        except Exception:
            break
        new = [p for p in batch if p["post_urn"] not in seen]
        for p in new:
            seen.add(p["post_urn"])
            rows.append(p)
        if not new:
            break
        if limit and len(rows) >= limit:
            break
        start += page
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None, job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, job_id)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from .db import jobs, linkedin_posts_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await linkedin_posts_results.insert_many(rows)
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
