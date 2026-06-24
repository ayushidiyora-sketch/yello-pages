"""Google Search Jobs Scraper — job listings for a query via Google Jobs (ibp=htl;jobs), proxy-only.

Fetches Google's Jobs results (`/search?q=<query>&ibp=htl;jobs`) through the proxy pool and reads the
embedded job data — JSON-LD `JobPosting` items first, then a best-effort DOM card parse. Google
blocks free/datacenter IPs, so a residential PROXY_URL is needed for live results (the free pool
returns a clear "blocked" error — the real IP is never used). The card parser is best-effort and may
need one live-tuning pass against a real proxied response.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import yp_us
from .scraper import STOP_REQUESTS

GOOGLE_S = "https://www.google.com/search"

GSJ_COLUMNS = ["query", "title", "company", "location", "via", "posted", "salary", "link",
               "description"]

_BLOCK = ("unusual traffic", "/sorry/", "captcha", "recaptcha", "before you continue",
          "enablejs", "not a robot")

# Google embeds the Jobs widget's own data (its internal API payload) in AF_initDataCallback({...}).
_AF_DATA = re.compile(r"AF_initDataCallback\(\{[^{}]*?data:\s*(\[)", re.DOTALL)
_URLISH = re.compile(r"^https?://")


def _txt(v) -> str:
    if isinstance(v, dict):
        for k in ("name", "value", "addressLocality", "@value"):
            if v.get(k):
                return str(v[k])
        return ""
    if isinstance(v, list):
        return ", ".join(_txt(x) for x in v if x)
    return str(v or "")


def _balanced_array(s: str, start: int):
    """Parse the JSON array starting at index `start` ('['), string-aware; returns the Python list."""
    depth, instr, esc = 0, False, False
    for j in range(start, len(s)):
        c = s[j]
        if instr:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                instr = False
        elif c == '"':
            instr = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:j + 1])
                except Exception:
                    return None
    return None


def _collect_jobs(node, rows, seen, query, depth=0):
    """Walk Google's internal jobs payload and pull job entries: a node whose subtree holds a
    title-like string plus an apply/job URL. Best-effort — finalize indices on a real response."""
    if depth > 60 or not isinstance(node, list):
        return
    # gather shallow strings of this node + one level down
    flat = []
    for x in node:
        if isinstance(x, str):
            flat.append(x)
        elif isinstance(x, list):
            for y in x:
                if isinstance(y, str):
                    flat.append(y)
    url = next((x for x in flat if _URLISH.match(x) and ("job" in x.lower() or "apply" in x.lower()
               or "google.com/search" in x)), "")
    titles = [x for x in flat if 3 <= len(x) <= 160 and not _URLISH.match(x) and " " in x
              and not x.startswith("/") and "{" not in x]
    if url and titles:
        title = titles[0]
        key = title[:100]
        if key not in seen:
            seen.add(key)
            others = [x for x in titles[1:] if x != title]
            rows.append({
                "query": query, "title": title,
                "company": others[0] if others else "",
                "location": others[1] if len(others) > 1 else "",
                "via": "", "posted": "", "salary": "",
                "link": url, "description": "",
            })
        return
    for x in node:
        _collect_jobs(x, rows, seen, query, depth + 1)


def _internal_jobs(html_text: str, query: str) -> list[dict]:
    """PRIMARY: read jobs straight from Google's embedded internal data (AF_initDataCallback)."""
    rows, seen = [], set()
    for m in _AF_DATA.finditer(html_text):
        arr = _balanced_array(html_text, m.start(1))
        if arr is not None:
            _collect_jobs(arr, rows, seen, query)
    return rows


def _jsonld_jobs(soup, query: str) -> list[dict]:
    """Pull schema.org JobPosting items out of <script type=application/ld+json> blocks."""
    rows = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict) or it.get("@type") not in ("JobPosting", ["JobPosting"]):
                continue
            org = it.get("hiringOrganization") or {}
            loc = it.get("jobLocation") or {}
            addr = (loc[0] if isinstance(loc, list) and loc else loc)
            addr = addr.get("address", addr) if isinstance(addr, dict) else addr
            sal = it.get("baseSalary") or {}
            rows.append({
                "query": query,
                "title": _txt(it.get("title")),
                "company": _txt(org.get("name") if isinstance(org, dict) else org),
                "location": _txt(addr),
                "via": _txt(it.get("identifier", {}).get("name") if isinstance(it.get("identifier"), dict) else ""),
                "posted": _txt(it.get("datePosted"))[:10],
                "salary": _txt(sal.get("value") if isinstance(sal, dict) else sal),
                "link": _txt(it.get("url") or it.get("sameAs")),
                "description": re.sub(r"<[^>]+>", " ", _txt(it.get("description")))[:500].strip(),
            })
    return rows


def _dom_jobs(soup, query: str) -> list[dict]:
    """Best-effort fallback: read Google Jobs cards from the rendered widget (selectors are
    obfuscated/locale-dependent — finalize against a real proxied response)."""
    rows, seen = [], set()
    for li in soup.select("li, div.iFjolb, div.PwjeAc"):
        title_el = li.select_one("div.BjJfJf, div.tNxQIb, [role='heading']")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        if not title or title in seen:
            continue
        meta = li.select_one("div.vNEEBe, div.wHYlTd")
        comp_loc = (meta.get_text(" · ", strip=True) if meta else "").split("·")
        seen.add(title)
        rows.append({
            "query": query, "title": title,
            "company": comp_loc[0].strip() if comp_loc else "",
            "location": comp_loc[1].strip() if len(comp_loc) > 1 else "",
            "via": "", "posted": "", "salary": "",
            "link": "", "description": "",
        })
    return rows


def search_sync(query: str, pages: int = 1, language: str = "en", region: str = "us",
                job_id: str | None = None) -> list[dict]:
    headers = {"Accept-Language": f"{(language or 'en')}-{(region or 'us').upper()},"
                                  f"{(language or 'en')};q=0.9"}
    rows, seen = [], set()
    for p in range(max(1, int(pages or 1))):
        if job_id and job_id in STOP_REQUESTS:
            break
        params = {"q": query, "ibp": "htl;jobs", "hl": language or "en",
                  "gl": (region or "us").lower(), "start": str(p * 10)}
        r = yp_us.pooled_get(GOOGLE_S, params, timeout=20, headers=headers)
        if r is None or r.status_code != 200:
            if rows:
                break
            raise RuntimeError("Google Jobs needs a paid residential PROXY_URL in .env — Google "
                               "blocks free/datacenter IPs (no real IP is used).")
        low = (r.text or "").lower()
        if any(b in low for b in _BLOCK):
            if rows:
                break
            raise RuntimeError("Google blocked this request (CAPTCHA / unusual traffic). Use a "
                               "cleaner residential PROXY_URL.")
        # PRIMARY: Google's internal jobs API data (embedded); fallbacks: JSON-LD, then DOM cards
        batch = _internal_jobs(r.text, query)
        if not batch:
            soup = BeautifulSoup(r.text, "lxml")
            batch = _jsonld_jobs(soup, query) or _dom_jobs(soup, query)
        new = [j for j in batch if j["title"] not in seen]
        for j in new:
            seen.add(j["title"])
        rows += new
        if not new:
            break
    return rows


async def search(query: str, pages: int = 1, language: str = "en", region: str = "us",
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, pages, language, region, job_id)


async def run_job(job_id: str, queries: list[str], pages: int, language: str, region: str) -> None:
    from .db import jobs, gsjobs_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, pages, language, region, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gsjobs_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "stopped" if stopped else "done", "total_scraped": total,
            "finished_at": datetime.utcnow()}})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
