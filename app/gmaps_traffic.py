"""Google Maps Traffic Scraper — directions (travel time + distance) between two points.

For each Start→Stop pair it renders the Google Maps directions page (`/maps/dir/?api=1`) in a
headless browser through the proxy and reads the route's duration + distance from the panel — the
same free, no-API, Playwright+proxy approach as the other Google Maps scrapers. The time-frame +
interval produce one sampled row per time slot.

PROXY-ONLY, paid/residential REQUIRED (Google blocks free/datacenter IPs): set PROXY_URL in .env.
HONEST LIMIT: the free Google Maps web route gives the **current** traffic duration; per-interval
*future* departure traffic needs the paid Directions API — so every sample in a frame carries the
current duration with its own `sample_time`. Selectors are obfuscated/locale-dependent and may need
one live-tuning pass.
"""
import asyncio
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from .config import settings
from .gmaps_reviews import _run, _proxy_opts, _UA, _accept_consent, _looks_blocked

GMT_COLUMNS = ["start", "stop", "travel_mode", "sample_time", "duration", "distance"]

# our Travel Mode -> Google Maps `travelmode` (api=1)
_MODE = {"best": "driving", "driving": "driving", "transit": "transit",
         "walking": "walking", "cycling": "bicycling", "flights": "driving"}

_DUR_RE = re.compile(r"(\d+\s*(?:h|hr|hour|hours|min|mins|minute|minutes)"
                     r"(?:\s*\d+\s*min(?:s)?)?)", re.I)
_DIST_RE = re.compile(r"(\d[\d,\.]*\s*(?:km|mi|m|ft)\b)", re.I)


def _dir_url(start: str, stop: str, mode: str) -> str:
    tm = _MODE.get((mode or "best").lower(), "driving")
    return ("https://www.google.com/maps/dir/?api=1"
            f"&origin={quote(start.strip())}&destination={quote(stop.strip())}"
            f"&travelmode={tm}&hl=en")


def _samples(time_from: str, time_to: str, interval_min: int) -> list[str]:
    """ISO timestamps from time_from to time_to stepping interval_min (cap 200). [] -> one 'now'."""
    def _parse(s):
        s = (s or "").strip().replace("/", "-")
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s[:len(fmt) + 2], fmt)
            except Exception:
                continue
        return None
    a, b = _parse(time_from), _parse(time_to)
    step = max(1, int(interval_min or 60))
    if not a:
        return [""]
    if not b or b <= a:
        return [a.strftime("%Y-%m-%d %H:%M")]
    out, t = [], a
    while t <= b and len(out) < 200:
        out.append(t.strftime("%Y-%m-%d %H:%M"))
        t += timedelta(minutes=step)
    return out


# ---------- internal directions-API interception (Option B) ----------
# Google computes the route via its own RPC; the response JSON carries the duration + distance as
# human-readable strings ("25 min", "12.3 km"). We capture those response bodies and read the values
# straight out — i.e. the internal API's data, not the rendered HTML. (DOM stays as a fallback.)
_ROUTE_RPC = ("/maps/dir", "/maps/preview/directions", "/maps/rpc", "/maps/preview", "/_/",
              "directions", "route")


def _attach_route_capture(pg):
    """Collect the raw bodies of Google Maps' internal directions-RPC responses for this page."""
    bodies: list[str] = []

    def _on_response(resp):
        try:
            u = (resp.url or "").lower()
            if "google.com/maps" in u and any(k in u for k in _ROUTE_RPC):
                bodies.append(resp.text())
        except Exception:
            pass

    pg.on("response", _on_response)
    return bodies


def _route_from_bodies(bodies: list[str]) -> dict:
    """Pull the route's duration + distance out of the captured internal-API JSON (first match =
    Google's recommended route, which it returns first)."""
    for b in bodies or []:
        dm = _DUR_RE.search(b or "")
        km = _DIST_RE.search(b or "")
        if dm or km:
            return {"duration": dm.group(1).strip() if dm else "",
                    "distance": km.group(1).strip() if km else ""}
    return {}


def _route_once(browser, start: str, stop: str, mode: str, proxy: str) -> dict:
    url = _dir_url(start, stop, mode)
    kw = {"locale": "en-US", "user_agent": _UA, "viewport": {"width": 1366, "height": 900},
          "proxy": _proxy_opts(proxy)}
    ctx = browser.new_context(**kw)
    try:
        ctx.add_cookies([{"name": "CONSENT", "value": "YES+", "domain": ".google.com", "path": "/"}])
        pg = ctx.new_page()
        route_bodies = _attach_route_capture(pg)   # Option B: capture the internal directions API
        pg.goto(url, timeout=45000, wait_until="domcontentloaded")
        pg.wait_for_timeout(3500)
        _accept_consent(pg)
        blocked = _looks_blocked(pg)
        if blocked:
            raise RuntimeError(f"Google blocked this request — {blocked}. Use a cleaner residential "
                               "proxy (datacenter/free IPs are blocked).")
        # wait for a trip card to render (also lets the directions RPC fire)
        for sel in ['div[id^="section-directions-trip-"]', '.MespJc', '[aria-label*="min" i]']:
            try:
                pg.wait_for_selector(sel, timeout=6000)
                break
            except Exception:
                pass
        # PRIMARY: duration + distance from the captured internal directions API (JSON), not the DOM
        api = _route_from_bodies(route_bodies)
        if api.get("duration") or api.get("distance"):
            return {"duration": api.get("duration", ""), "distance": api.get("distance", "")}
        # FALLBACK: read them from the rendered directions panel (DOM)
        duration, distance = "", ""
        try:
            el = pg.query_selector('div[id^="section-directions-trip-"]') or pg.query_selector(".MespJc")
            txt = (el.inner_text() if el else "") or ""
        except Exception:
            txt = ""
        if not txt:
            try:
                txt = pg.inner_text("body")
            except Exception:
                txt = ""
        dm = _DUR_RE.search(txt)
        km = _DIST_RE.search(txt)
        duration = dm.group(1).strip() if dm else ""
        distance = km.group(1).strip() if km else ""
        return {"duration": duration, "distance": distance}
    finally:
        ctx.close()


async def run_job(job_id: str, pairs: list[dict], time_from: str, time_to: str,
                  interval_min: int, travel_mode: str) -> None:
    from .db import jobs, gmaps_traffic
    proxy = settings.PROXY_URL.strip()
    total = 0
    try:
        if not proxy:
            raise RuntimeError("Google Maps traffic scraping needs a paid residential PROXY_URL in "
                               ".env — Google blocks free/datacenter IPs (no real IP is used).")
        slots = _samples(time_from, time_to, interval_min)
        for p in pairs:
            start, stop = (p.get("start") or "").strip(), (p.get("stop") or "").strip()
            if not (start and stop):
                continue
            route = await asyncio.to_thread(_run, lambda b: _route_once(b, start, stop, travel_mode, proxy))
            rows = [{"start": start, "stop": stop, "travel_mode": travel_mode,
                     "sample_time": s, "duration": route["duration"], "distance": route["distance"],
                     "job_id": job_id} for s in slots]
            if rows:
                await gmaps_traffic.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
