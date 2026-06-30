"""Apollo Scraper — people / companies from an app.apollo.io search URL (auth via your cookies).

Apollo is login-walled, so it's read through Apollo's own internal search API using YOUR session
cookies (pasted in JSON — the "Cookie Editor" export format). Each search URL's filters (from the
`#/people?…` or `#/companies?…` hash) are converted to the API body and POSTed to
  /api/v1/mixed_people/search   or   /api/v1/mixed_companies/search
which returns the rows the UI shows. Paginated up to `limit`.

PROXY-ONLY: the request goes through a proxy (paid PROXY_URL if set, else the free pool); the real IP
is never used. Without cookies it returns a clear "cookies required" message. NOTE: Apollo's API is
internal and rate-limits/credits-gates aggressively — field mapping is best-effort and may need a
live-tuning pass against a real authenticated response.
"""
import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qsl

from curl_cffi import requests as cffi

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

APOLLO_COLUMNS = ["query", "type", "name", "title", "email", "phone", "company", "domain",
                  "industry", "employees", "location", "linkedin_url"]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_PER_PAGE = 100
_ENDPOINT = {"people": "mixed_people", "companies": "mixed_companies"}


def _camel_to_snake(s: str) -> str:
    # split before an uppercase letter only — keep version digits attached (V2 -> _v2, not _v_2)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def _parse_url(url: str):
    """(kind, params) from an app.apollo.io search URL. kind = 'people' | 'companies'; params are the
    hash query (camelCase + []) converted to the API's snake_case body shape."""
    frag = urlparse(url).fragment or url
    kind = "companies" if ("/companies" in frag or "/accounts" in frag) else "people"
    query = frag.split("?", 1)[1] if "?" in frag else ""
    params: dict = {}
    for k, v in parse_qsl(query, keep_blank_values=True):
        is_list = k.endswith("[]")
        key = _camel_to_snake(k[:-2] if is_list else k)
        if is_list:
            params.setdefault(key, []).append(v)
        else:
            params[key] = v
    return kind, params


def _cookie_header(cookies_json: str):
    """Parse the pasted Cookie-Editor JSON -> (cookie header string, csrf token if present)."""
    arr = json.loads(cookies_json)
    if isinstance(arr, dict):
        arr = [{"name": k, "value": v} for k, v in arr.items()]
    header = "; ".join(f"{c['name']}={c['value']}" for c in arr if c.get("name"))
    csrf = ""
    for c in arr:
        if str(c.get("name", "")).lower() in ("x-csrf-token", "csrf-token", "x-csrf_token"):
            csrf = c.get("value", "")
    return header, csrf


def _proxies():
    px = settings.PROXY_URL.strip()
    if px:
        return {"http": px, "https": px}
    try:
        yp_us.ensure_pool({"search_terms": "x", "geo_location_terms": "y", "page": "1"}, 3)
        with yp_us._LOCK:
            warm = list(yp_us._GOOD)
        px = (warm or yp_us._fetch_candidates() or [None])[0]
    except Exception:
        px = None
    return {"http": px, "https": px} if px else None


def _csrf_from_home(cookie_header: str, proxies) -> str:
    try:
        r = cffi.get("https://app.apollo.io/", impersonate="chrome",
                     headers={"cookie": cookie_header, "user-agent": _UA}, proxies=proxies,
                     timeout=20, verify=False)
        m = (re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
             or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', r.text)
             or re.search(r'"X-CSRF-Token"\s*:\s*"([^"]+)"', r.text))
        return m.group(1) if m else ""
    except Exception:
        return ""


def _loc(d: dict) -> str:
    return ", ".join(x for x in (d.get("city"), d.get("state"), d.get("country")) if x)


def _row_person(query: str, p: dict) -> dict:
    org = p.get("organization") or p.get("account") or {}
    name = p.get("name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
    phones = p.get("phone_numbers") or []
    phone = phones[0].get("sanitized_number") if phones and isinstance(phones[0], dict) else (p.get("sanitized_phone") or "")
    return {
        "query": query, "type": "person", "name": name, "title": p.get("title") or "",
        "email": p.get("email") or "", "phone": phone or "",
        "company": org.get("name") or "", "domain": org.get("primary_domain") or org.get("website_url") or "",
        "industry": org.get("industry") or "", "employees": org.get("estimated_num_employees") or "",
        "location": _loc(p), "linkedin_url": p.get("linkedin_url") or "",
    }


def _row_company(query: str, o: dict) -> dict:
    return {
        "query": query, "type": "company", "name": o.get("name") or "", "title": "",
        "email": "", "phone": (o.get("phone") or o.get("sanitized_phone") or ""),
        "company": o.get("name") or "", "domain": o.get("primary_domain") or o.get("website_url") or "",
        "industry": o.get("industry") or "", "employees": o.get("estimated_num_employees") or "",
        "location": _loc(o), "linkedin_url": o.get("linkedin_url") or "",
    }


def search_sync(query: str, cookies_json: str, limit: int | None = None,
                job_id: str | None = None) -> list[dict]:
    if not (cookies_json or "").strip():
        raise RuntimeError("Apollo is login-walled — paste your Apollo cookies (JSON) to enable this "
                           "scraper. Use the Cookie-Editor extension on app.apollo.io and export all cookies.")
    try:
        cookie_header, csrf = _cookie_header(cookies_json)
    except Exception:
        raise RuntimeError("Could not parse the Apollo cookies — paste the Cookie-Editor JSON export "
                           "(an array of {name, value, domain} objects).")
    kind, base_params = _parse_url(query)
    proxies = _proxies()
    if proxies is None:
        raise RuntimeError("No proxy available to reach Apollo (set a PROXY_URL). Real IP unused.")
    if not csrf:
        csrf = _csrf_from_home(cookie_header, proxies)
    headers = {"content-type": "application/json", "user-agent": _UA, "cookie": cookie_header,
               "x-csrf-token": csrf, "origin": "https://app.apollo.io",
               "referer": "https://app.apollo.io/"}
    api = f"https://app.apollo.io/api/v1/{_ENDPOINT[kind]}/search"

    rows: list[dict] = []
    page = int(base_params.get("page") or 1)
    for _ in range(50):
        if job_id and job_id in STOP_REQUESTS:
            break
        body = {**base_params, "page": page, "per_page": _PER_PAGE}
        try:
            r = cffi.post(api, impersonate="chrome", json=body, headers=headers, proxies=proxies,
                          timeout=settings.REQUEST_TIMEOUT, verify=False)
        except Exception:
            break
        if r.status_code != 200:
            if page == (int(base_params.get("page") or 1)):
                raise RuntimeError(f"Apollo API returned {r.status_code} — the cookies may be expired "
                                   "or out of credits. Re-export fresh Apollo cookies.")
            break
        try:
            data = r.json()
        except Exception:
            break
        items = (data.get("people") or data.get("contacts")
                 or data.get("organizations") or data.get("accounts") or [])
        if not items:
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            rows.append(_row_company(query, it) if kind == "companies" else _row_person(query, it))
        if limit and len(rows) >= limit:
            break
        total_pages = ((data.get("pagination") or {}).get("total_pages")) or 0
        page += 1
        if total_pages and page > total_pages:
            break
    return rows[:limit] if limit else rows


async def search(query: str, cookies_json: str, limit: int | None = None,
                 job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, cookies_json, limit, job_id)


async def run_job(job_id: str, queries: list[str], cookies_json: str, limit: int | None) -> None:
    from .db import jobs, apollo_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, cookies_json, limit, job_id)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await apollo_results.insert_many(rows)
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
