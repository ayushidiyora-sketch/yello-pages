"""Product Hunt Profiles Scraper — public user profiles from producthunt.com.

A query is a Product Hunt profile URL (producthunt.com/@username), an @username, or a bare username.
Each profile page is fetched through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the
rotating free pool — NEVER the real IP). Product Hunt embeds the profile in a JSON `User` object
inside the page (`{"__typename":"User", ...}`); we bracket-match and parse it. One row per profile.

Product Hunt is reachable on the free pool (not hard bot-walled), so this works without a paid proxy.
"""
import asyncio
import json
import re
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

BASE = "https://www.producthunt.com"

PH_COLUMNS = [
    "query", "username", "name", "headline", "profile_url", "followers", "following",
    "products_count", "posts_submitted", "collections_count", "reviews_count",
    "is_maker", "twitter", "user_id",
]

_USER_RE = re.compile(r'\{"__typename":"User"')


def _username(query: str) -> str:
    q = (query or "").strip()
    if q.lower().startswith("http"):
        m = re.search(r"/@([A-Za-z0-9_.-]+)", q)
        return m.group(1) if m else q.rstrip("/").split("/")[-1].lstrip("@")
    return q.lstrip("@").strip()


def _profile_url(query: str) -> str:
    return f"{BASE}/@{_username(query)}"


# ---------------- JSON extraction ----------------

def _balanced(s: str, start: int) -> str | None:
    """Return the brace-matched JSON object starting at `start` (string-aware)."""
    depth = 0
    in_str = esc = False
    for j in range(start, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:j + 1]
    return None


def _user_objects(html: str) -> list[dict]:
    out = []
    for m in _USER_RE.finditer(html or ""):
        raw = _balanced(html, m.start())
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("username"):
            out.append(d)
    return out


def _pick_profile(users: list[dict], want: str) -> dict | None:
    """The main profile: the User whose username matches the query, else the richest User node."""
    want = (want or "").lower()
    for d in users:
        if str(d.get("username", "")).lower() == want:
            return d
    # else the one carrying profile-level counts / headline
    scored = sorted(users, key=lambda d: sum(k in d for k in
                    ("headline", "followersCount", "followingsCount", "productsCount", "name")),
                    reverse=True)
    return scored[0] if scored else None


def _num(v):
    return str(v) if isinstance(v, (int, float)) else (v or "")


def _row(d: dict, query: str) -> dict:
    uname = d.get("username") or ""
    row = {c: "" for c in PH_COLUMNS}
    row.update({
        "query": query,
        "username": uname,
        "name": d.get("name") or "",
        "headline": d.get("headline") or "",
        "profile_url": f"{BASE}/@{uname}" if uname else "",
        "followers": _num(d.get("followersCount")),
        "following": _num(d.get("followingsCount")),
        "products_count": _num(d.get("productsCount")),
        "posts_submitted": _num(d.get("submittedPostsCount")),
        "collections_count": _num(d.get("collectionsCount")),
        "reviews_count": _num(d.get("reviewsCount")),
        "is_maker": ("Yes" if d.get("isMaker") else "No") if "isMaker" in d else "",
        "twitter": d.get("twitterUsername") or "",
        "user_id": str(d.get("id") or ""),
    })
    return row


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = _profile_url(query)
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return []
    if r is None or r.status_code != 200 or "__typename" not in (r.text or ""):
        return []
    users = _user_objects(r.text)
    prof = _pick_profile(users, _username(query))
    if not prof:
        return []
    return [_row(prof, query)]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in PH_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Product Hunt profile and store one row per profile."""
    from .db import jobs, producthunt_profiles
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await producthunt_profiles.insert_many(rows)
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
