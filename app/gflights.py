"""Google Search Flights Scraper — flight results from Google Flights.

Reads Google's own internal data (the `AF_initDataCallback({…data:[…]})` payload its scripts consume)
from the Google Flights results page — NOT the rendered HTML DOM — the same approach as the Search
Shopping / Jobs scrapers. PROXY-ONLY, paid/residential REQUIRED (Google blocks free/datacenter IPs and
loads flight results via an internal RPC): set PROXY_URL in .env; the free pool returns a clear
"blocked" note (the real IP is never used). Flight field-mapping is best-effort and finalizes against a
real proxied response.

Input = an "ORIGIN,DESTINATION" pair (IATA codes, e.g. "EWR,LAX") plus optional departure/return
dates. The frontend builds these pairs from either the route cards or the plain-queries textarea.
"""
import asyncio
import re
from datetime import datetime
from urllib.parse import quote_plus

from . import yp_us
from .gsjobs import _balanced_array          # string-aware nested-array JSON parser (reused)
from .scraper import STOP_REQUESTS

FLIGHTS_URL = "https://www.google.com/travel/flights"

GFL_COLUMNS = ["origin", "destination", "airline", "departure_time", "arrival_time", "duration",
               "stops", "price", "departure_date", "return_date", "query"]

_BLOCK = ("unusual traffic", "/sorry/", "captcha", "recaptcha", "before you continue",
          "enablejs", "not a robot")
_AF_DATA = re.compile(r"AF_initDataCallback\(\{[^{}]*?data:\s*(\[)", re.DOTALL)
_PRICE = re.compile(r"^(?:[$₹€£¥]|Rs\.?|US\$|AED|INR)\s?\d[\d,]*$")
_TIME = re.compile(r"^\d{1,2}:\d{2}\s?(?:AM|PM)?$", re.I)
_DURATION = re.compile(r"\b\d+\s*hr\b", re.I)
_STOPS = re.compile(r"^(?:Nonstop|\d+\s*stop(?:s)?)$", re.I)


def _route(query: str) -> tuple[str, str]:
    parts = [p.strip().upper() for p in re.split(r"[,\->/]+", (query or "")) if p.strip()]
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def _flight_url(origin: str, destination: str, depart: str, ret: str,
                language: str, region: str) -> str:
    q = f"Flights from {origin} to {destination}"
    if depart:
        q += f" on {depart}"
    if ret:
        q += f" returning {ret}"
    params = (f"q={quote_plus(q)}&curr=USD&hl={quote_plus(language or 'en')}"
              f"&gl={quote_plus((region or 'us').lower())}")
    return f"{FLIGHTS_URL}?{params}"


def _flat_strings(node, out, depth=0):
    if depth > 9:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            _flat_strings(x, out, depth + 1)


def _collect_flights(node, rows, seen, ctx, depth=0):
    """A flight ENTRY is a subtree whose flat strings hold a price, a duration, and >=2 clock times
    (depart + arrive). Best-effort — finalize the indices against a real proxied response."""
    if depth > 70 or not isinstance(node, list):
        return
    sub = []
    _flat_strings(node, sub)
    price = next((x for x in sub if _PRICE.match(x.strip())), "")
    duration = next((x for x in sub if _DURATION.search(x.strip())), "")
    times = [x for x in sub if _TIME.match(x.strip())]
    # only treat THIS node as one flight if it's a tight subtree (not the whole results array)
    if price and duration and len(times) >= 2 and len(sub) <= 40:
        key = (times[0], times[1], price, duration)
        if key not in seen:
            seen.add(key)
            stops = next((x for x in sub if _STOPS.match(x.strip())), "")
            # airline: a human-readable string that isn't a price/time/duration/stops/airport code
            airline = next((x for x in sub if 3 <= len(x) <= 40 and " " not in x[:1]
                            and not _PRICE.match(x.strip()) and not _TIME.match(x.strip())
                            and not _DURATION.search(x) and not _STOPS.match(x.strip())
                            and not re.fullmatch(r"[A-Z]{3}", x.strip())
                            and any(c.isalpha() for c in x)), "")
            rows.append({
                "origin": ctx["origin"], "destination": ctx["destination"],
                "airline": airline, "departure_time": times[0], "arrival_time": times[1],
                "duration": duration, "stops": stops, "price": price.strip(),
                "departure_date": ctx["depart"], "return_date": ctx["ret"], "query": ctx["query"],
            })
        return
    for x in node:
        _collect_flights(x, rows, seen, ctx, depth + 1)


def _internal_flights(html_text: str, ctx: dict) -> list[dict]:
    rows, seen = [], set()
    for m in _AF_DATA.finditer(html_text):
        arr = _balanced_array(html_text, m.start(1))
        if arr is not None:
            _collect_flights(arr, rows, seen, ctx)
    return rows


def search_sync(query: str, depart: str = "", ret: str = "", limit: int | None = None,
                language: str = "en", region: str = "us", job_id: str | None = None) -> list[dict]:
    origin, destination = _route(query)
    if not origin or not destination:
        raise RuntimeError(f"Could not read an origin/destination pair from '{query}' "
                           "(use 'EWR,LAX').")
    ctx = {"origin": origin, "destination": destination, "depart": depart, "ret": ret,
           "query": query}
    headers = {"Accept-Language": f"{(language or 'en')}-{(region or 'us').upper()},"
                                  f"{(language or 'en')};q=0.9"}
    r = yp_us.pooled_get(_flight_url(origin, destination, depart, ret, language, region),
                         {}, timeout=25, headers=headers)
    if r is None or r.status_code != 200:
        raise RuntimeError("Google Flights needs a paid residential PROXY_URL in .env — Google blocks "
                           "free/datacenter IPs (no real IP is used).")
    low = (r.text or "").lower()
    if any(b in low for b in _BLOCK):
        raise RuntimeError("Google blocked this request (CAPTCHA / unusual traffic). Use a cleaner "
                           "residential PROXY_URL.")
    rows = _internal_flights(r.text, ctx)
    return rows[:limit] if limit else rows


async def search(query: str, depart: str = "", ret: str = "", limit: int | None = None,
                 language: str = "en", region: str = "us", job_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, depart, ret, limit, language, region, job_id)


async def run_job(job_id: str, queries: list[str], depart: str, ret: str, limit: int | None,
                  language: str, region: str) -> None:
    from .db import jobs, gflights_results
    total = 0
    last_err = ""
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            try:
                rows = await search(q, depart, ret, limit, language, region, job_id)
            except Exception as qe:           # a blocked/invalid query shouldn't fail the whole job
                last_err = str(qe)
                rows = []
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gflights_results.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        stopped = job_id in STOP_REQUESTS
        STOP_REQUESTS.discard(job_id)
        done = {"status": "stopped" if stopped else "done", "total_scraped": total,
                "finished_at": datetime.utcnow()}
        if not total and not stopped:
            done["note"] = last_err or (
                "Google Flights returned 0 results — Google blocks free/datacenter IPs and loads "
                "flights via an internal RPC. Set a paid residential PROXY_URL in .env for results "
                "(the real IP is never used).")
        await jobs.update_one({"job_id": job_id}, {"$set": done})
    except Exception as e:
        STOP_REQUESTS.discard(job_id)
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
