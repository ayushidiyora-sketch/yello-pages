"""Tiny SMTP sender for the Trustpilot Reviews Monitoring email reports.

No-op (never raises) when SMTP isn't configured — the monitor still scrapes and records its state, it
just doesn't send. Configure SMTP_HOST/PORT/USER/PASS/FROM in .env to enable.
"""
import asyncio
import smtplib
from email.message import EmailMessage

from .config import settings


def _send_sync(to: str, subject: str, html: str) -> tuple[bool, str]:
    host = settings.SMTP_HOST.strip()
    if not host:
        return False, "SMTP not configured (set SMTP_HOST in .env)"
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


async def send_email(to: str, subject: str, html: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_send_sync, to, subject, html)
