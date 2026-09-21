"""Minimal live web/documentation scraping hook.

Uses requests + the stdlib HTMLParser (no bs4 dependency) to fetch a URL and
return clean text plus any code samples, so FRIDAY's tool loop can answer
questions about live docs/APIs without opening a browser.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser


TEXT_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "figcaption"}
CODE_TAGS = {"pre", "code"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "header", "nav", "footer"}
_MAX_TEXT = 6000
_MAX_CODE = 4000


class _TextScraper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.current_code = []
        self.in_code = False
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        if self.skip_depth:
            return
        if tag in CODE_TAGS and not self.in_code:
            self.in_code = True
            self.current_code = []
        elif tag in TEXT_TAGS:
            self.blocks.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        if self.skip_depth:
            return
        if tag in CODE_TAGS and self.in_code:
            self.in_code = False
            snippet = "".join(self.current_code).strip()
            if snippet:
                self.blocks.append("\n```\n" + snippet[: _MAX_CODE] + "\n```\n")
            self.current_code = []

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_code:
            self.current_code.append(data)
        else:
            self.blocks.append(data)


def _clean_text(raw):
    text = re.sub(r"[ \t]+", " ", raw)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def web_fetch(url, timeout=12.0, max_chars=_MAX_TEXT):
    """Fetch ``url`` and return {ok, error, title, text, final_url}."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    try:
        import requests

        response = requests.get(url, timeout=timeout, headers={"User-Agent": "FRIDAY/1.0"}, allow_redirects=True)
        response.raise_for_status()
    except Exception as error:
        return {"ok": False, "error": str(error), "title": "", "text": "", "final_url": url}
    scraper = _TextScraper()
    try:
        scraper.feed(response.text)
    except Exception:
        pass
    body = _clean_text("".join(scraper.blocks))
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = html.unescape(title_match.group(1)).strip()
    if len(body) > max_chars:
        body = body[:max_chars] + "\n… (truncated)"
    return {"ok": True, "error": "", "title": title, "text": body, "final_url": response.url}


_BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_KNOWN_DOCS = {
    "fastapi": "https://fastapi.tiangolo.com/",
    "flask": "https://flask.palletsprojects.com/",
    "django": "https://docs.djangoproject.com/",
    "requests": "https://requests.readthedocs.io/",
    "httpx": "https://www.python-httpx.org/",
    "numpy": "https://numpy.org/doc/stable/",
    "pandas": "https://pandas.pydata.org/docs/",
    "python": "https://docs.python.org/3/",
    "opencv": "https://docs.opencv.org/",
    "pytorch": "https://pytorch.org/docs/stable/",
    "ffmpeg": "https://ffmpeg.org/documentation.html",
    "unit testing": "https://docs.python.org/3/library/unittest.html",
    "fastapi": "https://fastapi.tiangolo.com/",
}


def _query_tokens(query):
    return {token for token in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(token) >= 3}


def _canonical_doc_url(query):
    tokens = _query_tokens(query)
    best = None
    best_score = 0
    for name, url in _KNOWN_DOCS.items():
        name_tokens = set(re.split(r"[^a-z0-9]+", name.lower()))
        score = len(tokens & name_tokens)
        if score and score > best_score:
            best, best_score = url, score
    return best


def _b_algo_blocks(text):
    """Extract complete ``<li class="b_algo">`` blocks, handling nested <li>."""
    blocks = []
    for match in re.finditer(r"<li\s+class=\"b_algo\"", text):
        i = match.end()
        depth = 1
        remaining = 0
        while depth and i < len(text) and remaining < 20000:
            open_i = text.find("<li", i)
            close_i = text.find("</li>", i)
            if close_i == -1:
                break
            if open_i != -1 and open_i < close_i:
                depth += 1
                i = open_i + 3
            else:
                depth -= 1
                i = close_i + 5
            remaining += 5
        blocks.append(text[match.start():i])
    return blocks


def _bing_results(query, limit=4, timeout=12.0):
    """Scrape Bing organic results into {url, title, snippet} dicts."""
    import requests

    response = requests.get(
        "https://www.bing.com/search",
        params={"q": query, "setlang": "en", "cc": "US"},
        timeout=timeout,
        headers={"User-Agent": _BING_UA},
    )
    response.raise_for_status()
    tokens = _query_tokens(query)
    candidates = []
    for block in _b_algo_blocks(response.text):
        anchor = re.search(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.DOTALL)
        if not anchor:
            continue
        url = html.unescape(anchor.group(1))
        if url in {item[0] for item in candidates}:
            continue
        title = _clean_text(html.unescape(re.sub(r"<[^>]+>", "", anchor.group(2))))
        snippet = ""
        paragraph = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        if paragraph:
            snippet = _clean_text(html.unescape(re.sub(r"<[^>]+>", "", paragraph.group(1))))
        if not snippet:
            continue
        score = len(tokens & _query_tokens(title + " " + snippet))
        if score:
            candidates.append((score, {"url": url, "title": title, "snippet": snippet}))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in candidates[:limit]]


def search_and_fetch(query, timeout=15.0, max_chars=_MAX_TEXT):
    """Search ``query`` and fetch the top results for a live answer.

    Uses a canonical-docs shortcut first for well-known projects, then Bing
    organic results.  Always truthfully reports failures as non-ok.
    """
    canonical = _canonical_doc_url(query)
    if canonical:
        fetched = web_fetch(canonical, timeout=timeout, max_chars=max_chars)
        if fetched["ok"]:
            return {"ok": True, "error": "", "results": [{
                "url": fetched["final_url"],
                "title": fetched["title"] or canonical,
                "ok": True,
                "text": fetched["text"],
            }]}
    try:
        results = _bing_results(query, timeout=timeout)
    except Exception as error:
        return {"ok": False, "error": str(error), "results": []}
    if not results:
        return {"ok": False, "error": "no relevant results", "results": []}
    enriched = []
    for item in results:
        fetched = web_fetch(item["url"], timeout=timeout, max_chars=max_chars)
        enriched.append({**item, "ok": fetched["ok"], "text": fetched["text"]})
    return {"ok": True, "error": "", "results": enriched}