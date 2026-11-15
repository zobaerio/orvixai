"""Document intelligence using only the standard library.

Real extraction for TXT/MD/code/JSON, CSV (with column stats), XLSX and DOCX
(both are ZIP+XML formats), and PDF files that contain an extractable text
layer (FlateDecode streams). Scanned/image-only PDFs honestly report that no
text layer was found instead of inventing content.
"""
from __future__ import annotations

import csv
import difflib
import io
import json
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

MAX_FILE_BYTES = 20 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".py", ".js", ".ts", ".json", ".log", ".html", ".xml", ".yaml", ".yml", ".ini"}


class DocumentError(RuntimeError):
    pass


def _read(path: Path) -> bytes:
    if not path.exists():
        raise DocumentError(f"file not found: {path}")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(f"file too large ({len(data)} bytes > {MAX_FILE_BYTES})")
    return data


def _xlsx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError("invalid XLSX (not a zip archive)") from exc
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        shared = ["".join(t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
                  for si in root.findall("m:si", ns)]
    sheets = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
    out: list[str] = []
    for sheet in sorted(sheets):
        root = ElementTree.fromstring(zf.read(sheet))
        for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
            cells = []
            for c in row.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                v = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                if v is None or v.text is None:
                    cells.append("")
                elif c.get("t") == "s":
                    try:
                        cells.append(shared[int(v.text)])
                    except (ValueError, IndexError):
                        cells.append(v.text)
                else:
                    cells.append(v.text)
            out.append("\t".join(cells))
    return "\n".join(out)


def _docx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        xml = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DocumentError("invalid DOCX (word/document.xml missing)") from exc
    root = ElementTree.fromstring(xml)
    paras = []
    for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        paras.append("".join(t.text or "" for t in
                             p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")))
    return "\n".join(paras)


def _pdf_text(data: bytes) -> str:
    texts: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = match.group(1)
        try:
            decoded = zlib.decompress(raw)
        except zlib.error:
            decoded = raw
        for tj in re.findall(rb"\((?:[^()\\]|\\.)*\)\s*Tj", decoded):
            s = tj[1:tj.rindex(b")")]
            texts.append(s.decode("latin-1").encode().decode("unicode_escape", errors="ignore"))
        for tj in re.findall(rb"\[(.*?)\]\s*TJ", decoded, re.S):
            parts = re.findall(rb"\((?:[^()\\]|\\.)*\)", tj)
            texts.append("".join(p[1:-1].decode("latin-1") for p in parts))
    return "\n".join(texts)


def extract_text(path: str | Path) -> str:
    p = Path(path)
    data = _read(p)
    suffix = p.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return data.decode("utf-8", errors="replace")
    if suffix == ".csv":
        return data.decode("utf-8", errors="replace")
    if suffix == ".xlsx":
        return _xlsx_text(data)
    if suffix == ".docx":
        return _docx_text(data)
    if suffix == ".pdf":
        text = _pdf_text(data)
        if not text.strip():
            raise DocumentError("no extractable text layer in this PDF (scanned/image PDF needs OCR, not available)")
        return text
    raise DocumentError(f"unsupported document type: {suffix or '(none)'}")


def csv_stats(path: str | Path) -> dict:
    text = extract_text(path)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {"rows": 0, "columns": 0}
    header, body = rows[0], rows[1:]
    numeric: dict[str, dict] = {}
    for idx, name in enumerate(header):
        values = []
        for row in body:
            if idx < len(row):
                try:
                    values.append(float(row[idx]))
                except ValueError:
                    pass
        if values:
            numeric[name] = {"count": len(values), "min": min(values), "max": max(values),
                             "mean": round(sum(values) / len(values), 4)}
    return {"rows": len(body), "columns": len(header), "header": header, "numeric_columns": numeric}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "")) if len(s.strip()) > 20]


def summarize_extractive(text: str, max_sentences: int = 5) -> str:
    """Real extractive summary: rank sentences by word frequency."""
    sentences = _sentences(text)
    if not sentences:
        return (text or "")[:500]
    freq: dict[str, int] = {}
    for word in re.findall(r"[a-zA-Z]{3,}", text.lower()):
        freq[word] = freq.get(word, 0) + 1
    ranked = sorted(enumerate(sentences),
                    key=lambda ix: -sum(freq.get(w, 0) for w in re.findall(r"[a-zA-Z]{3,}", ix[1].lower())))
    chosen = sorted(ix for ix, _s in ranked[:max_sentences])
    return " ".join(sentences[i] for i in chosen)


def answer_question(text: str, question: str) -> str:
    """Sentence-retrieval QA over extracted document text."""
    q_words = {w for w in re.findall(r"[a-zA-Z]{3,}", (question or "").lower())}
    if not q_words:
        return "Please ask a question with searchable keywords."
    scored = [(sum(1 for w in q_words if w in s.lower()), s) for s in _sentences(text)]
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] == 0:
        return "I could not find anything in the document that matches that question."
    return " ".join(s for score, s in scored[:3] if score > 0)


def compare_documents(path_a: str | Path, path_b: str | Path) -> dict:
    a, b = extract_text(path_a), extract_text(path_b)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    a_lines, b_lines = set(a.splitlines()), set(b.splitlines())
    return {"similarity": round(ratio, 4),
            "only_in_a": sorted(x for x in a_lines - b_lines if x.strip())[:50],
            "only_in_b": sorted(x for x in b_lines - a_lines if x.strip())[:50],
            "chars_a": len(a), "chars_b": len(b)}
