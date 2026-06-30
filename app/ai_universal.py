"""Universal AI-Powered Scraper — extract chosen attributes from ANY web page using Claude.

A query is a URL. The page is fetched through the proxy pool (paid PROXY_URL / rotating PROXY_LIST,
else the free pool — the REAL IP is never used), its readable text is extracted and sent to Claude
(claude-opus-4-8) along with the list of attributes the user wants (e.g. name, price, discount price,
description, image, rating, reviews — or any custom attribute). Claude returns one object per item on
the page (or one for a single-item page); each becomes a row whose columns are exactly the requested
attributes.

Needs ANTHROPIC_API_KEY in .env (a paid Claude API key — https://console.anthropic.com). With no key
the job returns a clear "set ANTHROPIC_API_KEY" error. Model is configurable via AI_MODEL.
"""
import asyncio
import json
import re
from datetime import datetime

from .ai_scraper import _page_text
from .config import settings
from .scraper import STOP_REQUESTS


# ---------------- Claude extraction ----------------

def _extract(url: str, title: str, text: str, attributes: list[str]):
    """Ask Claude to extract the requested attributes; return a list of objects (or None)."""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY.strip())

    attrs = [a.strip() for a in attributes if a and a.strip()]
    attr_lines = "\n".join(f"- {a}" for a in attrs)
    parts = [
        f"URL: {url}", f"Page title: {title}", "", "PAGE CONTENT:", text, "",
        "Extract EXACTLY these attributes for each relevant item on the page:",
        attr_lines, "",
        "Rules:",
        "- Return ONLY valid JSON — an array of objects (use a single-element array for a single-item page).",
        "- Each object must have EXACTLY these keys: " + ", ".join(json.dumps(a) for a in attrs) + ".",
        "- Use the empty string \"\" when an attribute is not present on the page. Do not invent values.",
        "- No prose, no markdown code fences.",
    ]
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


def _rows(data, url: str, attributes: list[str]) -> list[dict]:
    attrs = [a.strip() for a in attributes if a and a.strip()]
    items = data if isinstance(data, list) else [data]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = {"query": url}
        for a in attrs:
            v = it.get(a, "")
            row[a] = v if (v is None or isinstance(v, (str, int, float, bool))) \
                else json.dumps(v, ensure_ascii=False)
        out.append(row)
    return out


# ---------------- scrape + run loop ----------------

def search_sync(query: str, attributes: list[str], limit: int | None = None) -> list[dict]:
    if not settings.ANTHROPIC_API_KEY.strip():
        raise RuntimeError("Universal AI-Powered Scraper needs an ANTHROPIC_API_KEY in .env (a paid "
                           "Claude API key from console.anthropic.com). The real IP is never used for "
                           "page fetches.")
    url = (query or "").strip()
    if not url or not attributes:
        return []
    title, text = _page_text(url)
    if text is None:
        return []
    data = _extract(url, title, text, attributes)
    if data is None:
        return []
    rows = _rows(data, url, attributes)
    return rows[:limit] if limit else rows


async def search(query: str, attributes: list[str], limit: int | None = None) -> list[dict]:
    return await asyncio.to_thread(search_sync, query, attributes, limit)


async def run_job(job_id: str, queries: list[str], attributes: list[str], limit: int | None) -> None:
    """Background task: extract the requested attributes from each URL with Claude and store rows."""
    from .db import jobs, ai_universal_results
    total = 0
    try:
        for q in queries:
            if job_id in STOP_REQUESTS:
                break
            rows = await search(q, attributes, limit)
            for i, r in enumerate(rows):
                r["job_id"] = job_id
                r["position"] = total + i + 1
            if rows:
                await ai_universal_results.insert_many(rows)
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
