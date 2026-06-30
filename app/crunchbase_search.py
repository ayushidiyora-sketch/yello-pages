"""Crunchbase Search Scraper — find companies on crunchbase.com by name / domain / URL.

A query is a search term (e.g. "uber"), a domain ("uber.com"), or a full URL ("https://www.uber.com").
For each query the site's autocomplete/search feed
`https://www.crunchbase.com/v4/data/autocompletes?query=<q>&collection_ids=organizations` is fetched
through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the
real IP). One row per matching company. `limit` caps matches per query.

Crunchbase is protected by Cloudflare (JS challenge) — the same aggressive anti-bot tier as the
Crunchbase (organization) Scraper. The datacenter free pool (and even a real IP) gets a 403, so live
scraping needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The parser below reads Crunchbase's
autocomplete entity list, so it returns rows as soon as a residential proxy is configured.
"""
import asyncio
import html
import json
import re
from urllib.parse import quote
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

CBS_COLUMNS = [
    "query", "name", "crunchbase_url", "permalink", "short_description", "entity_type",
]

_AUTOCOMPLETE = "https://www.crunchbase.com/v4/data/autocompletes?query={q}&collection_ids=organizations&limit={n}"
_ORG = "https://www.crunchbase.com/organization/"


def _u(v):
    return html.unescape(str(v)) if v else ""


def _term(query: str) -> str:
    """Normalise a query/URL/domain into a Crunchbase search term."""
    q = (query or "").strip()
    if not q:
        return ""
    # a crunchbase org URL -> use its slug
    m = re.search(r"crunchbase\.com/organization/([^/?#]+)", q, re.I)
    if m:
        return m.group(1).replace("-", " ")
    # any other URL/domain -> bare host without protocol / www / path
    q = re.sub(r"^https?://", "", q, flags=re.I)
    q = re.sub(r"^www\.", "", q, flags=re.I)
    q = q.split("/")[0]
    # drop a trailing TLD so "uber.com" searches "uber"
    q = re.sub(r"\.(com|net|org|io|co|ai|app|de|nl|at|pl)$", "", q, flags=re.I)
    return q.strip()


def _row(ent: dict, query: str) -> dict | None:
    if not isinstance(ent, dict):
        return None
    ident = ent.get("identifier") or ent
    if not isinstance(ident, dict):
        ident = {}
    name = ident.get("value") or ent.get("name") or ""
    perma = ident.get("permalink") or ent.get("permalink") or ""
    if not name:
        return None
    row = {c: "" for c in CBS_COLUMNS}
    row.update({
        "query": query,
        "name": _u(name),
        "crunchbase_url": (_ORG + perma) if perma else "",
        "permalink": _u(perma),
        "short_description": _u(re.sub(r"<[^>]+>", " ", str(ent.get("short_description") or ""))).strip()[:500],
        "entity_type": _u(ident.get("entity_def_id") or ent.get("entity_def_id") or "organization"),
    })
    return row


def _parse(data, query: str) -> list[dict]:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return []
    out, seen = [], set()
    # autocomplete: {"entities":[{identifier:{...}, short_description}]}
    # search:       {"entities":[{properties:{identifier:{...}}}]}
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            ents = cur.get("entities")
            if isinstance(ents, list):
                for e in ents:
                    src = e.get("properties") if isinstance(e, dict) and isinstance(e.get("properties"), dict) else e
                    row = _row(src, query)
                    if row:
                        key = row["permalink"] or row["name"]
                        if key not in seen:
                            seen.add(key)
                            out.append(row)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    term = _term(query)
    if not term:
        return []
    n = limit if (limit and limit > 0) else 25
    url = _AUTOCOMPLETE.format(q=quote(term), n=n)
    try:
        r = yp_us.pooled_get(url, {"Accept": "application/json"}, timeout=25)
    except Exception:
        return []
    if r is None or r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        data = r.text
    rows = _parse(data, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in CBS_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: search Crunchbase for each query and store one row per matching company."""
    from .db import jobs, crunchbase_search_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            if not rows:                          # free proxies flaky — retry once
                rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await crunchbase_search_results.insert_many(rows)
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
