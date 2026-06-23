from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "yellowpages"

    # Optional paid US proxy gateway for yellowpages.com (US site geo-blocks non-US IPs).
    # Empty = use the rotating free US-proxy pool in yp_us.py.
    PROXY_URL: str = ""

    # Optional ScraperAPI key (free tier ~1000 req/month, no card). When set, the G2 Reviews
    # scraper fetches through ScraperAPI's residential proxies + JS render, which clears G2's
    # DataDome bot-check (your own IP is never used). Empty = G2 stays on the free proxy pool.
    SCRAPER_API_KEY: str = ""

    # Airbnb Search: when a paid PROXY_URL is set, fetch via Airbnb's StaysSearch GraphQL API
    # (faster, clean JSON) instead of the headless HTML crawl. Set false to always use the crawl.
    AIRBNB_API: bool = True

    # Google Maps Data Scraper uses Google's official Places API (New). Needs a Google Cloud key
    # with the Places API (New) enabled + billing. Empty = the scraper returns 0 with a setup note.
    GOOGLE_MAPS_API_KEY: str = ""

    MAX_PAGES: int = 50
    MIN_DELAY: float = 1.0
    MAX_DELAY: float = 3.0
    REQUEST_TIMEOUT: int = 30

    # Website enrichment: visit each business's site to pull socials / website meta / emails
    # (the extra Outscraper-style columns). Set ENRICH=false to skip it (faster scrapes).
    ENRICH: bool = True
    ENRICH_TIMEOUT: int = 20       # per-site fetch timeout (seconds) — free proxies are slow,
                                   # an 8s cap timed out most website crawls -> empty enrichment
    ENRICH_CONCURRENCY: int = 12   # how many sites to crawl at once

    # Amenities live on each business's YP detail page, so capturing them needs one extra
    # fetch per business (slow on the US free-proxy pool). Set ENRICH_AMENITIES=false to skip.
    ENRICH_AMENITIES: bool = True

    # Reverse-phone owner name + address (free, via thatsthem.com). Off by default — those
    # whitepages name/address columns were removed from the export, so the lookups are skipped.
    ENRICH_PHONE_OWNER: bool = False
    PHONE_OWNER_ALL: bool = False

    # Fetch each site's about/team/contact page for contact title, phone, and employee count
    # (best-effort, low fill). Adds up to 2 extra fetches per site; set false to skip.
    ENRICH_TEAM: bool = True

    # SMTP for the Trustpilot Reviews Monitoring email reports. Empty SMTP_HOST = email disabled
    # (the monitor still scrapes and records its state; it just skips sending).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_FROM: str = ""            # falls back to SMTP_USER if empty
    MONITOR_TICK_SECONDS: int = 60  # how often the scheduler checks for due monitors

    # The Google Maps Reviews Scraper renders Maps in a headless browser and scrolls all reviews.
    # Google blocks free/datacenter IPs, so it routes through the paid residential PROXY_URL above
    # (the free pool can't reach Maps). No API key — only that proxy.


settings = Settings()
