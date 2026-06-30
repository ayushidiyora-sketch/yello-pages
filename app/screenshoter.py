"""WebPage Screenshoter — capture a screenshot (or PDF) of any URL with a headless browser.

Renders each URL in headless Chromium (Playwright) at the requested viewport and saves a PNG / JPEG /
PDF under data/screenshots/<job>/. The browser is routed THROUGH A PROXY when one is available (real IP
not used); with no proxy configured it falls back to a direct render so the capture still works. WEBP is
not supported by the engine and falls back to PNG. One row per URL (metadata + a link to the image).
"""
import asyncio
import queue
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import yp_us
from .config import settings

SS_COLUMNS = ["url", "status", "format", "width", "height", "full_page", "screenshot"]
_DIR = Path("data/screenshots")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---- single Playwright worker thread (its own browser, separate from trustpilot's) ----
_jobs: "queue.Queue" = queue.Queue()
_started = False
_lock = threading.Lock()


def _worker():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True,
                                 args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    while True:
        fn, holder, done = _jobs.get()
        try:
            holder.append(fn(browser))
        except Exception as e:
            holder.append(e)
        finally:
            done.set()


def _run(fn):
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
    holder, done = [], threading.Event()
    _jobs.put((fn, holder, done))
    done.wait()
    res = holder[0]
    if isinstance(res, Exception):
        raise res
    return res


def _proxy_opts(px: str) -> dict:
    u = urlparse(px if "://" in px else "http://" + px)
    o = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        o["username"] = u.username
    if u.password:
        o["password"] = u.password
    return o


def _proxies() -> list:
    """Proxy candidates (paid PROXY_URL first, then a few rotating proxies.txt IPs); '' = direct as a
    last resort so a screenshot still happens when no proxy is configured."""
    pin = settings.PROXY_URL.strip()
    rot = [p for p in yp_us._plist_candidates(4) if p != pin]
    return ([pin] if pin else []) + rot + [""]


def _ext_for(fmt: str) -> tuple:
    f = (fmt or "png").lower()
    if f == "pdf":
        return "pdf", "pdf"
    if f in ("jpeg", "jpg"):
        return "jpeg", "jpg"
    return "png", "png"  # png and webp(unsupported)->png


def _capture(browser, url, kind, width, height, full_page, out_path) -> bool:
    for px in _proxies():
        kw = {"locale": "en-US", "user_agent": _UA, "viewport": {"width": width, "height": height}}
        if px:
            kw["proxy"] = _proxy_opts(px)
        ctx = browser.new_context(**kw)
        try:
            pg = ctx.new_page()
            pg.goto(url, timeout=40000, wait_until="load")
            pg.wait_for_timeout(1200)  # let late assets paint
            if kind == "pdf":
                data = pg.pdf(print_background=True)
                out_path.write_bytes(data)
            else:
                pg.screenshot(path=str(out_path), full_page=full_page, type=kind)
            return True
        except Exception:
            pass
        finally:
            ctx.close()
    return False


def search_sync(url: str, fmt: str, width: int, height: int, full_page: bool, job_id: str, idx: int) -> dict:
    u = (url or "").strip()
    if not u:
        return {}
    if not u.lower().startswith("http"):
        u = "https://" + u
    kind, ext = _ext_for(fmt)
    row = {"url": u, "status": "", "format": ext, "width": width, "height": height,
           "full_page": full_page, "screenshot": ""}
    out_dir = _DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{idx}.{ext}"
    try:
        ok = _run(lambda b: _capture(b, u, kind, width, height, full_page, out_path))
    except Exception:
        ok = False
    if ok and out_path.exists():
        row["status"] = "ok"
        row["screenshot"] = f"/api/screenshoter/file/{job_id}/{idx}.{ext}"
    else:
        row["status"] = "failed (page did not load / proxy blocked)"
    return row


async def run_job(job_id: str, queries: list, fmt: str = "png", width: int = 1200,
                  height: int = 800, full_page: bool = False) -> None:
    from .db import jobs, screenshoter
    total = 0
    try:
        for i, q in enumerate(queries):
            row = await asyncio.to_thread(search_sync, q, fmt, width, height, full_page, job_id, i)
            if row:
                row["job_id"] = job_id
                await screenshoter.insert_one(row)
                total += 1
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
