import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, Response

from .db import jobs, businesses, products, ensure_indexes
from .models import ScrapeRequest, AmazonScrapeRequest
from .scraper import run_scrape, request_stop, apply_view, REGIONS, SUPPORTED_REGIONS
from . import yp_us, amazon


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_indexes()
    yield


app = FastAPI(title="YellowPages US Scraper", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/api/services")
async def services():
    """The Live Scraper service catalog (from the source sheet) for the sidebar."""
    import json
    with open("static/services.json", encoding="utf-8") as f:
        return json.load(f)


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


@app.post("/api/amazon/scrape")
async def start_amazon_scrape(req: AmazonScrapeRequest):
    queries = [q.strip() for q in (req.queries or []) if q and q.strip()]
    if not queries:
        raise HTTPException(400, "Provide at least one ASIN, product URL, or search.")
    job_id = uuid.uuid4().hex
    # Expected total for the progress bar: a direct ASIN/product URL yields 1 product;
    # a search query yields up to `limit`. (So 3 product URLs => 3, not 3 x limit.)
    expected = 0
    for q in queries:
        spec = amazon.classify(q, req.domain)
        if not spec:
            continue
        expected += 1 if spec[0] in ("asin", "product", "product_url") else max(1, req.limit)
    await jobs.insert_one({
        "job_id": job_id,
        "kind": "amazon",
        "domain": req.domain,
        "postcode": req.postcode,
        "language": req.language,
        "currency": req.currency,
        "limit": req.limit,
        "queries": queries,
        "status": "running",
        "total_scraped": 0,
        "total_available": expected or len(queries),
        "started_at": datetime.utcnow(),
        "finished_at": None,
    })
    asyncio.create_task(amazon.run_amazon_scrape(
        job_id, queries, req.domain, req.postcode, req.language, req.currency, req.limit))
    return {"job_id": job_id}


@app.post("/api/amazon/parse-file")
async def amazon_parse_file(file: UploadFile = File(...)):
    """Extract Amazon product URLs / ASINs from an uploaded CSV/XLSX/TXT/Parquet file
    so the UI can drop them into the Product ASINs/URLs box."""
    data = await file.read()
    try:
        lines = amazon.parse_upload(file.filename or "", data)
    except Exception as e:
        raise HTTPException(400, f"Could not parse '{file.filename}': {e}")
    return {"lines": lines, "count": len(lines)}


@app.get("/api/amazon/results/{job_id}")
async def amazon_results(job_id: str, limit: int = 1000):
    rows = [amazon.to_export(d) async for d in products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows[:limit]


@app.get("/api/amazon/export-excel/{job_id}")
async def amazon_export_excel(job_id: str):
    """Download all scraped products as an .xlsx with the full 95-column schema."""
    import io
    import openpyxl

    rows = [amazon.to_export(d) async for d in products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)

    # header = fixed 95 columns + any dynamic details_/overview_ columns the products have
    fixed = set(amazon.EXPORT_COLUMNS)
    extras = sorted({k for r in rows for k in r if k not in fixed})
    header = [c for c in amazon.EXPORT_COLUMNS if c not in ("position", "query")] \
        + extras + ["position", "query"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(header)
    for r in rows:
        ws.append([_xlsx_cell(r.get(c, "")) for c in header])

    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="amazon_products_{job_id[:8]}.xlsx"'},
    )


def _xlsx_cell(v):
    """Excel-safe cell value: scalars pass through, anything else becomes a string."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


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
