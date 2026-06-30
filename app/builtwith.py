"""BuiltWith Scraper — a website's tech stack (CMS, JS frameworks, analytics, ecommerce, CDN, server).

Fetches the site's HTML + response headers THROUGH A PROXY (real IP never used) and matches a signature
table (the same idea as BuiltWith/Wappalyzer). General websites aren't anti-bot protected, so this works
on the free pool. Best-effort: a blocked/empty fetch yields empty tech fields. One row per domain.
"""
import asyncio
from datetime import datetime

from . import yp_us
from .config import settings
from .emails_contacts import _url

BW_COLUMNS = ["domain", "cms", "ecommerce", "javascript", "analytics", "cdn", "marketing",
              "server", "technologies"]

# (display name, category, needle, where) — where: "html" (lowercased body) or "header"
_SIGS = [
    # CMS
    ("WordPress", "cms", "wp-content", "html"), ("WordPress", "cms", "wp-includes", "html"),
    ("Shopify", "cms", "cdn.shopify.com", "html"), ("Wix", "cms", "static.wixstatic.com", "html"),
    ("Squarespace", "cms", "squarespace.com", "html"), ("Drupal", "cms", "drupal.settings", "html"),
    ("Joomla", "cms", "/media/jui/", "html"), ("Webflow", "cms", "webflow.com", "html"),
    ("Ghost", "cms", "content/themes", "html"), ("HubSpot CMS", "cms", "hs-scripts.com", "html"),
    # ecommerce (specific markers, not brand names that show up in marketing copy)
    ("Shopify", "ecommerce", "cdn.shopify.com", "html"),
    ("WooCommerce", "ecommerce", "/plugins/woocommerce", "html"),
    ("Magento", "ecommerce", "x-magento-init", "html"), ("Magento", "ecommerce", "/static/frontend/", "html"),
    ("BigCommerce", "ecommerce", "cdn11.bigcommerce.com", "html"),
    # JS frameworks
    ("Next.js", "javascript", "/_next/", "html"), ("Next.js", "javascript", "__next_data__", "html"),
    ("React", "javascript", "data-reactroot", "html"), ("Vue.js", "javascript", "data-v-", "html"),
    ("Nuxt.js", "javascript", "__nuxt__", "html"), ("Angular", "javascript", "ng-version", "html"),
    ("Gatsby", "javascript", "___gatsby", "html"), ("jQuery", "javascript", "jquery", "html"),
    ("Svelte", "javascript", "svelte-", "html"),
    # analytics
    ("Google Analytics", "analytics", "google-analytics.com", "html"),
    ("Google Analytics", "analytics", "gtag(", "html"),
    ("Google Tag Manager", "analytics", "googletagmanager.com", "html"),
    ("Facebook Pixel", "analytics", "connect.facebook.net", "html"),
    ("Hotjar", "analytics", "hotjar", "html"), ("Segment", "analytics", "cdn.segment.com", "html"),
    ("Mixpanel", "analytics", "mixpanel", "html"), ("Plausible", "analytics", "plausible.io", "html"),
    # marketing / chat
    ("HubSpot", "marketing", "hs-scripts.com", "html"), ("Intercom", "marketing", "intercom", "html"),
    ("Mailchimp", "marketing", "mailchimp", "html"), ("Drift", "marketing", "drift.com", "html"),
    ("Zendesk", "marketing", "zdassets.com", "html"),
    # CDN (header or html)
    ("Cloudflare", "cdn", "cf-ray", "header"), ("Cloudflare", "cdn", "cloudflare", "html"),
    ("Fastly", "cdn", "fastly", "header"), ("CloudFront", "cdn", "cloudfront.net", "html"),
    ("Akamai", "cdn", "akamai", "header"),
]


def _detect(html: str, headers: dict) -> dict:
    low = (html or "").lower()
    hdr = " ".join(f"{k}:{v}" for k, v in (headers or {}).items()).lower()
    cats: dict[str, list] = {"cms": [], "ecommerce": [], "javascript": [], "analytics": [],
                             "cdn": [], "marketing": []}
    for name, cat, needle, where in _SIGS:
        blob = hdr if where == "header" else low
        if needle in blob and name not in cats[cat]:
            cats[cat].append(name)
    return cats


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input domain -> one row of detected technologies grouped by category."""
    url = _url(query)
    if not url:
        return []
    dom = url.split("//", 1)[-1].split("/", 1)[0]
    row = {c: "" for c in BW_COLUMNS}
    row["domain"] = dom
    try:
        r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
    except Exception:
        r = None
    if r is None or r.status_code != 200 or not r.text:
        return [row]
    headers = dict(getattr(r, "headers", {}) or {})
    cats = _detect(r.text, headers)
    for c in ("cms", "ecommerce", "javascript", "analytics", "marketing"):
        row[c] = ", ".join(cats[c])
    row["cdn"] = ", ".join(cats["cdn"])
    row["server"] = headers.get("Server") or headers.get("server") or ""
    allt = [t for c in cats.values() for t in c]
    row["technologies"] = ", ".join(dict.fromkeys(allt))  # dedup, keep order
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, builtwith
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await builtwith.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
