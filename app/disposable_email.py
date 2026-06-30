"""Disposable Email Checker — classify each email's domain as disposable, free or corporate.

Offline classification (no network): disposable domains via the shared `enrich._DISPOSABLE` set,
free/webmail providers via a built-in list, everything else with a valid format is treated as corporate.
One row per email.
"""
import asyncio
from datetime import datetime

from . import enrich

DE_COLUMNS = ["email", "domain", "type", "disposable", "free", "corporate"]

# common free / webmail providers (not disposable, not corporate)
_FREE = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.in", "ymail.com", "rocketmail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com",
    "me.com", "mac.com", "protonmail.com", "proton.me", "gmx.com", "gmx.net", "gmx.de", "mail.com",
    "zoho.com", "yandex.com", "yandex.ru", "tutanota.com", "fastmail.com", "hey.com", "qq.com",
    "163.com", "126.com", "sina.com", "naver.com", "rediffmail.com",
}


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input email -> one classification row (disposable / free / corporate)."""
    addr = (query or "").strip()
    if not addr:
        return []
    row = {c: "" for c in DE_COLUMNS}
    row["email"] = addr
    if not enrich.EMAIL_RE.fullmatch(addr):
        row.update(type="invalid", domain="", disposable=False, free=False, corporate=False)
        return [row]
    domain = addr.partition("@")[2].lower()
    row["domain"] = domain
    disposable = domain in enrich._DISPOSABLE
    free = (not disposable) and domain in _FREE
    corporate = not disposable and not free
    row.update(type="disposable" if disposable else "free" if free else "corporate",
               disposable=disposable, free=free, corporate=corporate)
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, disposable_email
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await disposable_email.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
