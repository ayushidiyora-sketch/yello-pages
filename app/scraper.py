import asyncio
import json
import os
import random
import re
from datetime import datetime

from .config import settings
from .db import jobs, businesses

# ----- Regions: code -> base URL. Only the US site (yellowpages.com) is implemented;
# the others are selectable in the UI but need a site-specific parser. -----
REGIONS = {
    "us": "https://www.yellowpages.com",
    "au": "https://www.yellowpages.com.au",
    "ca": "https://www.yellowpages.ca",
    "be": "https://www.goldenpages.be",
    "fr": "https://www.yellowpages.fr",
}
SUPPORTED_REGIONS = {"us"}

# ----- Cooperative cancellation: a Stop request drops the job_id in here; run_scrape
# checks it between pages and finishes early with status "stopped". -----
STOP_REQUESTS: set[str] = set()


def request_stop(job_id: str) -> None:
    STOP_REQUESTS.add(job_id)


async def export_json(job_id: str, search: str, location: str) -> str:
    os.makedirs("exports", exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    path = os.path.join("exports", f"{safe(search)}_{safe(location)}_{ts}.json")
    rows = []
    async for doc in businesses.find({"job_id": job_id}, {"_id": 0}):
        if isinstance(doc.get("scraped_at"), datetime):
            doc["scraped_at"] = doc["scraped_at"].isoformat()
        rows.append(doc)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    return path


async def _save(job_id: str, items: list[dict], seen: set, remaining: int | None = None) -> int:
    """Insert new (deduped) items live; return how many were added. If `remaining` is
    given, stop once that many have been added (respects the record limit)."""
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
                f"Region '{region}' isn't supported yet — only YellowPages US "
                f"(yellowpages.com) is implemented. Choose 'US' in the Region dropdown."
            )

        # ---- US: yellowpages.com via curl_cffi + US proxy (concurrent pages) ----
        from . import yp_us
        paid = bool(settings.PROXY_URL.strip())
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "proxy_mode": "us-paid-proxy" if paid else "us-free-pool",
        }})
        BATCH = 4  # pages fetched concurrently per round

        # Page 1 first: gives the grand total and (for the free pool) warms _GOOD.
        if stopped():
            await finish("stopped"); return
        first_html = await yp_us.fetch_us_page(search, location, 1)
        page_total = yp_us.parse_us_total(first_html)
        max_records = page_total or (settings.MAX_PAGES * 30)
        if hard_cap:
            max_records = min(max_records, hard_cap)
        await jobs.update_one({"job_id": job_id}, {"$set": {"total_available": page_total}})

        total += await _save(job_id, yp_us.parse_us_cards(first_html), seen,
                             remaining=(hard_cap - total) if hard_cap else None)
        await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})

        last_pages = max(1, -(-max_records // 30))  # ceil(records / 30 per page)
        next_page = 2

        # Only warm the pool if we'll actually fetch more pages (skip for one-page jobs,
        # e.g. limit <= 30 — otherwise we'd waste time probing proxies we never use).
        if not paid and total < max_records and next_page <= last_pages:
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
                *[yp_us.fetch_us_page(search, location, p) for p in batch],
                return_exceptions=True,
            )
            empty = False
            for h in htmls:
                if isinstance(h, Exception):
                    continue
                cards = yp_us.parse_us_cards(h)
                if not cards:
                    empty = True
                    continue
                total += await _save(job_id, cards, seen,
                                     remaining=(hard_cap - total) if hard_cap else None)
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
