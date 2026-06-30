"""Shared schema.org JSON-LD event parser (used by the Eventbrite and Meetup scrapers).

Event listing sites embed their events as JSON-LD `Event` nodes in the page HTML — this pulls them out
into flat rows. Standard schema.org fields, so one parser covers both sites.
"""
import json
import re

from bs4 import BeautifulSoup

EVENT_COLUMNS = ["query", "name", "start", "end", "venue", "address", "price", "currency",
                 "organizer", "url"]

_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def _addr(loc) -> tuple:
    """(venue_name, address_str) from a schema.org location/Place."""
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    if not isinstance(loc, dict):
        return "", ""
    venue = loc.get("name") or ""
    a = loc.get("address")
    if isinstance(a, str):
        return venue, a
    if isinstance(a, dict):
        parts = [a.get("streetAddress"), a.get("addressLocality"), a.get("addressRegion"),
                 a.get("postalCode"), a.get("addressCountry")]
        return venue, ", ".join(str(p) for p in parts if p)
    return venue, ""


def _offer(offers) -> tuple:
    """(price, currency) from schema.org offers (lowest if a list)."""
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return "", ""
    price = offers.get("price") or offers.get("lowPrice") or ""
    return str(price), offers.get("priceCurrency") or ""


def _event_row(node: dict, query: str) -> dict:
    venue, address = _addr(node.get("location"))
    price, currency = _offer(node.get("offers"))
    org = node.get("organizer")
    if isinstance(org, list):
        org = org[0] if org else {}
    return {
        "query": query,
        "name": node.get("name") or "",
        "start": node.get("startDate") or "",
        "end": node.get("endDate") or "",
        "venue": venue,
        "address": address,
        "price": price,
        "currency": currency,
        "organizer": (org or {}).get("name", "") if isinstance(org, dict) else "",
        "url": node.get("url") or "",
    }


def _walk(node, out: list, query: str):
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.endswith("Event") for x in types) and node.get("name"):
            out.append(_event_row(node, query))
        for v in node.values():
            _walk(v, out, query)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, query)


def events_from_html(html: str, query: str) -> list:
    """All schema.org Event nodes in the page's JSON-LD, as flat rows (deduped by name+start)."""
    out: list = []
    for m in _LD_RE.finditer(html or ""):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        _walk(data, out, query)
    seen, uniq = set(), []
    for r in out:
        key = (r["name"], r["start"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


# fallback: a few sites only expose events via Next.js data, not JSON-LD
def soup(html: str):
    return BeautifulSoup(html or "", "lxml")
