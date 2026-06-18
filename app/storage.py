"""Per-job file storage: every scrape, when it finishes, is also saved to disk as an
Excel (+ JSON) file inside its own folder — alongside MongoDB (which stays the live store
that the UI polls during scraping).

Layout:
    data/<service>/<job_id8>_<timestamp>/results.xlsx
    data/<service>/<job_id8>_<timestamp>/results.json

This module is intentionally standalone: main.py wraps each background scrape so that, the
moment it returns (done / stopped / error), the rows already written to Mongo are exported
here. The scraper modules themselves are untouched.
"""
import asyncio
import json
import os
import re
from datetime import datetime

from .db import jobs

DATA_ROOT = "data"


def _slug(s, default="job"):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "")).strip("-")
    return s or default


def _xlsx_cell(v):
    """Excel-safe cell value: scalars pass through, lists become comma strings, rest str()."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def _header_from_rows(rows):
    """First-seen union of keys across all rows (stable column order)."""
    header, seen = [], set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                header.append(k)
    return header


def _write_sync(service, job_id, rows, header):
    import openpyxl

    if not header:
        header = _header_from_rows(rows)
    # unique per run: <service>_<job8>_<UTC date_time> — a fresh, never-overwritten file each scrape
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    svc = _slug(service)
    name = f"{svc}_{job_id[:8]}_{ts}"
    folder = os.path.join(DATA_ROOT, svc, name)
    os.makedirs(folder, exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(list(header))
    for r in rows:
        ws.append([_xlsx_cell(r.get(c, "")) for c in header])
    xlsx_path = os.path.join(folder, name + ".xlsx")
    wb.save(xlsx_path)

    json_path = os.path.join(folder, name + ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)

    return folder, xlsx_path, json_path


async def archive(service, job_id, rows, header=None):
    """Write the job's rows to data/<service>/<job>/results.{xlsx,json} and record the
    paths on the job document. Off-loads the (blocking) openpyxl write to a thread."""
    rows = list(rows)
    folder, xlsx_path, json_path = await asyncio.to_thread(
        _write_sync, service, job_id, rows, header)
    await jobs.update_one({"job_id": job_id}, {"$set": {
        "data_dir": folder,
        "data_excel": xlsx_path,
        "data_json": json_path,
        "data_rows": len(rows),
        "data_saved_at": datetime.utcnow(),
    }})
    return folder
