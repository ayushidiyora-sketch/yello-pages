"""Email Addresses Finder — find a professional email from a full name + company domain.

A query is "<full name> <company domain>", e.g. "John Doe company.com" or "John company.com".
For each query we build the common corporate email permutations (first.last@, flast@, first@, …),
look up the domain's MX host (DNS-over-HTTPS through the proxy — the real IP is never used) and probe
each candidate over SMTP, returning the address(es) the mail server accepts (best guess first).

If the domain is catch-all or SMTP (port 25) is blocked, the candidates can't be confirmed live; the
top pattern is then returned as a best-effort guess marked accordingly. SMTP probing follows the same
EMAIL_SMTP_* settings as the Email Address Verifier.
"""
import asyncio
import re
from datetime import datetime

from . import enrich
from .config import settings
from .email_verifier import _mx_host, _smtp_probe
from .scraper import STOP_REQUESTS

EF_COLUMNS = [
    "query", "full_name", "domain", "email", "pattern", "status", "status_details",
    "deliverable", "catch_all", "mx_host",
]

_PROBE_LOCAL = "no-such-user-zzqx9137"


def _parse_query(query: str) -> tuple[str, str, str]:
    """Split "John Doe company.com" -> (first, last, domain). Domain = the token containing a dot."""
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return "", "", ""
    tokens = q.split(" ")
    domain = ""
    name_tokens = []
    for t in tokens:
        # a domain token: has a dot, or is an email -> take the part after @
        if "@" in t:
            domain = t.split("@")[-1]
        elif "." in t and re.search(r"\.[a-z]{2,}$", t, re.I):
            domain = t
        else:
            name_tokens.append(t)
    domain = re.sub(r"^https?://", "", domain, flags=re.I).replace("www.", "").strip("/").lower()
    first = name_tokens[0].lower() if name_tokens else ""
    last = name_tokens[-1].lower() if len(name_tokens) > 1 else ""
    first = re.sub(r"[^a-z]", "", first)
    last = re.sub(r"[^a-z]", "", last)
    return first, last, domain


def _candidates(first: str, last: str, domain: str) -> list[tuple[str, str]]:
    """Return ordered (email, pattern_label) candidates, most-likely first."""
    f, l = first, last
    pats = []
    if f and l:
        pats = [
            (f"{f}.{l}", "first.last"),
            (f"{f}{l}", "firstlast"),
            (f"{f[0]}{l}", "flast"),
            (f"{f}{l[0]}", "firstl"),
            (f"{f}_{l}", "first_last"),
            (f"{f}-{l}", "first-last"),
            (f"{l}.{f}", "last.first"),
            (f"{l}{f}", "lastfirst"),
            (f"{f[0]}.{l}", "f.last"),
            (f, "first"),
            (l, "last"),
        ]
    elif f:
        pats = [(f, "first")]
    seen, out = set(), []
    for local, label in pats:
        if local and local not in seen:
            seen.add(local)
            out.append((f"{local}@{domain}", label))
    return out


# ---------------- find + run loop ----------------

def search_sync(query: str, limit: int | None = None) -> list[dict]:
    first, last, domain = _parse_query(query)
    if not domain or not first:
        return [{c: "" for c in EF_COLUMNS} | {
            "query": query, "full_name": (first + " " + last).strip(),
            "domain": domain, "status": "invalid",
            "status_details": "Need a full name and a company domain (e.g. 'John Doe company.com')."}]

    cands = _candidates(first, last, domain)
    base = {c: "" for c in EF_COLUMNS}
    base.update({"query": query, "full_name": (first + " " + last).strip(), "domain": domain})

    host = _mx_host(domain)
    if not host:
        # no MX — return top guess, unverifiable
        row = dict(base)
        email, label = cands[0]
        row.update(email=email, pattern=label, status="guess",
                   status_details="No MX record for domain; unverified best-guess.", mx_host="")
        return [row]

    smtp_enabled = getattr(settings, "EMAIL_SMTP_CHECK", True)
    if not smtp_enabled:
        row = dict(base)
        email, label = cands[0]
        row.update(email=email, pattern=label, status="guess",
                   status_details="MX ok; SMTP check disabled — unverified best-guess.",
                   mx_host=host, deliverable=False)
        return [row]

    # detect catch-all once
    _, catch_all, reachable = _smtp_probe(host, domain, f"{_PROBE_LOCAL}@{domain}")
    if not reachable:
        row = dict(base)
        email, label = cands[0]
        row.update(email=email, pattern=label, status="guess", mx_host=host,
                   status_details="SMTP unreachable (port 25 blocked) — unverified best-guess.")
        return [row]
    if catch_all:
        row = dict(base)
        email, label = cands[0]
        row.update(email=email, pattern=label, status="risky", catch_all=True, mx_host=host,
                   status_details="Catch-all domain — accepts any address; best-guess pattern.")
        return [row]

    found = []
    for email, label in cands:
        if limit and len(found) >= limit:
            break
        mailbox_ok, _ca, ok = _smtp_probe(host, domain, email)
        if not ok:
            break
        if mailbox_ok:
            row = dict(base)
            row.update(email=email, pattern=label, status="deliverable", deliverable=True,
                       status_details="SMTP accepted the mailbox.", mx_host=host)
            found.append(row)
    if found:
        return found[:limit] if limit else found

    # none accepted — return top guess
    row = dict(base)
    email, label = cands[0]
    row.update(email=email, pattern=label, status="not_found", mx_host=host,
               status_details="No common pattern was accepted by the mail server.")
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


def to_export(doc: dict) -> dict:
    return {c: doc.get(c, "") for c in EF_COLUMNS}


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    """Background task: find the professional email for each name+domain query."""
    from .db import jobs, email_finder_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await email_finder_results.insert_many(rows)
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
