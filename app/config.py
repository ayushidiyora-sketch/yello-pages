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


settings = Settings()
