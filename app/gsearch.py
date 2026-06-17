"""Google Search Scraper backend — powered by DuckDuckGo.

Real Google blocks every free scraping route (429 / "enable JS" / /sorry CAPTCHA), so this
fetches DuckDuckGo's HTML endpoint instead — through the proxy pool (`yp_us.pooled_get`), so the
real IP is never used. Returns rows shaped like Outscraper's Google Search output:
query, link, title, description, question, position, type.
"""
import asyncio
from urllib.parse import unquote, urlparse, parse_qs

from bs4 import BeautifulSoup

from . import yp_us

DDG = "https://html.duckduckgo.com/html/"
# Outscraper-style date range -> DuckDuckGo `df` param
DATE_MAP = {"": "", "any": "", "any time": "", "day": "d", "past day": "d", "week": "w",
            "past week": "w", "month": "m", "past month": "m", "year": "y", "past year": "y"}


def _real_url(href: str) -> str:
    """DDG wraps links as //duckduckgo.com/l/?uddg=<encoded-url> — decode to the real URL."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        q = parse_qs(urlparse(href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href


# DuckDuckGo locale codes are fixed `country-language` pairs (not free-form).
_VALID_KL = {
    "us-en", "uk-en", "ca-en", "ca-fr", "au-en", "in-en", "ie-en", "nz-en", "za-en", "sg-en",
    "fr-fr", "be-fr", "ch-fr", "de-de", "at-de", "ch-de", "es-es", "mx-es", "ar-es", "cl-es",
    "it-it", "nl-nl", "be-nl", "pt-pt", "br-pt",
}
_LANG_HOME = {"en": "us-en", "fr": "fr-fr", "de": "de-de", "es": "es-es",
              "it": "it-it", "pt": "pt-pt", "nl": "nl-nl"}


def _kl(region: str, language: str) -> str:
    """Pick a valid DDG locale honoring the chosen region+language. If the exact pair isn't a
    real DDG locale (e.g. India+German), prefer the LANGUAGE's home locale so the selected
    language still applies; else fall back to the region in English."""
    r = (region or "us").lower()[:2]
    l = (language or "en").lower()[:2]
    combo = f"{r}-{l}"
    if combo in _VALID_KL:
        return combo                       # both region and language honored
    if l in _LANG_HOME:
        return _LANG_HOME[l]               # honor the chosen language
    if f"{r}-en" in _VALID_KL:
        return f"{r}-en"                    # honor the region (English)
    return "wt-wt"


TRANSLATE = "https://translate.googleapis.com/translate_a/single"


def _translate_one(text: str, target: str) -> str:
    """Translate a single string into `target`. Returns the original on any failure."""
    text = (text or "").strip()
    if not text:
        return text
    try:
        r = yp_us.pooled_get(TRANSLATE,
                             {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
                             timeout=20)
        data = r.json()
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        return out or text
    except Exception:
        return text


def _translate_rows(rows: list[dict], target: str) -> None:
    """Translate each result's title + description into `target` (in place), via the free Google
    Translate endpoint through the proxy pool. One request per row (title+description joined by a
    newline, split back) keeps alignment exact. Skips English (the base) and related rows."""
    if not target or target.lower().startswith("en"):
        return
    for r in rows:
        if r.get("type") == "related":
            continue
        title = (r.get("title") or "").replace("\n", " ").strip()
        desc = (r.get("description") or "").replace("\n", " ").strip()
        if not title and not desc:
            continue
        out = _translate_one(title + "\n" + desc, target) if (title and desc) else None
        parts = out.split("\n") if out else []
        if len(parts) == 2:
            r["title"], r["description"] = parts[0].strip(), parts[1].strip()
        else:
            # newline didn't survive (or only one field) -> translate each field on its own
            if title:
                r["title"] = _translate_one(title, target)
            if desc:
                r["description"] = _translate_one(desc, target)


def _parse(html: str, query: str, start_pos: int) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    rows, pos = [], start_pos
    for c in soup.select("div.result"):
        a = c.select_one("a.result__a")
        if not a:
            continue
        cls = c.get("class") or []
        rtype = "ad" if "result--ad" in cls else ("news" if "result--news" in cls else "organic")
        sn = c.select_one(".result__snippet")
        pos += 1
        rows.append({
            "query": query,
            "link": _real_url(a.get("href")),
            "title": a.get_text(" ", strip=True),
            "description": sn.get_text(" ", strip=True) if sn else "",
            "question": "",
            "position": pos,
            "type": rtype,
        })
    # DuckDuckGo has no "People also ask"; surface related searches in the `question` field
    for rel in soup.select(".related-searches__link, a.js-related-search-link"):
        t = rel.get_text(" ", strip=True)
        if t:
            rows.append({"query": query, "link": "", "title": "", "description": "",
                         "question": t, "position": 0, "type": "related"})
    return rows


MAX_PAGES = 15  # safety cap when limit is blank ("all")


def search_sync(query: str, limit: int | None = None, date_range: str = "",
                region: str = "us", language: str = "en") -> list[dict]:
    """Scrape DuckDuckGo results for one query via the proxy pool. `limit` caps the number of
    result rows (blank/None = fetch all available, up to MAX_PAGES); e.g. limit=10 -> 10 rows."""
    query = (query or "").strip()
    if not query:
        return []
    df = DATE_MAP.get((date_range or "").strip().lower(), "")
    kl = _kl(region, language)
    rows, pos, s = [], 0, 0
    for _page in range(MAX_PAGES):
        params = {"q": query, "kl": kl}
        if df:
            params["df"] = df
        if s:
            params["s"] = str(s)
        r = None
        for _attempt in range(4):  # free proxies are flaky — retry a few before giving up
            resp = yp_us.pooled_get(DDG, params, timeout=20)
            if resp is not None and resp.status_code == 200 and "result__a" in resp.text:
                r = resp
                break
        if r is None:
            break
        page = _parse(r.text, query, pos)
        organic = [x for x in page if x["type"] in ("organic", "ad", "news")]
        rows += page
        if not organic:
            break
        pos = max((x["position"] for x in organic), default=pos)
        s += len(organic)
        if limit and sum(1 for x in rows if x["type"] != "related") >= limit:
            break
    # trim organic rows to exactly `limit` (keep related-search rows as extras)
    if limit:
        out, n = [], 0
        for x in rows:
            if x["type"] == "related":
                out.append(x)
            elif n < limit:
                out.append(x)
                n += 1
        rows = out
    # translate title + description into the selected language (free Google Translate via proxy)
    _translate_rows(rows, language)
    return rows


async def search(query: str, limit: int | None = None, date_range: str = "",
                 region: str = "us", language: str = "en") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, date_range, region, language)


async def run_job(job_id: str, queries: list[str], limit: int | None, date_range: str,
                  region: str, language: str) -> None:
    """Background task: scrape each query (via the proxy pool) and store the result rows."""
    from datetime import datetime
    from .db import jobs, gresults
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, date_range, region, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gresults.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
