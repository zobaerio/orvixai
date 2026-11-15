"""Web research with honest source labelling.

Fetches real pages over HTTP(S) with size/time bounds (same discipline as
the existing vision code). Claims found in >= 2 independent sources are
labelled "corroborated"; everything else is labelled "single-source /
uncertain". Nothing is presented as verified fact from one source alone.
Search uses DuckDuckGo's public HTML endpoint; if the network blocks it,
the error is reported plainly.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional

MAX_BYTES = 1024 * 1024
TIMEOUT = 20.0


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        text = data.strip()
        if text:
            self.parts.append(text)


@dataclass
class Source:
    url: str
    title: str
    text: str
    fetched_at: float = field(default_factory=time.time)


def fetch_url(url: str, *, timeout: float = TIMEOUT) -> Source:
    if not re.match(r"^https?://", url or ""):
        raise ValueError(f"only http(s) URLs are supported: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ORVIX-AI-Research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]
    parser = _TextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return Source(url=url, title=parser.title.strip(), text=text)


def search_web(query: str, max_results: int = 5, *, timeout: float = TIMEOUT) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "ORVIX-AI-Research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        page = resp.read(MAX_BYTES).decode("utf-8", errors="replace")
    results = []
    for match in re.finditer(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        href, title = match.group(1), re.sub(r"<[^>]+>", "", match.group(2))
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        results.append({"url": html.unescape(href), "title": html.unescape(title).strip()})
        if len(results) >= max_results:
            break
    return results


@dataclass
class ResearchReport:
    query: str
    sources: list
    findings: list  # {claim, label, source_urls}

    def to_text(self) -> str:
        lines = [f"Research: {self.query}", f"Sources consulted: {len(self.sources)}"]
        for f in self.findings:
            lines.append(f"- [{f['label']}] {f['claim']} ({', '.join(f['source_urls'])})")
        return "\n".join(lines)


def _claims(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or ""))
            if 40 <= len(s.strip()) <= 300]


def research(query: str, *, urls: Optional[list] = None, use_search: bool = True,
             max_sources: int = 5) -> ResearchReport:
    targets = list(urls or [])
    errors: list[str] = []
    if use_search and len(targets) < max_sources:
        try:
            for hit in search_web(query, max_results=max_sources):
                if hit["url"] not in targets:
                    targets.append(hit["url"])
        except Exception as exc:
            errors.append(f"web search failed: {exc.__class__.__name__}: {exc}")
    sources: list[Source] = []
    for url in targets[:max_sources]:
        try:
            sources.append(fetch_url(url))
        except Exception as exc:
            errors.append(f"fetch failed for {url}: {exc.__class__.__name__}")
    if not sources:
        raise RuntimeError("research produced no sources. " + ("; ".join(errors) if errors else "No URLs given."))
    # Corroboration: a claim's key terms appearing in another source's text.
    findings = []
    seen: set[str] = set()
    for src in sources:
        for claim in _claims(src.text)[:20]:
            key = claim[:60].lower()
            if key in seen:
                continue
            seen.add(key)
            terms = {w for w in re.findall(r"[a-zA-Z]{5,}", claim.lower())}
            corroborated_by = [s.url for s in sources if s.url != src.url
                               and terms and sum(1 for t in terms if t in s.text.lower()) >= max(2, len(terms) // 3)]
            findings.append({"claim": claim,
                             "label": "corroborated" if corroborated_by else "single-source / uncertain",
                             "source_urls": [src.url] + corroborated_by})
    findings.sort(key=lambda f: 0 if f["label"] == "corroborated" else 1)
    return ResearchReport(query=query, sources=sources, findings=findings[:30])
