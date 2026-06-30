"""Email Address Verifier — clean an email list before sending: detect invalid, risky and
undeliverable addresses, Outscraper-style (RECEIVING / Catch All / SMTP validated / Cannot validate).

Pipeline per email:
  1. format check (regex)
  2. disposable-domain flag
  3. MX lookup via DNS-over-HTTPS THROUGH A PROXY (real IP not used for DNS)
  4. live SMTP mailbox check (best-effort): connect to the MX host, MAIL FROM + RCPT TO the address,
     plus a random-address probe to detect a catch-all domain.

The SMTP socket is a DIRECT connection (mail servers reject HTTP proxies), so step 4 uses the real IP
— unlike the proxied DNS lookup. Outbound port 25 is blocked on many networks; when it is (or
EMAIL_SMTP_CHECK=false), the address falls back to the domain-level verdict ("Cannot validate"). No
mail is ever sent. One row per email.
"""
import asyncio
import smtplib
import socket
from datetime import datetime

from . import enrich
from . import yp_us
from .config import settings

# columns mirror the Outscraper emails-validator output (status + status_details) plus a few flags
EV_COLUMNS = ["email", "status", "status_details", "deliverable", "catch_all", "disposable",
              "role_account", "mx_host"]

# a local-part that is extremely unlikely to exist — used to detect catch-all domains
_PROBE_LOCAL = "no-such-user-zzqx9137"


def _mx_host(domain: str) -> str | None:
    """Best MX exchange host for a domain (lowest preference), via DNS-over-HTTPS through the proxy."""
    try:
        r = yp_us.pooled_get(f"https://dns.google/resolve?name={domain}&type=MX", timeout=8)
        if r is not None and r.status_code == 200:
            mx = [a for a in (r.json().get("Answer") or []) if a.get("type") == 15 and a.get("data")]
            if mx:
                mx.sort(key=lambda a: int(a["data"].split()[0]) if a["data"].split()[0].isdigit() else 99)
                return mx[0]["data"].split()[-1].rstrip(".")
    except Exception:
        pass
    return None


def _smtp_probe(mx_host: str, domain: str, addr: str) -> tuple:
    """Returns (mailbox_ok, catch_all, reachable). mailbox_ok/catch_all are None when unknown.
    reachable is False when the SMTP server could not be contacted (port 25 blocked / timeout)."""
    server = None
    try:
        server = smtplib.SMTP(timeout=settings.EMAIL_SMTP_TIMEOUT)
        server.connect(mx_host, 25)
        server.ehlo_or_helo_if_needed()
        sender = settings.EMAIL_SMTP_FROM
        server.mail(sender)
        code, _ = server.rcpt(addr)
        mailbox_ok = code in (250, 251)
        # catch-all: does the server also accept an address that surely doesn't exist?
        catch_all = False
        if mailbox_ok:
            code2, _ = server.rcpt(f"{_PROBE_LOCAL}@{domain}")
            catch_all = code2 in (250, 251)
        return mailbox_ok, catch_all, True
    except (socket.timeout, smtplib.SMTPException, OSError, ConnectionError):
        return None, None, False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def search_sync(query: str, limit: int | None = None) -> list[dict]:
    """One input email -> one verification row (Outscraper-style status + status_details)."""
    addr = (query or "").strip()
    if not addr:
        return []
    row = {"email": addr, "status": "", "status_details": "", "deliverable": False,
           "catch_all": False, "disposable": False, "role_account": False, "mx_host": ""}

    # 1. format
    if not enrich.EMAIL_RE.fullmatch(addr):
        row.update(status="invalid", status_details="Invalid format")
        return [row]

    local, _, domain = addr.partition("@")
    domain = domain.lower()
    row["role_account"] = local.lower() in enrich._ROLE_LOCALS

    # 2. disposable
    if domain in enrich._DISPOSABLE:
        row.update(status="risky", status_details="Disposable domain", disposable=True)
        return [row]

    # 3. MX
    host = _mx_host(domain)
    row["mx_host"] = host or ""
    if not host:
        row.update(status="undeliverable", status_details="No MX record")
        return [row]

    # 4. live SMTP mailbox check (best-effort, direct connection)
    if not settings.EMAIL_SMTP_CHECK:
        row.update(status="risky", status_details="MX ok; SMTP check disabled", deliverable=True)
        return [row]

    mailbox_ok, catch_all, reachable = _smtp_probe(host, domain, addr)
    if not reachable:
        # port 25 blocked / server unreachable — domain is mail-capable but mailbox unconfirmed
        row.update(status="unknown", status_details="Cannot validate (SMTP blocked)", deliverable=True)
    elif catch_all:
        row.update(status="risky", status_details="Catch All", deliverable=True, catch_all=True)
    elif mailbox_ok:
        row.update(status="receiving", status_details="SMTP validated", deliverable=True)
    elif mailbox_ok is False:
        row.update(status="undeliverable", status_details="Mailbox not found")
    else:
        row.update(status="unknown", status_details="Cannot validate", deliverable=True)
    return [row]


async def search(query: str, limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit)


async def run_job(job_id: str, queries: list[str], limit: int | None = None) -> None:
    from .db import jobs, email_verifier
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await email_verifier.insert_many(rows)
                total += len(rows)
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
