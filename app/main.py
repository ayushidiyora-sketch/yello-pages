import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .db import jobs, businesses, ensure_indexes
from .models import ScrapeRequest
from .scraper import run_scrape, request_stop, apply_view, REGIONS, SUPPORTED_REGIONS
from . import yp_us


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_indexes()
    yield


app = FastAPI(title="YellowPages US Scraper", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/api/regions")
async def regions():
    """Region options for the dropdown. `supported` = parser implemented today."""
    labels = {
        "us": "US (yellowpages.com)",
        "au": "AU (yellowpages.com.au)",
        "ca": "CA (yellowpages.ca)",
        "be": "BE (goldenpages.be)",
        "fr": "FR (yellowpages.fr)",
    }
    return [
        {"code": code, "label": labels.get(code, code), "supported": code in SUPPORTED_REGIONS}
        for code in REGIONS
    ]


@app.get("/api/filters")
async def filters(search: str, location: str, region: str = "us"):
    """YP's own filter options (Category / Features / Neighborhoods) for this search,
    used to populate the All Filters modal. Live fetch — may take a few seconds."""
    if region not in SUPPORTED_REGIONS:
        raise HTTPException(400, "Filters are only available for the US region.")
    return await yp_us.get_filters(search, location)


@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id,
        "search": req.search,
        "location": req.location,
        "region": req.region,
        "limit": req.limit,
        "sort": req.sort,
        "categories": req.categories,
        "status": "running",
        "total_scraped": 0,
        "started_at": datetime.utcnow(),
        "finished_at": None,
    })
    # launch background scraping (returns immediately)
    asyncio.create_task(run_scrape(job_id, req.search, req.location, req.region, req.limit))
    return {"job_id": job_id}


@app.post("/api/stop/{job_id}")
async def stop(job_id: str):
    doc = await jobs.find_one({"job_id": job_id}, {"_id": 0, "status": 1})
    if not doc:
        raise HTTPException(404, "job not found")
    if doc.get("status") == "running":
        request_stop(job_id)
    return {"job_id": job_id, "stopping": True}


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    doc = await jobs.find_one({"job_id": job_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "job not found")
    return doc


@app.get("/api/results/{job_id}")
async def results(job_id: str, limit: int = 200):
    job = await jobs.find_one({"job_id": job_id}, {"_id": 0, "sort": 1, "categories": 1}) or {}
    # fetch all, then apply the job's Category filter + Sort, then cap (sort/filter need the
    # full set before slicing — a DB-level .limit() would truncate before the view applies)
    rows = [d async for d in businesses.find({"job_id": job_id}, {"_id": 0})]
    rows = apply_view(rows, job.get("sort"), job.get("categories"))
    return rows[:limit]


@app.get("/api/export/{job_id}")
async def export(job_id: str):
    doc = await jobs.find_one({"job_id": job_id})
    if not doc or not doc.get("export_path"):
        raise HTTPException(404, "export not ready")
    import os
    return FileResponse(
        doc["export_path"],
        media_type="application/json",
        filename=os.path.basename(doc["export_path"]),
    )
