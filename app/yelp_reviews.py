"""Y.E.L.P Reviews Scraper — customer reviews from a yelp.com business.

Reuses the Yelp proxy fetch (app/yelp.py — PROXY-ONLY, residential PROXY_URL needed; Yelp hard-blocks
free/datacenter IPs; the real IP is never used). A query is a yelp.com /biz/ URL, a bare business
slug (e.g. "eggcellent-waffles-san-francisco"), or a business id alias. Reviews come from the biz
page's embedded JSON-LD (`Review` items) with the rendered review cards as a fallback. Parser is
best-effort — finalized against a real (residential-proxy) biz page.
"""
import asyncio
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .yelp import _get_html, _page_url, BASE

YELP_REVIEW_COLUMNS = ["query", "business", "reviewer", "location", "rating", "date", "review"]


def _biz_url(query: str) -> str:
    """A query is a /biz/ URL, a bare slug, or a business id -> canonical /biz/<slug> URL."""
    q = (query or "").strip()
    if q.lower().startswith("http"):
        return q.split("?")[0].rstrip("/")
    return f"{BASE}/biz/{q.strip('/')}"


def _biz_name(html: str) -> str:
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html or "")
    if m:
        return m.group(1).split(" - ")[0].strip()
    h = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.S | re.I)
    return re.sub(r"<[^>]+>", "", h.group(1)).strip() if h else ""


def _parse_reviews(html: str, query: str, business: str):
    """Reviews from JSON-LD (`Review` objects) first, then visible review cards."""
    out, seen = [], set()
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            revs = obj.get("review") or obj.get("reviews") or []
            if obj.get("@type") == "Review":
                revs = [obj]
            for rv in (revs if isinstance(revs, list) else [revs]):
                if not isinstance(rv, dict):
                    continue
                author = rv.get("author")
                author = author.get("name") if isinstance(author, dict) else author
                rr = rv.get("reviewRating") or {}
                body = rv.get("description") or rv.get("reviewBody") or ""
                key = (author or "", (body or "")[:40])
                if not body or key in seen:
                    continue
                seen.add(key)
                out.append({
                    "query": query, "business": business, "reviewer": author or "",
                    "location": "", "rating": str(rr.get("ratingValue") or "") if isinstance(rr, dict) else "",
                    "date": rv.get("datePublished") or "", "review": body,
                })
    if not out:                                        # fallback: rendered review cards
        soup = BeautifulSoup(html or "", "lxml")
        for c in soup.select('[data-testid="serp-review"], ul li div[class*="review"], div.review'):
            body = _t(c.select_one('[class*="comment"] span, p, span[lang]'))
            if not body or len(body) < 8:
                continue
            rt = c.get_text(" ", strip=True)
            rm = re.search(r"(\d(?:\.\d)?)\s*star", rt, re.I)
            key = body[:48]
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "query": query, "business": business,
                "reviewer": _t(c.select_one('[class*="user-passport"] a, a[href*="/user_details"]')),
                "location": _t(c.select_one('[class*="responsiveU"] span, [class*="userLocation"]')),
                "rating": rm.group(1) if rm else "", "date": _t(c.select_one("time, span[class*='date']")),
                "review": body,
            })
    return out


def _t(node):
    return node.get_text(" ", strip=True) if node else ""


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    base = _biz_url(query)
    rows, seen, business = [], set(), ""
    for page in range(0, 30):
        html = _get_html(_page_url(base, page * 10))
        if html is None:
            break
        if not business:
            business = _biz_name(html)
        page_rows = _parse_reviews(html, query, business)
        new = [r for r in page_rows if (r["reviewer"], r["review"][:40]) not in seen]
        if not new:
            break
        for r in new:
            seen.add((r["reviewer"], r["review"][:40]))
            rows.append(r)
            if limit and len(rows) >= limit:
                return rows
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None) -> None:
    from .db import jobs, yelp_reviews
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await yelp_reviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        done = {"status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}
        if not total:
            done["note"] = ("Yelp returned 0 reviews — it hard-blocks free/datacenter IPs (the real "
                            "IP is never used). Set a US residential PROXY_URL in .env to scrape it.")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
