from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "yellowpages"

    # Optional paid US proxy gateway for yellowpages.com (US site geo-blocks non-US IPs).
    # Empty = use the rotating free US-proxy pool in yp_us.py.
    PROXY_URL: str = ""

    # Optional rotating paid-proxy pool. A path to a proxies file (one `ip:port:user:pass` or
    # `http://user:pass@ip:port` per line), or an inline whitespace/comma list. When set, pooled_get
    # rotates through it and skips rate-limited (429) / blocked IPs — useful for rate-limited services
    # (e.g. Google Search verticals) that work on datacenter IPs but throttle a single one.
    PROXY_LIST: str = ""

    # Optional file of rotating proxies (one per line: IP:PORT:USER:PASS or a full http:// URL).
    # When PROXY_URL is empty and this file exists, the Google Trends scraper rotates through these
    # IPs (skipping any currently rate-limited / 429'd one and pinning a working one) instead of the
    # free pool. Useful for rate-limit-only sites (Trends) where datacenter IPs work if not 429'd.
    # The real IP is never used. Empty/missing file = fall back to the free pool.
    PROXY_LIST_FILE: str = "proxies.txt"

    # Walmart-ONLY proxy. Used solely by the Walmart Products scraper; no other service reads it.
    # Empty = Walmart falls back to PROXY_URL (if set) then the free pool. Lets you point Walmart at a
    # dedicated proxy without affecting any other scraper.
    WALMART_PROXY_URL: str = ""

    # Trustpilot-ONLY proxy. Used solely by the Trustpilot scraper; no other service reads it. Trustpilot
    # is now PROXY-ONLY (never the real IP): if neither this nor PROXY_URL is set, it returns a clear
    # "blocked" error instead of rendering on the real IP. A residential proxy is needed for data
    # (Cloudflare blocks datacenter IPs).
    TRUSTPILOT_PROXY_URL: str = ""

    # BestBuy-ONLY US proxy for the headless "all products" render; no other service reads it.
    # Empty = BestBuy falls back to PROXY_URL then the free US pool. BestBuy needs a US IP and the
    # headless render is steadier on a fixed paid US proxy.
    BESTBUY_PROXY_URL: str = ""

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

    # LinkedIn Posts Scraper: company posts sit behind LinkedIn's login wall (no public source), so
    # they're read through LinkedIn's authenticated voyager API. Set LINKEDIN_COOKIE to your account's
    # `li_at` cookie value (best paired with a residential PROXY_URL). Empty = the scraper returns 0
    # with a clear "login required" note. The real IP is never used for the fetch.
    LINKEDIN_COOKIE: str = ""

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

    # Resend (https://resend.com) — simplest email path for the monitoring reports: just an API key,
    # no SMTP host/port. When RESEND_API_KEY is set it takes priority over SMTP. RESEND_FROM must be
    # a sender on a domain you've verified in Resend; the shared sandbox "onboarding@resend.dev" works
    # with no verification but can only deliver to your own Resend account email.
    RESEND_API_KEY: str = ""
    RESEND_FROM: str = "Live Scraper <onboarding@resend.dev>"

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
