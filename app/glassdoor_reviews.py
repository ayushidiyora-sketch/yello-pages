"""Glassdoor Reviews Scraper — glassdoor.com company reviews.

Same design as the other live scrapers: every request goes through a proxy IP (paid PROXY_URL
if set, else the rotating free pool — NEVER the real IP). Glassdoor is bot-protected (Cloudflare),
so on the free pool it is often blocked and returns 0 until a paid PROXY_URL is set (same as the
BBB / Glassdoor-Jobs scrapers). The proxy fetch is shared with app/glassdoor_jobs.py.

A query is a Glassdoor company-reviews URL, e.g.
    https://www.glassdoor.com/Reviews/Amazon-Reviews-E6036.htm
Each query yields up to `limit` review rows. `sort` maps to Glassdoor's review ordering.
"""
import asyncio
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .config import settings
from . import glassdoor_jobs

BASE = "https://www.glassdoor.com"
MAX_PAGES = 10

# dropdown value -> Glassdoor sort.sortType ("" = Glassdoor's default ordering)
SORT_PARAM = {"": "", "most_recent": "DATE", "most_helpful": "RELEVANCE"}

# one row per review
GLASSDOOR_REVIEW_COLUMNS = [
    "query", "company", "review_title", "rating", "pros", "cons", "reviewer",
    "employment_status", "location", "date", "recommends", "ceo_approval",
    "outlook", "helpful", "review_url", "position",
]


def _blank_row():
    return {c: "" for c in GLASSDOOR_REVIEW_COLUMNS}


def _company_from_url(url: str) -> str:
    m = re.search(r"/Reviews/(.+?)-Reviews-E\d+", url or "")
    return m.group(1).replace("-", " ").strip() if m else ""


def _page_url(url: str, page: int, sort: str) -> str:
    u = url
    if page > 1:                                    # Glassdoor reviews paginate as ..._P2.htm
        u = re.sub(r"\.htm", f"_P{page}.htm", u, count=1)
    val = SORT_PARAM.get(sort or "", "")
    if val:
        sep = "&" if "?" in u else "?"
        u = f"{u}{sep}sort.sortType={val}&sort.ascending=false"
    return u


# ---------------- parsing ----------------

def _txt(node):
    return node.get_text(" ", strip=True) if node else ""


def _row_from_jsonld(j: dict, company: str, query: str) -> dict | None:
    if not isinstance(j, dict):
        return None
    author = j.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    rating = ""
    rr = j.get("reviewRating")
    if isinstance(rr, dict):
        rating = str(rr.get("ratingValue") or "")
    row = _blank_row()
    row["query"] = query
    row["company"] = company
    row["review_title"] = j.get("name") or ""
    row["rating"] = rating
    row["pros"] = j.get("positiveNotes") and re.sub(r"<[^>]+>", " ", str(j.get("positiveNotes"))) or ""
    row["cons"] = j.get("negativeNotes") and re.sub(r"<[^>]+>", " ", str(j.get("negativeNotes"))) or ""
    if not (row["pros"] or row["cons"]):
        row["pros"] = re.sub(r"<[^>]+>", " ", j.get("reviewBody") or "")[:1500].strip()
    row["reviewer"] = author or ""
    row["date"] = j.get("datePublished") or ""
    return row if (row["review_title"] or row["pros"] or row["cons"]) else None


def _parse(html: str, query: str) -> list[dict]:
    """Reviews from JSON-LD first, then visible HTML cards as a fallback."""
    soup = BeautifulSoup(html, "lxml")
    company = _company_from_url(query)
    if not company:
        h1 = soup.select_one("h1")
        company = _txt(h1).split(" Reviews")[0] if h1 else ""

    out, seen = [], set()

    # 1) schema.org Review blocks
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            revs = []
            if obj.get("@type") == "Review":
                revs = [obj]
            elif isinstance(obj.get("review"), list):
                revs = obj["review"]
            for j in revs:
                row = _row_from_jsonld(j, company, query)
                if row:
                    key = (row["review_title"], row["reviewer"], row["date"], (row["pros"] or "")[:40])
                    if key not in seen:
                        seen.add(key)
                        out.append(row)

    # 2) visible review cards (data-test attrs are the most stable selectors)
    cards = soup.select('li[data-test="employer-review"], div[data-test="review-details"], '
                        'li.empReview, div.gdReview')
    for c in cards:
        title_el = (c.select_one('[data-test="review-details-title"]')
                    or c.select_one('h2 a, h3 a') or c.select_one('h2, h3'))
        title = _txt(title_el).strip('"')
        pros = _txt(c.select_one('[data-test="review-text-PROS"], span[data-test="pros"]'))
        cons = _txt(c.select_one('[data-test="review-text-CONS"], span[data-test="cons"]'))
        if not (title or pros or cons):
            continue
        row = _blank_row()
        row["query"] = query
        row["company"] = company
        row["review_title"] = title
        row["pros"] = pros
        row["cons"] = cons
        rt = c.select_one('[data-test="review-rating-label"], [data-test="rating-number"], .ratingNumber')
        row["rating"] = _txt(rt)
        if not row["rating"]:
            sr = c.select_one('span[title]')
            if sr and re.match(r"^[0-5](\.\d)?$", (sr.get("title") or "").strip()):
                row["rating"] = sr["title"].strip()
        row["reviewer"] = _txt(c.select_one('[data-test="review-avatar-label"], .authorJobTitle'))
        status = _txt(c.select_one('.authorInfo, [data-test="review-author"]'))
        row["employment_status"] = status
        row["location"] = _txt(c.select_one('[data-test="review-location"], .authorLocation'))
        row["date"] = _txt(c.select_one('[data-test="review-date"], time, .authorInfo time'))
        ctext = c.get_text(" ", strip=True)
        if "Recommends" in ctext:
            row["recommends"] = "Yes"
        if "Approves of CEO" in ctext:
            row["ceo_approval"] = "Yes"
        if "Positive outlook" in ctext:
            row["outlook"] = "Positive"
        elif "Negative outlook" in ctext:
            row["outlook"] = "Negative"
        hm = re.search(r"(\d[\d,]*)\s+(?:person|people) found this review helpful", ctext, re.I)
        if hm:
            row["helpful"] = hm.group(1).replace(",", "")
        a = title_el if (title_el and title_el.name == "a") else (title_el.find_parent("a") if title_el else None)
        if a and a.get("href"):
            href = a["href"]
            row["review_url"] = href if href.startswith("http") else BASE + href
        key = (row["review_title"], row["reviewer"], row["date"], (row["pros"] or "")[:40])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, limit: int | None, sort: str) -> list[dict]:
    rows, page, last = [], 1, MAX_PAGES
    while page <= last:
        html = glassdoor_jobs._get_text(_page_url(query, page, sort))
        if html is None:
            break   # blocked / no proxy passed — finish quietly (pool already rotated proxies)
        page_rows = _parse(html, query)
        if not page_rows:
            break
        rows += page_rows
        if limit and len(rows) >= limit:
            break
        page += 1
    return rows[:limit] if limit else rows


async def search(query: str, limit: int | None, sort: str) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, sort)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in GLASSDOOR_REVIEW_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None, sort: str = "") -> None:
    """Background task: scrape each company's Glassdoor reviews and store the rows."""
    from .db import jobs, gdreviews
    total = 0
    try:
        mode = "glassdoor-free-pool" if not settings.PROXY_URL.strip() else "glassdoor-paid-proxy"
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": mode}})

        for q in queries:
            rows = await search(q, limit, sort)
            if not rows:                       # free proxies flaky — retry once with fresh proxies
                rows = await search(q, limit, sort)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await gdreviews.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
