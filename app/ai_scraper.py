"""AI Scraper — extract structured data from ANY web page using Claude.

A query is a URL. Each page is fetched through the proxy pool (paid PROXY_URL / rotating PROXY_LIST,
else the free pool — the REAL IP is never used), its readable text is extracted, and sent to Claude
(claude-opus-4-8) along with the user's free-text Prompt ("what data to extract") and/or a JSON
Schema (the field builder). Claude returns the structured data, which becomes one or more rows.

Needs ANTHROPIC_API_KEY in .env (a paid Claude API key — https://console.anthropic.com). With no key
the job returns a clear "set ANTHROPIC_API_KEY" error. Model is configurable via AI_MODEL.
"""
import asyncio
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import yp_us
from .config import settings
from .scraper import STOP_REQUESTS

_MAX_CHARS = 60000          # cap of page text sent to the model (keeps token cost bounded)


# ---------------- page fetch (never the real IP) ----------------

def _page_text(url: str):
    """Fetch a URL through the proxy pool and return (title, readable_text) or (None, None)."""
    try:
        r = yp_us.pooled_get(url, {}, timeout=25)
    except Exception:
        return None, None
    if r is None or r.status_code != 200 or not (r.text or "").strip():
        return None, None
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.extract()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return title, text[:_MAX_CHARS]


# ---------------- Claude extraction ----------------

def _extract(url: str, title: str, text: str, prompt: str, schema: dict | None):
    """Send the page to Claude with the prompt/schema; return parsed JSON (dict or list) or None."""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY.strip())

    parts = [f"URL: {url}", f"Page title: {title}", "", "PAGE CONTENT:", text, ""]
    has_schema = bool(schema and isinstance(schema, dict) and schema.get("properties"))
    if has_schema:
        parts += ["Extract data that matches EXACTLY this JSON Schema (only these fields, correct types):",
                  json.dumps(schema, ensure_ascii=False)]
    if prompt and prompt.strip():
        parts.append("Instructions: " + prompt.strip())
    if not has_schema and not (prompt and prompt.strip()):
        parts.append("Extract the most relevant structured data from this page.")
    parts.append("\nReturn ONLY valid JSON — a single object, or an array of objects if the page "
                 "lists multiple items. No prose, no markdown code fences.")

    resp = client.messages.create(
        model=settings.AI_MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    raw = next((b.text for b in resp.content if b.type == "text"), "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", raw, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _flatten_rows(data, url: str) -> list[dict]:
    """Turn Claude's JSON (object or list of objects) into flat rows (nested values -> JSON text)."""
    items = data if isinstance(data, list) else [data]
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = {"query": url}
        for k, v in it.items():
            row[k] = v if (v is None or isinstance(v, (str, int, float, bool))) \
                else json.dumps(v, ensure_ascii=False)
        rows.append(row)
    return rows


# ---------------- scrape + run loop ----------------

def search_sync(query: str, prompt: str = "", schema: dict | None = None,
                limit: int | None = None) -> list[dict]:
    if not settings.ANTHROPIC_API_KEY.strip():
        raise RuntimeError("AI Scraper needs an ANTHROPIC_API_KEY in .env (a paid Claude API key from "
                           "console.anthropic.com). The real IP is never used for page fetches.")
    url = (query or "").strip()
    if not url:
        return []
    title, text = _page_text(url)
    if text is None:
        return []
    data = _extract(url, title, text, prompt, schema)
    if data is None:
        return []
    rows = _flatten_rows(data, url)
    return rows[:limit] if limit else rows


async def search(query: str, prompt: str = "", schema: dict | None = None,
                 limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, prompt, schema, limit)


async def run_job(job_id: str, queries: list[str], prompt: str, schema: dict | None,
                  limit: int | None) -> None:
    """Background task: extract structured data from each URL with Claude and store the rows."""
    from .db import jobs, ai_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, prompt, schema, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await ai_results.insert_many(rows)
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
