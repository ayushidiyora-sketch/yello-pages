"""Zillow Transactions Scraper — an agent's transaction history from a Zillow profile.

A query is a Zillow agent profile URL (https://www.zillow.com/profile/<agent>/). The page is fetched
through the proxy pool (paid PROXY_URL / PROXY_LIST if set, else the rotating free pool — NEVER the
real IP). One row per transaction (past sale / listing) on the agent's profile. `limit` caps
transactions per agent.

Zillow is protected by PerimeterX behind CloudFront — the same aggressive anti-bot tier as StreetEasy
/ Crunchbase. The datacenter free pool (and even a real IP) gets a 403 / human-verification, so live
scraping needs RESIDENTIAL proxies in PROXY_URL / PROXY_LIST. The parser below reads the agent's
transaction list out of the page's embedded Next.js data, so rows come back as soon as a residential
proxy is configured.
"""
import asyncio
import html
import json
import re
from datetime import datetime

from . import yp_us
from .scraper import STOP_REQUESTS

ZT_COLUMNS = [
    "query", "agent", "address", "price", "transaction_type", "date", "status",
    "beds", "baths", "sqft", "property_type", "represented", "listing_url",
]

_PRICE_KEYS = {"soldPrice", "salePrice", "price", "amount", "lastSoldPrice", "listingPrice"}
_DATE_KEYS = {"soldDate", "saleDate", "date", "soldDateString", "transactionDate", "closeDate"}
_ADDR_KEYS = {"address", "addressString", "streetAddress", "fullAddress", "abbreviatedAddress"}


def _u(v):
    return html.unescape(str(v)) if v else ""


def _balanced_json(text: str, start: int) -> str:
    i = text.find("{", start)
    if i < 0:
        return ""
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def _next_data(html_text: str):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    for marker in ("__NEXT_DATA__", "__APOLLO_STATE__", "__INITIAL_STATE__"):
        idx = html_text.find(marker)
        if idx >= 0:
            blob = _balanced_json(html_text, idx)
            if blob:
                try:
                    return json.loads(blob)
                except Exception:
                    continue
    return None


def _raw(d: dict, keys: set):
    """Return the raw value for the first matching key (dict/scalar unchanged)."""
    for k in d:
        if k in keys:
            return d[k]
    return ""


def _val(d: dict, keys: set):
    v = _raw(d, keys)
    if isinstance(v, dict):
        return v.get("value") or v.get("text") or v.get("formatted") or ""
    return v


def _agent_name(data) -> str:
    """Best-effort agent display name from the profile data."""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k in ("fullName", "displayName", "name", "agentName", "screenName"):
                v = cur.get(k)
                if isinstance(v, str) and v.strip() and "Agent" not in k[:0]:
                    return v.strip()
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


def _addr_text(v):
    if isinstance(v, dict):
        parts = [v.get(k) for k in ("streetAddress", "line1", "city", "state", "zipcode", "postalCode")]
        return ", ".join(str(p) for p in parts if p)
    return _u(v)


def _looks_like_txn(d: dict) -> bool:
    keys = set(d.keys())
    return bool(keys & _PRICE_KEYS) and bool(keys & (_DATE_KEYS | _ADDR_KEYS))


def _row(d: dict, query: str, agent: str) -> dict | None:
    if not _looks_like_txn(d):
        return None
    row = {c: "" for c in ZT_COLUMNS}
    url = d.get("listingUrl") or d.get("hdpUrl") or d.get("url") or ""
    if isinstance(url, str) and url.startswith("/"):
        url = "https://www.zillow.com" + url
    row.update({
        "query": query,
        "agent": agent,
        "address": _addr_text(_raw(d, _ADDR_KEYS)),
        "price": str(_val(d, _PRICE_KEYS) or ""),
        "transaction_type": _u(d.get("transactionType") or d.get("type") or d.get("priceType") or ""),
        "date": _u(_val(d, _DATE_KEYS)),
        "status": _u(d.get("status") or d.get("homeStatus") or ""),
        "beds": str(d.get("bedrooms") or d.get("beds") or ""),
        "baths": str(d.get("bathrooms") or d.get("baths") or ""),
        "sqft": str(d.get("livingArea") or d.get("sqft") or d.get("livingAreaValue") or ""),
        "property_type": _u(d.get("propertyType") or d.get("homeType") or ""),
        "represented": _u(d.get("representedSide") or d.get("represented") or d.get("buyerSellerType") or ""),
        "listing_url": url if isinstance(url, str) else "",
    })
    return row


def _parse(html_text: str, query: str) -> list[dict]:
    data = _next_data(html_text)
    if data is None:
        return []
    agent = _agent_name(data)
    out, seen = [], set()
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            row = _row(cur, query, agent)
            if row:
                key = (row["address"], row["price"], row["date"])
                if key not in seen:
                    seen.add(key)
                    out.append(row)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    url = (query or "").strip()
    if not url.lower().startswith("http"):
        return []
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return []
    if r is None or r.status_code != 200:
        return []
    rows = _parse(r.text, query)
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in ZT_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: scrape each Zillow agent profile and store one row per transaction."""
    from .db import jobs, zillow_transactions_results
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
                await zillow_transactions_results.insert_many(rows)
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
