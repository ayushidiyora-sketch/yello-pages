"""Shared TikTok helpers — fetch a TikTok page and pull out its embedded rehydration JSON.

TikTok embeds page state in a `__UNIVERSAL_DATA_FOR_REHYDRATION__` script (older pages used
`SIGI_STATE`). A VIDEO page embeds the full video detail, so that scraper works from the HTML. Hashtag
/ search / comment LISTS are NOT in the HTML — TikTok lazy-loads them through a request-signed API, so
those scrapers can only return a clear "needs TikTok's signed API" status. All fetches go THROUGH A
PROXY (real IP never used); TikTok is IP-sensitive, so fetches are retried across rotating IPs.
"""
import json
import re

from . import yp_us
from .config import settings

_UNIVERSAL = re.compile(r'__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)
_SIGI = re.compile(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', re.S)

API_NOTE = ("TikTok loads this list via its request-signed API (not in page HTML) — not available "
            "without a TikTok API/signing service; even residential proxies need the signature.")


def fetch_scope(url: str, tries: int = 6):
    """Return (__DEFAULT_SCOPE__ dict, status_code). Retries across rotating proxy IPs (TikTok serves
    a captcha/blank on some IPs). SIGI_STATE is returned under the 'SIGI' key when present."""
    last = None
    for _ in range(tries):
        try:
            r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)
        except Exception:
            continue
        if r is None:
            continue
        last = r.status_code
        if r.status_code != 200:
            continue
        m = _UNIVERSAL.search(r.text)
        if m:
            try:
                return json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {}), 200
            except ValueError:
                pass
        m = _SIGI.search(r.text)
        if m:
            try:
                return {"SIGI": json.loads(m.group(1))}, 200
            except ValueError:
                pass
        return {}, 200  # 200 but no data blob (interstitial) -> retry
    return {}, last


def video_url(q: str) -> str:
    """A tiktok.com video URL as-is; a bare numeric id -> a /video/ URL (username unknown)."""
    q = (q or "").strip()
    if q.lower().startswith("http"):
        return q
    if q.isdigit():
        return f"https://www.tiktok.com/@/video/{q}"
    return q
