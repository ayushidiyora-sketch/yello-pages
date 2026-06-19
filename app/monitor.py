"""Trustpilot Reviews Monitoring engine — recurring proxy-only review scrapes + email reports.

A monitor doc lives in the `monitors` collection (config + last-run state). A background loop
(`monitor_loop`, launched on startup) re-runs each monitor at its frequency, scrapes reviews through
the proxy-only path (`trustpilot_reviews.search` — real IP never used), flags reviews with
`rating <= threshold` as negative, and emails an HTML report. Survives restarts (state in Mongo).
"""
import asyncio
import uuid
from datetime import datetime, timedelta
from html import escape

from . import trustpilot_reviews
from .emailer import send_email

FREQ_DAYS = {"daily": 1, "weekly": 7, "3weeks": 21, "monthly": 30, "3months": 90}
FREQ_LABEL = {"daily": "once a day", "weekly": "once a week", "3weeks": "once every 3 weeks",
              "monthly": "once a month", "3months": "once every 3 months"}


def _next_run(freq: str, frm: datetime) -> datetime:
    return frm + timedelta(days=FREQ_DAYS.get(freq, 7))


def _is_negative(r: dict, threshold: int) -> bool:
    try:
        return float(r.get("rating") or 0) <= float(threshold)
    except (TypeError, ValueError):
        return False


def _report_html(business, freq, threshold, total, negatives) -> str:
    rows = "".join(
        f"<tr><td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('reviewer') or 'Anonymous'))}</td>"
        f"<td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('rating') or ''))}★</td>"
        f"<td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('date') or '')[:10])}</td>"
        f"<td style='padding:4px 8px;border:1px solid #eee'>{escape(str(n.get('title') or ''))}<br>{escape(str(n.get('review') or '')[:300])}</td></tr>"
        for n in negatives[:50]
    ) or "<tr><td colspan='4' style='padding:8px'>No negative reviews this cycle. 🎉</td></tr>"
    return f"""<div style="font-family:Arial,sans-serif;color:#222">
      <h2>Trustpilot monitoring report</h2>
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
      <p style="color:#888;font-size:12px">Sent by Live Scraper · Trustpilot Reviews Monitoring</p>
    </div>"""


async def _scrape(queries, limit, language) -> list[dict]:
    rows = []
    for q in queries:
        rows += await trustpilot_reviews.search(q, limit, language)
    return rows


async def run_monitor(mon: dict, job_id: str | None = None) -> dict:
    """One monitoring cycle: scrape (proxy-only) → store → email → update the monitor doc."""
    from .db import jobs, trustpilot_reviews as tr_coll, monitors
    mid = mon["monitor_id"]
    queries = mon["queries"]
    limit = mon.get("limit") or 200
    language = mon.get("language", "all")
    threshold = mon.get("threshold", 3)
    email = mon.get("email")
    freq = mon.get("frequency", "weekly")
    job_id = job_id or uuid.uuid4().hex
    now = datetime.utcnow()
    await jobs.update_one({"job_id": job_id}, {"$set": {
        "job_id": job_id, "kind": "trustpilot_monitoring", "monitor_id": mid, "queries": queries,
        "status": "running", "total_scraped": 0, "started_at": now, "finished_at": None}}, upsert=True)
    try:
        rows = await _scrape(queries, limit, language)
        for r in rows:
            r["job_id"] = job_id
        if rows:
            await tr_coll.insert_many(rows)
        negatives = [r for r in rows if _is_negative(r, threshold)]
        business = rows[0].get("business") if rows else None
        emailed = "skipped (no reviews scraped)"
        if rows:
            subject = f"Trustpilot monitoring: {business or queries[0]} — {len(negatives)} negative review(s)"
            ok, detail = await send_email(email, subject,
                                          _report_html(business, freq, threshold, len(rows), negatives))
            emailed = f"sent to {email}" if ok else f"not sent — {detail}"
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": len(rows), "finished_at": datetime.utcnow()}})
        await monitors.update_one({"monitor_id": mid}, {"$set": {
            "last_run": now, "next_run": _next_run(freq, now), "last_job_id": job_id,
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


async def start_monitor(queries, frequency, email, threshold, language="all", limit=200) -> dict:
    """Create a monitor and kick off its immediate first run (returns the run's job_id to poll)."""
    from .db import monitors
    mid = uuid.uuid4().hex
    now = datetime.utcnow()
    doc = {"monitor_id": mid, "queries": queries, "frequency": frequency, "email": email,
           "threshold": threshold, "language": language, "limit": limit, "status": "active",
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
