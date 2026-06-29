"""Phone Numbers Enricher — validity, line type, carrier, location and formatting for each phone.

Pure OFFLINE lookup via the `phonenumbers` library (Google's libphonenumber metadata) — no network,
no proxy. Input is international phone numbers (e.g. "+1 281 236 8208"); a number without a country
prefix is parsed against _DEFAULT_REGION. Carrier/location data is best-effort (libphonenumber has
carrier names mainly for mobile numbers; number portability leaves many blank). One row per number.
"""
import asyncio
from datetime import datetime

import phonenumbers
from phonenumbers import carrier, geocoder

PE_COLUMNS = ["phone", "valid", "type", "carrier", "location", "country", "country_code",
              "e164", "international", "national"]

# libphonenumber PhoneNumberType enum -> readable name
_TYPE_NAMES = {0: "fixed_line", 1: "mobile", 2: "fixed_line_or_mobile", 3: "toll_free",
               4: "premium_rate", 5: "shared_cost", 6: "voip", 7: "personal_number",
               8: "pager", 9: "uan", 10: "voicemail", 99: "unknown"}

_DEFAULT_REGION = "US"  # used only for bare national numbers (no "+<country code>" prefix)


def _parse(raw: str):
    """Parse as-is (works for +<cc>… numbers); fall back to the default region for bare numbers."""
    for region in (None, _DEFAULT_REGION):
        try:
            return phonenumbers.parse(raw, region)
        except phonenumbers.NumberParseException:
            continue
    return None


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input phone -> one enrichment row (validity, type, carrier, location, formats)."""
    raw = (query or "").strip()
    if not raw:
        return []
    row = {c: "" for c in PE_COLUMNS}
    row["phone"] = raw
    row["valid"] = False
    n = _parse(raw)
    if n is None:
        return [row]
    row["valid"] = phonenumbers.is_valid_number(n)
    row["type"] = _TYPE_NAMES.get(phonenumbers.number_type(n), "unknown")
    row["carrier"] = carrier.name_for_number(n, "en") or ""
    row["location"] = geocoder.description_for_number(n, "en") or ""
    row["country"] = phonenumbers.region_code_for_number(n) or ""
    row["country_code"] = f"+{n.country_code}" if n.country_code else ""
    row["e164"] = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    row["international"] = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    row["national"] = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.NATIONAL)
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, phone_enricher
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await phone_enricher.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
