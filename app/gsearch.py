"""Google Search Scraper backend — powered by DuckDuckGo.

Real Google blocks every free scraping route (429 / "enable JS" / /sorry CAPTCHA), so this
fetches DuckDuckGo's HTML endpoint instead — through the proxy pool (`yp_us.pooled_get`), so the
real IP is never used. Returns rows shaped like Outscraper's Google Search output:
query, link, title, description, question, position, type.
"""
import asyncio
import json
from urllib.parse import unquote, urlparse, parse_qs

from bs4 import BeautifulSoup

from . import yp_us

DDG = "https://html.duckduckgo.com/html/"
# Outscraper-style date range -> DuckDuckGo `df` param
DATE_MAP = {"": "", "any": "", "any time": "", "day": "d", "past day": "d", "week": "w",
            "past week": "w", "month": "m", "past month": "m", "year": "y", "past year": "y"}


def _real_url(href: str) -> str:
    """DDG wraps links as //duckduckgo.com/l/?uddg=<encoded-url> — decode to the real URL."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        q = parse_qs(urlparse(href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href


# DuckDuckGo locale codes are fixed `country-language` pairs (not free-form).
_VALID_KL = {
    "us-en", "uk-en", "ca-en", "ca-fr", "au-en", "in-en", "ie-en", "nz-en", "za-en", "sg-en",
    "fr-fr", "be-fr", "ch-fr", "de-de", "at-de", "ch-de", "es-es", "mx-es", "ar-es", "cl-es",
    "it-it", "nl-nl", "be-nl", "pt-pt", "br-pt",
}
_LANG_HOME = {"en": "us-en", "fr": "fr-fr", "de": "de-de", "es": "es-es",
              "it": "it-it", "pt": "pt-pt", "nl": "nl-nl"}


def _kl(region: str, language: str) -> str:
    """Pick a valid DDG locale honoring the chosen region+language. If the exact pair isn't a
    real DDG locale (e.g. India+German), prefer the LANGUAGE's home locale so the selected
    language still applies; else fall back to the region in English."""
    r = (region or "us").lower()[:2]
    l = (language or "en").lower()[:2]
    combo = f"{r}-{l}"
    if combo in _VALID_KL:
        return combo                       # both region and language honored
    if l in _LANG_HOME:
        return _LANG_HOME[l]               # honor the chosen language
    if f"{r}-en" in _VALID_KL:
        return f"{r}-en"                    # honor the region (English)
    return "wt-wt"


TRANSLATE = "https://translate.googleapis.com/translate_a/single"


def _translate_one(text: str, target: str) -> str:
    """Translate a single string into `target`. Retries through the flaky free proxy a few
    times before giving up (returns the original only if every attempt fails)."""
    text = (text or "").strip()
    if not text:
        return text
    for _attempt in range(5):
        try:
            r = yp_us.pooled_get(TRANSLATE,
                                 {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
                                 timeout=20)
            if r is not None and r.status_code == 200:
                data = r.json()
                out = "".join(seg[0] for seg in data[0] if seg and seg[0])
                if out:
                    return out
        except Exception:
            pass
    return text


def _needs_translation(text: str, target: str) -> bool:
    """Whether `text` should be translated into `target`. For non-English targets, always
    (translate everything). For English, only rows with a non-Latin-script char (Hangul, CJK,
    Cyrillic, Arabic, Greek, …) — so foreign-script results become English while already-Latin
    rows are left untouched (keeps big English runs fast and avoids rewording)."""
    if not target.lower().startswith("en"):
        return True
    # a LETTER beyond Latin (Latin/Latin-Extended end at U+024F) -> non-Latin script (Hangul, CJK,
    # Cyrillic, Arabic, Greek…). `isalpha` excludes punctuation/symbols like en-dash, ™, "smart quotes".
    return any(c.isalpha() and ord(c) > 0x024F for c in text)


def _translate_rows(rows: list[dict], target: str) -> None:
    """Translate each result's title + description into `target` (in place), via the free Google
    Translate endpoint through the proxy pool. One request per row (title+description joined by a
    newline, split back) keeps alignment exact. Rows already in the target language are skipped
    (see `_needs_translation`); related-search rows are skipped."""
    if not target:
        return
    for r in rows:
        if r.get("type") == "related":
            continue
        # the synthesized People-Also-Ask question follows the selected language too
        q = (r.get("question") or "").strip()
        if q and _needs_translation(q, target):
            r["question"] = _translate_one(q, target)
        title = (r.get("title") or "").replace("\n", " ").strip()
        desc = (r.get("description") or "").replace("\n", " ").strip()
        if not title and not desc:
            continue
        if not _needs_translation(title + " " + desc, target):
            continue
        out = _translate_one(title + "\n" + desc, target) if (title and desc) else None
        parts = out.split("\n") if out else []
        if len(parts) == 2:
            r["title"], r["description"] = parts[0].strip(), parts[1].strip()
        else:
            # newline didn't survive (or only one field) -> translate each field on its own
            if title:
                r["title"] = _translate_one(title, target)
            if desc:
                r["description"] = _translate_one(desc, target)


AC = "https://duckduckgo.com/ac/"
# question seeds -> "People also ask" style related questions from autocomplete
_Q_SEEDS = ["how", "what", "where", "why", "can", "best"]


def _autocomplete(seed: str) -> list[str]:
    """Suggestions for `seed` from DuckDuckGo autocomplete (through the proxy). Returns []. The
    response is `["seed", ["sug1", "sug2", ...]]`."""
    for _attempt in range(3):
        try:
            r = yp_us.pooled_get(AC, {"q": seed, "type": "list"}, timeout=15)
            if r is not None and r.status_code == 200:
                data = json.loads(r.text)
                if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
                    return [s for s in data[1] if isinstance(s, str)]
        except Exception:
            pass
    return []


def _people_also_ask(query: str, n: int = 10) -> list[str]:
    """Generate ~n related questions for `query` by seeding autocomplete with question words
    (DuckDuckGo has no real 'People also ask'). Title-cased and '?'-terminated."""
    query = (query or "").strip()
    if not query:
        return []
    ql, seen, out = query.lower(), set(), []
    for seed in _Q_SEEDS:
        for s in _autocomplete(f"{seed} {query}"):
            s = s.strip()
            k = s.lower()
            if not s or k in seen or ql not in k:
                continue
            seen.add(k)
            q = s[0].upper() + s[1:]
            out.append(q if q.endswith("?") else q + "?")
            if len(out) >= n:
                return out
    return out


def _parse(html: str, query: str, start_pos: int) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    rows, pos = [], start_pos
    for c in soup.select("div.result"):
        a = c.select_one("a.result__a")
        if not a:
            continue
        cls = c.get("class") or []
        rtype = "ad" if "result--ad" in cls else ("news" if "result--news" in cls else "organic")
        sn = c.select_one(".result__snippet")
        pos += 1
        rows.append({
            "query": query,
            "link": _real_url(a.get("href")),
            "title": a.get_text(" ", strip=True),
            "description": sn.get_text(" ", strip=True) if sn else "",
            "question": "",
            "position": pos,
            "type": rtype,
        })
    # DuckDuckGo has no "People also ask"; surface related searches in the `question` field
    for rel in soup.select(".related-searches__link, a.js-related-search-link"):
        t = rel.get_text(" ", strip=True)
        if t:
            rows.append({"query": query, "link": "", "title": "", "description": "",
                         "question": t, "position": 0, "type": "related"})
    return rows


MAX_PAGES = 15  # safety cap when limit is blank ("all")

# ---------------- DuckDuckGo's free JSON API (links.duckduckgo.com/d.js) ----------------
# This is the same internal API DuckDuckGo's own frontend uses — free, no key (just a `vqd`
# token obtained from a first request). We read JSON results instead of parsing the HTML page.
LINKS = "https://links.duckduckgo.com/d.js"


def _strip_tags(s: str) -> str:
    import re, html
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _vqd(query: str) -> str:
    """One-time token DuckDuckGo requires before serving JSON results (free, from the home page)."""
    import re
    for _attempt in range(3):
        r = yp_us.pooled_get("https://duckduckgo.com/", {"q": query, "ia": "web"}, timeout=15)
        if r is not None and r.status_code == 200:
            m = re.search(r'vqd=["\']?([\d-]+)', r.text)
            if m:
                return m.group(1)
    return ""


def _extract_array(text: str):
    """Pull the results array out of `DDG.pageLayout.load('d', [ ... ]);` (string-aware)."""
    import json as _json
    i = text.find("load('d',")
    i = text.find("[", i) if i >= 0 else -1
    if i < 0:
        return None
    depth, instr, esc = 0, False, False
    for j in range(i, len(text)):
        c = text[j]
        if instr:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                instr = False
        elif c == '"':
            instr = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return _json.loads(text[i:j + 1])
                except Exception:
                    return None
    return None


def _json_search(query: str, kl: str, df: str, limit: int | None) -> list[dict]:
    """Fetch results from DuckDuckGo's free JSON API. Returns organic rows (no HTML). [] on failure
    so the caller falls back to the HTML endpoint."""
    vqd = _vqd(query)
    if not vqd:
        return []
    rows, pos, s = [], 0, 0
    for _page in range(MAX_PAGES):
        params = {"q": query, "kl": kl, "l": kl, "vqd": vqd, "s": str(s)}
        if df:
            params["df"] = df
        arr = None
        for _attempt in range(4):
            r = yp_us.pooled_get(LINKS, params, timeout=20)
            if r is not None and r.status_code == 200 and '"u":' in r.text:
                arr = _extract_array(r.text)
                if arr is not None:
                    break
        if not arr:
            break
        results = [it for it in arr if isinstance(it, dict) and it.get("u") and it.get("t")]
        if not results:
            break
        for it in results:
            pos += 1
            rows.append({"query": query, "link": it.get("u", ""),
                         "title": _strip_tags(it.get("t", "")),
                         "description": _strip_tags(it.get("a", "")),
                         "question": "", "position": pos, "type": "organic"})
        s += len(results)
        if limit and len(rows) >= limit:
            break
    return rows


def search_sync(query: str, limit: int | None = None, date_range: str = "",
                region: str = "us", language: str = "en") -> list[dict]:
    """Scrape DuckDuckGo results for one query via the proxy pool. `limit` caps the number of
    result rows (blank/None = fetch all available, up to MAX_PAGES); e.g. limit=10 -> 10 rows."""
    query = (query or "").strip()
    if not query:
        return []
    df = DATE_MAP.get((date_range or "").strip().lower(), "")
    kl = _kl(region, language)
    # PRIMARY: DuckDuckGo's free JSON API. Fall back to the HTML endpoint only if it returns nothing.
    rows = _json_search(query, kl, df, limit)
    pos, s = 0, 0
    for _page in (range(MAX_PAGES) if not rows else []):
        params = {"q": query, "kl": kl}
        if df:
            params["df"] = df
        if s:
            params["s"] = str(s)
        r = None
        for _attempt in range(4):  # free proxies are flaky — retry a few before giving up
            resp = yp_us.pooled_get(DDG, params, timeout=20)
            if resp is not None and resp.status_code == 200 and "result__a" in resp.text:
                r = resp
                break
        if r is None:
            break
        page = _parse(r.text, query, pos)
        organic = [x for x in page if x["type"] in ("organic", "ad", "news")]
        rows += page
        if not organic:
            break
        pos = max((x["position"] for x in organic), default=pos)
        s += len(organic)
        if limit and sum(1 for x in rows if x["type"] != "related") >= limit:
            break
    # trim organic rows to exactly `limit` (keep related-search rows as extras)
    if limit:
        out, n = [], 0
        for x in rows:
            if x["type"] == "related":
                out.append(x)
            elif n < limit:
                out.append(x)
                n += 1
        rows = out
    # People Also Ask: DuckDuckGo has none, so synthesize related questions from autocomplete and
    # fill the `question` column — one on each organic row (round-robin) PLUS dedicated PAA rows.
    organic = [r for r in rows if r["type"] in ("organic", "ad", "news")]
    questions = _people_also_ask(query) if organic else []
    if questions:
        for i, r in enumerate(organic):
            r["question"] = questions[i % len(questions)]
        for q in questions:
            rows.append({"query": query, "link": "", "title": "", "description": "",
                         "question": q, "position": 0, "type": "people_also_ask"})
    # translate title + description (+ question) into the selected language (Google Translate via proxy)
    _translate_rows(rows, language)
    return rows


async def search(query: str, limit: int | None = None, date_range: str = "",
                 region: str = "us", language: str = "en") -> list[dict]:
    return await asyncio.to_thread(search_sync, query, limit, date_range, region, language)


async def run_job(job_id: str, queries: list[str], limit: int | None, date_range: str,
                  region: str, language: str) -> None:
    """Background task: scrape each query (via the proxy pool) and store the result rows."""
    from datetime import datetime
    from .db import jobs, gresults
    total = 0
    try:
        for q in queries:
            rows = await search(q, limit, date_range, region, language)
            for r in rows:
                r["job_id"] = job_id
            if rows:
                await gresults.insert_many(rows)
                # count only actual results — not the synthesized People-Also-Ask / related rows
                total += sum(1 for r in rows if r["type"] in ("organic", "ad", "news"))
            await jobs.update_one({"job_id": job_id}, {"$set": {"total_scraped": total}})
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "done", "total_scraped": total, "finished_at": datetime.utcnow()}})
    except Exception as e:
        await jobs.update_one({"job_id": job_id}, {"$set": {
            "status": "error", "error": str(e), "finished_at": datetime.utcnow()}})
