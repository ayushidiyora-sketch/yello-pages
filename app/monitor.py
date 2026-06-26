"""Reviews Monitoring engine — recurring review scrapes + email reports (Trustpilot & Google Maps).

A monitor doc lives in the `monitors` collection (config + last-run state). A background loop
(`monitor_loop`, launched on startup) re-runs each monitor at its frequency, scrapes reviews via the
scraper for its `kind` (Trustpilot proxy-only, or Google Maps official Places API), flags reviews
with `rating <= threshold` as negative, and emails an HTML report. Survives restarts (state in Mongo).
"""
import asyncio
import uuid
from datetime import datetime, timedelta
from html import escape

from .emailer import send_email

FREQ_DAYS = {"daily": 1, "weekly": 7, "3weeks": 21, "monthly": 30, "3months": 90}
FREQ_LABEL = {"daily": "once a day", "weekly": "once a week", "3weeks": "once every 3 weeks",
              "monthly": "once a month", "3months": "once every 3 months"}

# A scan that returns 0 rows is usually a transient rate-limit (esp. Google Play on the free proxy
# pool), not "no reviews". Retry it soon (a few times) instead of waiting the full frequency.
RETRY_MINUTES = 10
MAX_EMPTY_RETRIES = 3

# per-kind wiring: human label + which field carries the business/place name in a scraped row.
_KIND = {
    "trustpilot": {"label": "Trustpilot", "name_field": "business"},
    "gmaps": {"label": "Google Maps", "name_field": "place_name"},
    "gplay": {"label": "Google Play", "name_field": "app_id"},
    "booking_reviews": {"label": "Booking", "name_field": "query"},
}


def _next_run(freq: str, frm: datetime) -> datetime:
    return frm + timedelta(days=FREQ_DAYS.get(freq, 7))


def _is_negative(r: dict, threshold: int) -> bool:
    val = r.get("rating")
    if val in (None, ""):
        val = r.get("score")            # Booking reviews carry the score (0–10) here
    if val in (None, ""):
        return False                    # no rating/score (e.g. featured review) -> not counted
    try:
        return float(val) <= float(threshold)
    except (TypeError, ValueError):
        return False


def _report_html(label, business, freq, threshold, total, negatives) -> str:
    rows = "".join(
        f"<tr><td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('reviewer') or n.get('reviewer_name') or 'Anonymous'))}</td>"
        f"<td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('rating') or n.get('score') or ''))}★</td>"
        f"<td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('date') or '')[:10])}</td>"
        f"<td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('title') or n.get('review_title') or ''))}<br>{escape(str(n.get('review') or n.get('liked') or n.get('disliked') or '')[:300])}</td></tr>"
        for n in negatives[:50]
    ) or "<tr><td colspan='4' style='padding:8px'>No negative reviews this cycle. 🎉</td></tr>"
    return f"""<div style="font-family:Arial,sans-serif;color:#222">
      <h2>{escape(label)} monitoring report</h2>
      <p><b>Business:</b> {escape(str(business or '—'))}<br>
         <b>Frequency:</b> {escape(FREQ_LABEL.get(freq, freq))}<br>
         <b>Reviews scanned:</b> {total}<br>
         <b>Negative (rating ≤ {threshold}):</b> <span style="color:#c0392b"><b>{len(negatives)}</b></span></p>
      <table style="border-collapse:collapse;font-size:14px">
        <thead><tr><th style="padding:4px 8px;border:1px solid #eee">Reviewer</th>
        <th style="padding:4px 8px;border:1px solid #eee">Rating</th>
        <th style="padding:4px 8px;border:1px solid #eee">Date</th>
        <th style="padding:4px 8px;border:1px solid #eee">Review</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px">Sent by Live Scraper · {escape(label)} Reviews Monitoring</p>
    </div>"""


async def _scrape(kind, queries, limit, language, sort) -> list[dict]:
    rows = []
    if kind == "gmaps":
        from . import gmaps_reviews
        for q in queries:
            rows += await gmaps_reviews.search(q, sort, limit, language)
    elif kind == "gplay":
        from . import gplay
        for q in queries:
            rows += await gplay.search(q, limit, sort, language)
    elif kind == "booking_reviews":
        from . import booking_reviews
        for q in queries:
            rows += await booking_reviews.search(q, limit, sort)
    else:
        from . import trustpilot_reviews
        for q in queries:
            rows += await trustpilot_reviews.search(q, limit, language)
    return rows


async def run_monitor(mon: dict, job_id: str | None = None) -> dict:
    """One monitoring cycle: scrape → store → email → update the monitor doc."""
    from .db import jobs, monitors
    from .db import (trustpilot_reviews as tr_coll, gmaps_reviews as gm_coll,
                     gplay_results as gp_coll, booking_reviews_results as bk_coll)
    kind = mon.get("kind", "trustpilot")
    info = _KIND.get(kind, _KIND["trustpilot"])
    coll = {"gmaps": gm_coll, "gplay": gp_coll, "booking_reviews": bk_coll}.get(kind, tr_coll)
    mid = mon["monitor_id"]
    queries = mon["queries"]
    limit = mon.get("limit") or {"gmaps": 100, "gplay": 150}.get(kind, 200)
    language = mon.get("language", "all" if kind == "trustpilot" else "en")
    sort = mon.get("sort", "newest")
    threshold = mon.get("threshold", 3)
    email = mon.get("email")
    freq = mon.get("frequency", "weekly")
    job_id = job_id or uuid.uuid4().hex
    now = datetime.utcnow()
    await jobs.update_one({"job_id": job_id}, {"$set": {
        "job_id": job_id, "kind": f"{kind}_monitoring", "monitor_id": mid, "queries": queries,
        "status": "running", "total_scraped": 0, "started_at": now, "finished_at": None}}, upsert=True)
    try:
        rows = await _scrape(kind, queries, limit, language, sort)
        for r in rows:
            r["job_id"] = job_id
        if rows:
            await coll.insert_many(rows)
        negatives = [r for r in rows if _is_negative(r, threshold)]
        business = rows[0].get(info["name_field"]) if rows else None
        emailed = "skipped (no reviews scraped)"
        if rows:
            subject = f"{info['label']} monitoring: {business or queries[0]} — {len(negatives)} negative review(s)"
            ok, detail = await send_email(email, subject,
                                          _report_html(info["label"], business, freq, threshold,
                                                       len(rows), negatives))
            emailed = f"sent to {email}" if ok else f"not sent — {detail}"
        # Empty scan = likely a transient rate-limit → retry in ~10 min (capped), not the full cycle.
        if rows:
            next_run, empty_retries = _next_run(freq, now), 0
        else:
            empty_retries = mon.get("empty_retries") or 0
            if empty_retries < MAX_EMPTY_RETRIES:
                next_run, empty_retries = now + timedelta(minutes=RETRY_MINUTES), empty_retries + 1
            else:
                next_run, empty_retries = _next_run(freq, now), 0   # give up, resume normal schedule
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": len(rows), "emailed": emailed,
            "negatives": len(negatives), "finished_at": datetime.utcnow()}})
        await monitors.update_one({"monitor_id": mid}, {"$set": {
            "last_run": now, "next_run": next_run, "last_job_id": job_id, "empty_retries": empty_retries,
            "last_total": len(rows), "last_negatives": len(negatives), "last_emailed": emailed,
            "last_error": None}})
        return {"total": len(rows), "negatives": len(negatives), "emailed": emailed}
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
        await monitors.update_one({"monitor_id": mid}, {"$set": {
            "last_run": now, "next_run": _next_run(freq, now), "last_job_id": job_id,
            "last_error": str(e)}})
        return {"error": str(e)}


async def start_monitor(queries, frequency, email, threshold, language="all", limit=200,
                        kind="trustpilot", sort="newest") -> dict:
    """Create a monitor and kick off its immediate first run (returns the run's job_id to poll)."""
    from .db import monitors
    mid = uuid.uuid4().hex
    now = datetime.utcnow()
    doc = {"monitor_id": mid, "kind": kind, "queries": queries, "frequency": frequency,
           "email": email, "threshold": threshold, "language": language, "sort": sort,
           "limit": limit, "status": "active",
           "created_at": now, "next_run": _next_run(frequency, now), "last_run": None,
           "last_job_id": None, "last_total": None, "last_negatives": None, "last_emailed": None,
           "last_error": None}
    await monitors.insert_one(doc)
    job_id = uuid.uuid4().hex
    asyncio.create_task(run_monitor(dict(doc), job_id))
    return {"monitor_id": mid, "job_id": job_id, "next_run": doc["next_run"].isoformat() + "Z"}


async def monitor_loop() -> None:
    """Background scheduler: every tick, claim + run any active monitor whose next_run is due."""
    from .config import settings
    from .db import monitors
    while True:
        try:
            now = datetime.utcnow()
            due = [m async for m in monitors.find({"status": "active", "next_run": {"$lte": now}})]
            for m in due:
                # atomically claim it (push next_run forward) so a slow run isn't picked up twice
                claimed = await monitors.update_one(
                    {"monitor_id": m["monitor_id"], "next_run": {"$lte": now}},
                    {"$set": {"next_run": _next_run(m.get("frequency", "weekly"), now)}})
                if claimed.modified_count:
                    try:
                        await run_monitor(m)
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(max(15, settings.MONITOR_TICK_SECONDS))
