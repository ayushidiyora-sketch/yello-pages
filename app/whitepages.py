"""Reverse-phone owner lookup — free, via thatsthem.com.

whitepages.com / numlookup / usphonebook / truepeoplesearch are all Cloudflare-walled, but
thatsthem.com serves the same kind of data (phone owner name + address) as plain HTML with a
structured JSON-LD `Person` node — fetchable with curl_cffi, no browser/key/paywall.

Fills the Outscraper `*.whitepages_phones.name` / `.address` columns. Best-effort: a number
with no record, a timeout, or a rate-limit just yields empties (never an error).
"""
import asyncio
import json
import re
import threading

from curl_cffi import requests as cffi

from .config import settings

BASE = "https://thatsthem.com/phone/"
_cache: dict[str, dict] = {}
_lock = threading.Lock()
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _digits(number: str) -> str | None:
    d = re.sub(r"\D", "", number or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return d if len(d) == 10 else None


def _fmt_address(a: dict) -> str | None:
    if not isinstance(a, dict):
        return None
    line = ", ".join(x for x in (a.get("streetAddress"), a.get("addressLocality")) if x)
    sz = " ".join(x for x in (a.get("addressRegion"), a.get("postalCode")) if x)
    return ", ".join(x for x in (line, sz) if x) or None


def _first_person(html: str) -> dict:
    """Return {name, address} from the first JSON-LD Person node, if any."""
    for m in _LD_RE.finditer(html):
        try:
            d = json.loads(m.group(1))
        except (ValueError, json.JSONDecodeError):
            continue
        graph = d.get("@graph", [d]) if isinstance(d, dict) else (d if isinstance(d, list) else [d])
        for n in graph:
            if not (isinstance(n, dict) and n.get("@type") == "Person" and n.get("name")):
                continue
            addr = None
            hl = n.get("homeLocation") or []
            if isinstance(hl, dict):
                hl = [hl]
            for place in hl:
                addr = _fmt_address((place or {}).get("address"))
                if addr:
                    break
            return {"name": n.get("name"), "address": addr}
    return {"name": None, "address": None}


def lookup_sync(number: str) -> dict:
    d = _digits(number)
    if not d:
        return {"name": None, "address": None}
    with _lock:
        if d in _cache:
            return _cache[d]
    res = {"name": None, "address": None}
    try:
        from . import yp_us
        url = f"{BASE}{d[:3]}-{d[3:6]}-{d[6:]}"
        r = yp_us.pooled_get(url, timeout=settings.ENRICH_TIMEOUT)  # proxy, never real IP
        if r is not None and r.status_code == 200 and "ld+json" in r.text.lower():
            res = _first_person(r.text)
    except Exception:
        pass
    with _lock:
        _cache[d] = res
    return res


async def lookup(number: str) -> dict:
    return await asyncio.to_thread(lookup_sync, number)
