"""Email sender for the Reviews Monitoring reports — Resend API first, SMTP fallback.

Priority: if RESEND_API_KEY is set, send via Resend's HTTP API (just an API key, no SMTP host).
Otherwise fall back to SMTP (SMTP_HOST/PORT/USER/PASS/FROM). If neither is configured this is a
no-op (never raises) — the monitor still scrapes and records its state, it just doesn't send.
"""
import asyncio
import json
import smtplib
import urllib.request
from email.message import EmailMessage

from .config import settings


def _send_resend_sync(to: str, subject: str, html: str) -> tuple[bool, str]:
    key = settings.RESEND_API_KEY.strip()
    if not (to or "").strip():
        return False, "no recipient email"
    sender = settings.RESEND_FROM.strip() or "Live Scraper <onboarding@resend.dev>"
    payload = json.dumps({"from": sender, "to": [to], "subject": subject, "html": html}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 # Resend sits behind Cloudflare, which 403s urllib's default UA (error 1010).
                 "User-Agent": "Mozilla/5.0 (LiveScraper monitoring)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
        eid = ""
        try:
            eid = json.loads(body).get("id", "")
        except Exception:
            pass
        return True, f"sent via Resend{(' id=' + eid) if eid else ''}"
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300] if hasattr(e, "read") else str(e)
        return False, f"Resend HTTP {e.code}: {detail}"
    except Exception as e:
        return False, f"Resend {type(e).__name__}: {e}"


def _send_smtp_sync(to: str, subject: str, html: str) -> tuple[bool, str]:
    host = settings.SMTP_HOST.strip()
    if not host:
        return False, "no email provider configured (set RESEND_API_KEY or SMTP_HOST in .env)"
    if not (to or "").strip():
        return False, "no recipient email"
    sender = settings.SMTP_FROM.strip() or settings.SMTP_USER.strip() or "no-reply@livescraper.local"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("This report is HTML — open it in an HTML-capable mail client.")
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, settings.SMTP_PORT, timeout=30) as s:
            s.ehlo()
            try:
                s.starttls()
                s.ehlo()
            except smtplib.SMTPException:
                pass  # server may not support STARTTLS
            if settings.SMTP_USER.strip():
                s.login(settings.SMTP_USER.strip(), settings.SMTP_PASS)
            s.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _send_sync(to: str, subject: str, html: str) -> tuple[bool, str]:
    if settings.RESEND_API_KEY.strip():
        return _send_resend_sync(to, subject, html)
    return _send_smtp_sync(to, subject, html)


async def send_email(to: str, subject: str, html: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_send_sync, to, subject, html)
