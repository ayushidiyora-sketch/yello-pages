import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, Response

from .db import (jobs, businesses, products, reviews, ebay_products, gresults, bbbresults,
                 g2reviews, bbbreviews, gjobs, gdreviews, walmart_products, walmart_reviews,
                 youtube_channels, airbnb_reviews, expedia_results, trustpilot_results,
                 hotels_results, hotels_reviews, trustpilot_search_results, homedepot_results,
                 trustpilot_reviews, monitors, ensure_indexes)
from .models import (ScrapeRequest, AmazonScrapeRequest, AmazonReviewsRequest,
                     EbayScrapeRequest, GSearchRequest, BBBRequest, G2Request, BBBReviewsRequest,
                     GlassdoorJobsRequest, GlassdoorReviewsRequest, WalmartProductsRequest,
                     WalmartReviewsRequest, YouTubeChannelsRequest, AirbnbReviewsRequest,
                     ExpediaRequest, TrustpilotRequest, HotelsRequest, HotelsReviewsRequest,
                     TrustpilotSearchRequest, HomeDepotRequest, TrustpilotReviewsRequest,
                     TrustpilotMonitorRequest)
from .scraper import run_scrape, request_stop, apply_view, REGIONS, SUPPORTED_REGIONS
from . import (yp_us, amazon, amazon_reviews, ebay, gsearch, bbb, bbb_reviews, g2,
               glassdoor_jobs, glassdoor_reviews, walmart, walmart_reviews as walmart_rv,
               youtube_channels as yt_channels, airbnb_reviews as airbnb_rv,
               expedia, trustpilot, hotels, hotels_reviews as hotels_reviews_mod,
               trustpilot_search, homedepot, trustpilot_reviews as trustpilot_reviews_mod,
               monitor, storage)


# ---------------- auto-save each finished job to data/<service>/<job>/results.xlsx ----------------
# A thin wrapper around every background scrape: run it as usual (Mongo stays the live store
# the UI polls), then — done, stopped, or error — export whatever landed in Mongo to a per-job
# folder with an Excel + JSON file. Scraper modules are untouched.

async def _amazon_rows(job_id):
    rows = [amazon.to_export(d) async for d in products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    fixed = set(amazon.EXPORT_COLUMNS)
    extras = sorted({k for r in rows for k in r if k not in fixed})
    header = [c for c in amazon.EXPORT_COLUMNS if c not in ("position", "query")] \
        + extras + ["position", "query"]
    return rows, header


async def _reviews_rows(job_id):
    rows = [amazon_reviews.to_export(d) async for d in reviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, amazon_reviews.REVIEW_COLUMNS


async def _ebay_rows(job_id):
    rows = [ebay.to_export(d) async for d in ebay_products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, ebay.EBAY_COLUMNS


async def _yp_rows(job_id):
    rows = [d async for d in businesses.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows, None


async def _gsearch_rows(job_id):
    rows = [d async for d in gresults.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows, None


async def _bbb_rows(job_id):
    rows = [d async for d in bbbresults.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows, None


async def _bbbreviews_rows(job_id):
    rows = [d async for d in bbbreviews.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows, None


async def _g2_rows(job_id):
    rows = [g2.to_export(d) async for d in g2reviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, g2.G2_COLUMNS


async def _gjobs_rows(job_id):
    rows = [glassdoor_jobs.to_export(d) async for d in gjobs.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, glassdoor_jobs.GLASSDOOR_JOB_COLUMNS


async def _gdreviews_rows(job_id):
    rows = [glassdoor_reviews.to_export(d) async for d in gdreviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, glassdoor_reviews.GLASSDOOR_REVIEW_COLUMNS


async def _walmart_rows(job_id):
    rows = [walmart.to_export(d) async for d in walmart_products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, walmart.WALMART_PRODUCT_COLUMNS


async def _walmartrv_rows(job_id):
    rows = [walmart_rv.to_export(d) async for d in walmart_reviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, walmart_rv.WALMART_REVIEW_COLUMNS


async def _ytchannels_rows(job_id):
    rows = [yt_channels.to_export(d) async for d in youtube_channels.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, yt_channels.YOUTUBE_CHANNEL_COLUMNS


async def _airbnb_rows(job_id):
    rows = [airbnb_rv.to_export(d) async for d in airbnb_reviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows, airbnb_rv.AIRBNB_REVIEW_COLUMNS


async def _run_and_archive(coro, service, job_id, fetch):
    try:
        await coro
    finally:
        try:
            rows, header = await fetch(job_id)
            await storage.archive(service, job_id, rows, header)
        except Exception:
            pass  # archiving must never break the job


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_indexes()
    loop_task = asyncio.create_task(monitor.monitor_loop())  # recurring Trustpilot monitors
    yield
    loop_task.cancel()


app = FastAPI(title="YellowPages US Scraper", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/bbb-seal.svg")
async def bbb_seal():
    """BBB Accredited Business seal, served locally (bbb.org blocks hot-linking)."""
    return FileResponse("static/bbb-seal.svg", media_type="image/svg+xml")


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
        "fr": "FR (pagesjaunes.fr)",
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
    asyncio.create_task(_run_and_archive(
        run_scrape(job_id, req.search, req.location, req.region, req.limit),
        "yellowpages", job_id, _yp_rows))
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
    asyncio.create_task(_run_and_archive(
        amazon.run_amazon_scrape(
            job_id, queries, req.domain, req.postcode, req.language, req.currency, req.limit),
        "amazon_products", job_id, _amazon_rows))
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


# ---------------- Amazon Reviews ----------------

@app.post("/api/amazon-reviews/scrape")
async def start_amazon_reviews(req: AmazonReviewsRequest):
    queries = [q.strip() for q in (req.queries or []) if q and q.strip()]
    if not queries:
        raise HTTPException(400, "Provide at least one ASIN or product URL.")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id,
        "kind": "amazon-reviews",
        "domain": req.domain,
        "limit": req.limit,
        "queries": queries,
        "status": "running",
        "total_scraped": 0,
        "total_available": len(queries) * max(1, req.limit),
        "started_at": datetime.utcnow(),
        "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        amazon_reviews.run_reviews_scrape(job_id, queries, req.domain, req.limit),
        "amazon_reviews", job_id, _reviews_rows))
    return {"job_id": job_id}


@app.get("/api/amazon-reviews/results/{job_id}")
async def amazon_reviews_results(job_id: str, limit: int = 2000):
    rows = [amazon_reviews.to_export(d) async for d in reviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows[:limit]


@app.get("/api/amazon-reviews/export-excel/{job_id}")
async def amazon_reviews_export_excel(job_id: str):
    """Download all scraped reviews as an .xlsx (one row per review)."""
    import io
    import openpyxl

    rows = [amazon_reviews.to_export(d) async for d in reviews.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reviews"
    ws.append(amazon_reviews.REVIEW_COLUMNS)
    for r in rows:
        ws.append([_xlsx_cell(r.get(c, "")) for c in amazon_reviews.REVIEW_COLUMNS])

    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="amazon_reviews_{job_id[:8]}.xlsx"'},
    )


# ---------------- eBay Products ----------------

@app.post("/api/ebay/scrape")
async def start_ebay_scrape(req: EbayScrapeRequest):
    queries = [q.strip() for q in (req.queries or []) if q and q.strip()]
    if not queries:
        raise HTTPException(400, "Provide at least one eBay item id, URL, or search.")
    job_id = uuid.uuid4().hex
    expected = 0
    for q in queries:
        spec = ebay.classify(q, ebay.domain_for(req.country))
        if not spec:
            continue
        expected += 1 if spec[0] == "item" else max(1, req.limit)
    await jobs.insert_one({
        "job_id": job_id, "kind": "ebay", "country": req.country, "postcode": req.postcode,
        "limit": req.limit, "queries": queries, "status": "running", "total_scraped": 0,
        "total_available": expected or len(queries),
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        ebay.run_ebay_scrape(job_id, queries, req.country, req.postcode, req.limit),
        "ebay_products", job_id, _ebay_rows))
    return {"job_id": job_id}


@app.get("/api/ebay/results/{job_id}")
async def ebay_results(job_id: str, limit: int = 1000):
    rows = [ebay.to_export(d) async for d in ebay_products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    return rows[:limit]


@app.get("/api/ebay/export-excel/{job_id}")
async def ebay_export_excel(job_id: str):
    """Download all scraped eBay products as an .xlsx."""
    import io
    import openpyxl

    rows = [ebay.to_export(d) async for d in ebay_products.find({"job_id": job_id}, {"_id": 0})]
    rows.sort(key=lambda r: r.get("position") or 0)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "eBay Products"
    ws.append(ebay.EBAY_COLUMNS)
    for r in rows:
        ws.append([_xlsx_cell(r.get(c, "")) for c in ebay.EBAY_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="ebay_products_{job_id[:8]}.xlsx"'},
    )


@app.post("/api/gsearch")
async def gsearch_start(req: GSearchRequest):
    """Google Search Scraper (powered by DuckDuckGo via the proxy pool — no real IP)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one query is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "gsearch", "queries": queries, "limit": req.limit,
        "date_range": req.date_range, "region": req.region, "language": req.language,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        gsearch.run_job(job_id, queries, req.limit, req.date_range or "", req.region, req.language),
        "google_search", job_id, _gsearch_rows))
    return {"job_id": job_id}


@app.get("/api/gresults/{job_id}")
async def gsearch_results(job_id: str, limit: int = 2000):
    rows = [d async for d in gresults.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/bbb")
async def bbb_start(req: BBBRequest):
    """BBB Business Scraper (bbb.org). Queries may be search terms or bbb.org URLs."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one query is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "bbb", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        bbb.run_job(job_id, queries, req.limit),
        "bbb_business", job_id, _bbb_rows))
    return {"job_id": job_id}


@app.get("/api/bbb/results/{job_id}")
async def bbb_results(job_id: str, limit: int = 2000):
    rows = [d async for d in bbbresults.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/g2")
async def g2_start(req: G2Request):
    """G2 Reviews Scraper (g2.com). Queries may be product-reviews URLs, product URLs, or slugs."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one query is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "g2", "queries": queries, "limit": req.limit,
        "sort": req.sort, "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        g2.run_job(job_id, queries, req.limit, req.sort or ""),
        "g2_reviews", job_id, _g2_rows))
    return {"job_id": job_id}


@app.get("/api/g2/results/{job_id}")
async def g2_results(job_id: str, limit: int = 4000):
    rows, _ = await _g2_rows(job_id)
    return rows[:limit]


@app.post("/api/glassdoor-jobs")
async def glassdoor_jobs_start(req: GlassdoorJobsRequest):
    """Glassdoor Job Scraper — jobs from a glassdoor.com job-search URL."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Glassdoor job-search URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "glassdoor_jobs", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        glassdoor_jobs.run_job(job_id, queries, req.limit),
        "glassdoor_jobs", job_id, _gjobs_rows))
    return {"job_id": job_id}


@app.get("/api/glassdoor-jobs/results/{job_id}")
async def glassdoor_jobs_results(job_id: str, limit: int = 4000):
    rows, _ = await _gjobs_rows(job_id)
    return rows[:limit]


@app.post("/api/glassdoor-reviews")
async def glassdoor_reviews_start(req: GlassdoorReviewsRequest):
    """Glassdoor Reviews Scraper — company reviews from a glassdoor.com reviews URL."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Glassdoor reviews URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "glassdoor_reviews", "queries": queries, "limit": req.limit,
        "sort": req.sort, "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        glassdoor_reviews.run_job(job_id, queries, req.limit, req.sort or ""),
        "glassdoor_reviews", job_id, _gdreviews_rows))
    return {"job_id": job_id}


@app.get("/api/glassdoor-reviews/results/{job_id}")
async def glassdoor_reviews_results(job_id: str, limit: int = 4000):
    rows, _ = await _gdreviews_rows(job_id)
    return rows[:limit]


@app.post("/api/walmart")
async def walmart_start(req: WalmartProductsRequest):
    """Walmart Products Scraper — products from walmart.com /ip/ URLs."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Walmart product URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "walmart", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        walmart.run_job(job_id, queries, req.limit),
        "walmart_products", job_id, _walmart_rows))
    return {"job_id": job_id}


@app.get("/api/walmart/results/{job_id}")
async def walmart_results(job_id: str, limit: int = 4000):
    rows, _ = await _walmart_rows(job_id)
    return rows[:limit]


@app.post("/api/walmart-reviews")
async def walmart_reviews_start(req: WalmartReviewsRequest):
    """Walmart Reviews Scraper — reviews from walmart.com product URLs."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Walmart product URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "walmart_reviews", "queries": queries, "limit": req.limit,
        "sort": req.sort, "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        walmart_rv.run_job(job_id, queries, req.limit, req.sort or ""),
        "walmart_reviews", job_id, _walmartrv_rows))
    return {"job_id": job_id}


@app.get("/api/walmart-reviews/results/{job_id}")
async def walmart_reviews_results(job_id: str, limit: int = 4000):
    rows, _ = await _walmartrv_rows(job_id)
    return rows[:limit]


@app.post("/api/youtube-channels")
async def youtube_channels_start(req: YouTubeChannelsRequest):
    """YouTube Channels Scraper — channel details from URLs or handles."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one channel URL or handle is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "youtube_channels", "queries": queries,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        yt_channels.run_job(job_id, queries),
        "youtube_channels", job_id, _ytchannels_rows))
    return {"job_id": job_id}


@app.get("/api/youtube-channels/results/{job_id}")
async def youtube_channels_results(job_id: str, limit: int = 4000):
    rows, _ = await _ytchannels_rows(job_id)
    return rows[:limit]


@app.post("/api/airbnb-reviews")
async def airbnb_reviews_start(req: AirbnbReviewsRequest):
    """Airbnb Reviews Scraper — reviews from airbnb.com room URLs or listing ids."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Airbnb room URL or listing id is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "airbnb_reviews", "queries": queries, "limit": req.limit,
        "sort": req.sort, "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        airbnb_rv.run_job(job_id, queries, req.limit, req.sort or ""),
        "airbnb_reviews", job_id, _airbnb_rows))
    return {"job_id": job_id}


@app.get("/api/airbnb-reviews/results/{job_id}")
async def airbnb_reviews_results(job_id: str, limit: int = 4000):
    rows, _ = await _airbnb_rows(job_id)
    return rows[:limit]


@app.post("/api/bbb-reviews")
async def bbb_reviews_start(req: BBBReviewsRequest):
    """BBB Business Reviews Scraper — customer reviews from a bbb.org business URL."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one bbb.org reviews/profile URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "bbb_reviews", "queries": queries, "limit": req.limit,
        "sort": req.sort, "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(_run_and_archive(
        bbb_reviews.run_job(job_id, queries, req.limit, req.sort),
        "bbb_reviews", job_id, _bbbreviews_rows))
    return {"job_id": job_id}


@app.get("/api/bbb-reviews/results/{job_id}")
async def bbb_reviews_results(job_id: str, limit: int = 4000):
    rows = [d async for d in bbbreviews.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/expedia")
async def expedia_start(req: ExpediaRequest):
    """Expedia Search Scraper — hotels from an expedia.com Hotel-Search URL (proxy-only)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Expedia Hotel-Search URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "expedia", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(expedia.run_job(job_id, queries, req.limit))
    return {"job_id": job_id}


@app.get("/api/expedia/results/{job_id}")
async def expedia_results_get(job_id: str, limit: int = 2000):
    rows = [d async for d in expedia_results.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/trustpilot")
async def trustpilot_start(req: TrustpilotRequest):
    """Trustpilot Scraper — companies from a trustpilot.com URL or company ID (proxy-only)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Trustpilot URL or company ID is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "trustpilot", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(trustpilot.run_job(job_id, queries, req.limit))
    return {"job_id": job_id}


@app.get("/api/trustpilot/results/{job_id}")
async def trustpilot_results_get(job_id: str, limit: int = 3000):
    rows = [d async for d in trustpilot_results.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/trustpilot-search")
async def trustpilot_search_start(req: TrustpilotSearchRequest):
    """Trustpilot Search Scraper — companies matching a keyword search (browser-rendered)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one search keyword is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "trustpilot_search", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(trustpilot_search.run_job(job_id, queries, req.limit))
    return {"job_id": job_id}


@app.get("/api/trustpilot-search/results/{job_id}")
async def trustpilot_search_results_get(job_id: str, limit: int = 3000):
    rows = [d async for d in trustpilot_search_results.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/trustpilot-reviews")
async def trustpilot_reviews_start(req: TrustpilotReviewsRequest):
    """Trustpilot Reviews Summary — reviews from a trustpilot.com /review/ page (browser, proxy-only)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Trustpilot /review/ URL or company id is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "trustpilot_reviews", "queries": queries, "limit": req.limit,
        "language": req.language, "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(trustpilot_reviews_mod.run_job(job_id, queries, req.limit, req.language))
    return {"job_id": job_id}


@app.get("/api/trustpilot-reviews/results/{job_id}")
async def trustpilot_reviews_results_get(job_id: str, limit: int = 5000):
    rows = [d async for d in trustpilot_reviews.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/trustpilot-monitoring")
async def trustpilot_monitoring_start(req: TrustpilotMonitorRequest):
    """Trustpilot Reviews Monitoring — create a recurring monitor + run the first scan now."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one Trustpilot /review/ URL or company id is required")
    if req.frequency not in monitor.FREQ_DAYS:
        raise HTTPException(400, f"frequency must be one of {list(monitor.FREQ_DAYS)}")
    res = await monitor.start_monitor(queries, req.frequency, req.email, req.threshold,
                                      req.language, req.limit)
    res["frequency"] = monitor.FREQ_LABEL[req.frequency]
    return res


@app.get("/api/trustpilot-monitoring/{monitor_id}")
async def trustpilot_monitoring_get(monitor_id: str):
    doc = await monitors.find_one({"monitor_id": monitor_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "monitor not found")
    return doc


@app.post("/api/hotels")
async def hotels_start(req: HotelsRequest):
    """Hotels Search Scraper — hotels from a hotels.com Hotel-Search URL (proxy-only)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one hotels.com Hotel-Search URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "hotels", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(hotels.run_job(job_id, queries, req.limit))
    return {"job_id": job_id}


@app.get("/api/hotels/results/{job_id}")
async def hotels_results_get(job_id: str, limit: int = 2000):
    rows = [d async for d in hotels_results.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/hotels-reviews")
async def hotels_reviews_start(req: HotelsReviewsRequest):
    """Hotels Reviews Scraper — guest reviews from a hotels.com hotel URL (proxy-only)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one hotels.com hotel URL is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "hotels_reviews", "queries": queries, "limit": req.limit,
        "sort": req.sort, "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(hotels_reviews_mod.run_job(job_id, queries, req.limit, req.sort))
    return {"job_id": job_id}


@app.get("/api/hotels-reviews/results/{job_id}")
async def hotels_reviews_results_get(job_id: str, limit: int = 3000):
    rows = [d async for d in hotels_reviews.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]


@app.post("/api/homedepot")
async def homedepot_start(req: HomeDepotRequest):
    """Home Depot Products Scraper — product listings from a homedepot.com URL (proxy-only)."""
    queries = [q.strip() for q in req.queries if q and q.strip()]
    if not queries:
        raise HTTPException(400, "at least one homedepot.com URL or keyword is required")
    job_id = uuid.uuid4().hex
    await jobs.insert_one({
        "job_id": job_id, "kind": "homedepot", "queries": queries, "limit": req.limit,
        "status": "running", "total_scraped": 0,
        "started_at": datetime.utcnow(), "finished_at": None,
    })
    asyncio.create_task(homedepot.run_job(job_id, queries, req.limit))
    return {"job_id": job_id}


@app.get("/api/homedepot/results/{job_id}")
async def homedepot_results_get(job_id: str, limit: int = 3000):
    rows = [d async for d in homedepot_results.find({"job_id": job_id}, {"_id": 0, "job_id": 0})]
    return rows[:limit]
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


# Catch-all (declared LAST so it never shadows /api/* or other routes): serve the SPA so a direct
# load / refresh of a per-service URL like /Emails-Contacts-Scraper works (client-side routing).
@app.get("/{full_path:path}", response_class=HTMLResponse)
async def spa(full_path: str):
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()
