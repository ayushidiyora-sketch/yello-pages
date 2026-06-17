import asyncio
import json
import os
import random
import re
from datetime import datetime

from .config import settings
from .db import jobs, businesses
from . import enrich

# ----- Regions: code -> base URL. Only the US site (yellowpages.com) is implemented;
# the others are selectable in the UI but need a site-specific parser. -----
REGIONS = {
    "us": "https://www.yellowpages.com",
    "au": "https://www.yellowpages.com.au",
    "ca": "https://www.yellowpages.ca",
    "be": "https://www.goldenpages.be",
    "fr": "https://www.yellowpages.fr",
}
SUPPORTED_REGIONS = {"us", "au", "ca"}

# ----- Cooperative cancellation: a Stop request drops the job_id in here; run_scrape
# checks it between pages and finishes early with status "stopped". -----
STOP_REQUESTS: set[str] = set()


def request_stop(job_id: str) -> None:
    STOP_REQUESTS.add(job_id)


def apply_view(rows: list[dict], sort: str | None = None,
               categories: list[str] | None = None) -> list[dict]:
    """Apply the user's Category filter + Sort to already-scraped rows.

    YP filters/sorts only client-side (not via URL params), so we replicate it on our own
    data. `categories` keeps rows tagged with ANY selected category label (exact match on
    the per-row `categories` list). `sort` is one of "name" (A-Z) or "average_rating"
    (high-to-low); anything else preserves scrape order. `sorted` is stable, so ties and
    the default order are preserved."""
    out = rows
    if categories:
        sel = set(categories)
        out = [r for r in out if sel & set(r.get("categories") or [])]
    if sort == "name":
        out = sorted(out, key=lambda r: (r.get("name") or "").lower())
    elif sort == "average_rating":
        def rating_key(r):
            try:
                return -float(r.get("rating"))
            except (TypeError, ValueError):
                return 1.0  # unrated sinks to the bottom
        out = sorted(out, key=rating_key)
    return out


async def export_json(job_id: str, search: str, location: str) -> str:
    os.makedirs("exports", exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    path = os.path.join("exports", f"{safe(search)}_{safe(location)}_{ts}.json")
    # honor the Category filter + Sort chosen for this job, so the export matches the view
    job = await jobs.find_one({"job_id": job_id}, {"_id": 0, "sort": 1, "categories": 1}) or {}
    rows = []
    async for doc in businesses.find({"job_id": job_id}, {"_id": 0}):
        if isinstance(doc.get("scraped_at"), datetime):
            doc["scraped_at"] = doc["scraped_at"].isoformat()
        rows.append(doc)
    rows = apply_view(rows, job.get("sort"), job.get("categories"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    return path


async def _save(job_id: str, items: list[dict], seen: set, remaining: int | None = None,
                query: str = "", position_start: int = 0) -> int:
    """Insert new (deduped) items live; return how many were added. If `remaining` is
    given, stop once that many have been added (respects the record limit). Also persists
    the derived columns (query, 1-based position, split email_1/2/3, founded_year, industry)
    onto each document so the database row matches the exported record."""
    added = 0
    for it in items:
        if remaining is not None and added >= remaining:
            break
        key = (it.get("name"), it.get("phone"))
        if key in seen:
            continue
        seen.add(key)
        it["job_id"] = job_id
        it["scraped_at"] = datetime.utcnow()
        it["query"] = query
        it["position"] = position_start + added + 1
        emails = it.get("emails") or []
        it["email_1"] = emails[0] if len(emails) > 0 else None
        it["email_2"] = emails[1] if len(emails) > 1 else None
        it["email_3"] = emails[2] if len(emails) > 2 else None
        # free company-insights derivations from data we already scraped
        it["industry"] = (it.get("categories") or [None])[0] or (
            (it.get("category") or "").split(",")[0].strip() or None)
        it["founded_year"] = None
        yrs = it.get("years_in_business")
        if yrs:
            try:
                it["founded_year"] = datetime.utcnow().year - int(re.sub(r"\D", "", str(yrs)))
            except (ValueError, TypeError):
                pass
        try:
            await businesses.insert_one(it)
            added += 1
        except Exception:
            pass  # unique-index duplicate
    return added


async def run_scrape(job_id: str, search: str, location: str,
                     region: str = "us", limit: int | None = None):
    """Background task: scrape yellowpages.com (US) page by page, inserting each record
    live. Honors a per-job record `limit` and a cooperative Stop request."""
    total = 0
    seen: set = set()
    hard_cap = limit if (limit and limit > 0) else None
    query = ", ".join(p for p in (search, location) if p)  # stored on each record

    def stopped() -> bool:
        return job_id in STOP_REQUESTS

    async def finish(status: str):
        STOP_REQUESTS.discard(job_id)
        export_path = await export_json(job_id, search, location)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": status, "total_scraped": total,
            "finished_at": datetime.utcnow(), "export_path": export_path,
        }})

    try:
        if region not in SUPPORTED_REGIONS:
            raise RuntimeError(
                f"Region '{region}' isn't supported yet — only US (yellowpages.com), "
                f"AU (yellowpages.com.au) and CA (yellowpages.ca) are implemented. "
                f"Choose US, AU or CA in the Region dropdown."
            )

        # ---- pick the per-region scraper (same card layout; different fetch + URL) ----
        from . import yp_us
        paid = bool(settings.PROXY_URL.strip())
        if region == "au":
            from . import yp_au
            fetch, parse_total, parse_cards = (
                yp_au.fetch_au_page, yp_au.parse_au_total, yp_au.parse_au_cards)
            detail_sync = yp_au.fetch_detail_sync
            proxy_mode = "au-paid-proxy" if paid else "au-free-pool"
            warm_pool = not paid  # route AU through the free pool so no real IP is used
        elif region == "ca":
            from . import yp_ca
            fetch, parse_total, parse_cards = (
                yp_ca.fetch_ca_page, yp_ca.parse_ca_total, yp_ca.parse_ca_cards)
            detail_sync = yp_ca.fetch_detail_sync
            proxy_mode = "ca-paid-proxy" if paid else "ca-free-pool"
            warm_pool = not paid  # route CA through the free pool so no real IP is used
        else:  # us
            fetch, parse_total, parse_cards = (
                yp_us.fetch_us_page, yp_us.parse_us_total, yp_us.parse_us_cards)
            detail_sync = yp_us.fetch_detail_sync
            proxy_mode = "us-paid-proxy" if paid else "us-free-pool"
            warm_pool = not paid
        await jobs.update_one({"job_id": job_id}, {"$set": {"proxy_mode": proxy_mode}})
        BATCH = 4  # pages fetched concurrently per round

        async def detail_fetch(url):
            return await asyncio.to_thread(detail_sync, url)

        async def collect(html_cards):
            """Website-enrichment + amenities (detail page) for a page's cards — run in
            parallel (independent fields) to cut wall-clock time."""
            await asyncio.gather(
                enrich.enrich_cards(html_cards, region),
                enrich.enrich_amenities(html_cards, detail_fetch),
            )
            return html_cards

        # Page 1 first: gives the grand total and (US free pool) warms _GOOD.
        if stopped():
            await finish("stopped"); return
        first_html = await fetch(search, location, 1)
        page_total = parse_total(first_html)
        max_records = page_total or (settings.MAX_PAGES * 30)
        if hard_cap:
            max_records = min(max_records, hard_cap)
        await jobs.update_one({"job_id": job_id}, {"$set": {"total_available": page_total}})

        # Warm the pool BEFORE enriching page 1. Website crawls + amenity detail fetches all go
        # through the free pool; an unwarmed pool (or a one-page job, which skips the paging warm
        # below) starves them and every enrichment column comes back empty. Probe a few proxies
        # first so page-1 enrichment actually has connections to use.
        if warm_pool and settings.ENRICH:
            await asyncio.to_thread(
                yp_us.ensure_pool,
                {"search_terms": search, "geo_location_terms": location, "page": "1"},
                BATCH + 2,
            )

        first_cards = await collect(parse_cards(first_html))
        total += await _save(job_id, first_cards, seen,
                             remaining=(hard_cap - total) if hard_cap else None,
                             query=query, position_start=total)
        await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        # No results: page 1 is a valid SERP but has zero listings. Finish immediately as
        # done(0) instead of paging through empties — so the UI stops polling and shows
        # "No listings found" right away (any region).
        if total == 0 and not page_total:
            await finish("done"); return

        last_pages = max(1, -(-max_records // 30))  # ceil(records / 30 per page)
        next_page = 2

        # Only warm the pool if we'll actually fetch more pages (skip for one-page jobs,
        # e.g. limit <= 30 — otherwise we'd waste time probing proxies we never use).
        if warm_pool and total < max_records and next_page <= last_pages:
            # enough warm proxies for one concurrent batch; stops probing early once found
            await asyncio.to_thread(
                yp_us.ensure_pool,
                {"search_terms": search, "geo_location_terms": location, "page": "1"},
                BATCH + 2,
            )
        while total < max_records and next_page <= last_pages:
            if stopped():
                await finish("stopped"); return
            batch = list(range(next_page, min(next_page + BATCH, last_pages + 1)))
            next_page += BATCH
            htmls = await asyncio.gather(
                *[fetch(search, location, p) for p in batch],
                return_exceptions=True,
            )
            empty = False
            for h in htmls:
                if isinstance(h, Exception):
                    continue
                cards = parse_cards(h)
                if not cards:
                    empty = True
                    continue
                cards = await collect(cards)
                total += await _save(job_id, cards, seen,
                                     remaining=(hard_cap - total) if hard_cap else None,
                                     query=query, position_start=total)
                if hard_cap and total >= hard_cap:
                    break
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
            if empty or (hard_cap and total >= hard_cap):
                break
        await finish("stopped" if stopped() else "done")
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow(),
        }})
