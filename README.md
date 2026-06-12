# YellowPages.com (US) Scraper

FastAPI + MongoDB scraper for **yellowpages.com** with live progress, record limit,
Stop button, paginated card UI, and JSON export.

## Setup (Windows PowerShell)
```powershell
cd yellowpages-scraper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then edit .env if you have a paid US proxy
```

## Run
```powershell
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

Pick **Region = US**, enter a search + location (e.g. `Dentists` / `New York, NY`),
optionally a **Limit**, then click **Start**. Watch the live count, **Stop** anytime,
and **Download JSON** when done. Results show as paginated cards (20/page). Data is
also stored in MongoDB (db: `yellowpages`, collections: `jobs` + `businesses`).

## How it works / notes
1. **Geo-block**: yellowpages.com sits behind Cloudflare and blocks non-US IPs. The
   scraper uses `curl_cffi` (real Chrome TLS fingerprint) routed through a **US proxy**.
2. **Proxy**: by default it auto-fetches + rotates a pool of free US proxies
   (`app/yp_us.py`). These are flaky/slow — for reliable/large pulls set a paid US
   `PROXY_URL` in `.env`.
3. **Selectors**: if results show 0, the markup may have changed — update the selectors
   in `parse_us_cards()` in `app/yp_us.py`.
4. **Legal**: scrape responsibly; respect the site's terms / robots.txt and rate limits.
