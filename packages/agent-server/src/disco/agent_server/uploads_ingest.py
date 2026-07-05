"""Upload bytes → Passage chunks.

Converts raw upload bytes into ``ExtractedDoc``/``Passage`` lists that the
upload route can ingest into the per-conversation upload corpus
(``upload:{cid}``) or a Space corpus. The chunker is intentionally simple —
paragraph-split for prose files, row-per-chunk for CSV — so it works fully
offline. PDF extraction is best-effort with optional libraries when installed,
falling back to uncompressed text snippets.
"""

from __future__ import annotations

import csv
import io
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath

from disco.retrieval.models import ExtractedDoc, Passage

# Extensions we accept for corpus ingestion.
_INGESTIBLE = frozenset({".txt", ".md", ".csv", ".html", ".htm", ".pdf"})

# Maximum characters per prose paragraph chunk — long paragraphs are split at
# sentence boundaries to keep passages to a readable grain (~400 chars).
_MAX_CHUNK_CHARS = 600


def parse_upload_to_doc(
    filename: str,
    data: bytes,
    cid: str,
    *,
    source_scheme: str = "upload",
) -> ExtractedDoc | None:
    """Convert upload bytes into an ``ExtractedDoc`` with embedded Passages.

    Returns ``None`` for unsupported extensions so the caller can skip corpus
    ingestion without an error (the file may still land in the sandbox or Space
    registry). Returns an ``ExtractedDoc`` with ``fetched_ok=True`` for supported
    content, even when the passage list is empty (e.g. empty file or image-only
    PDF).

    The ``source_url`` on each passage is ``{source_scheme}://{cid}/{filename}``
    so the UI can distinguish upload/space-sourced citations from live-web
    citations.
    ``corpus_id`` is set to ``f"upload:{cid}"`` by the ingestion call-site
    (``DefaultCorpusService.ingest`` copies it onto each passage), not here.
    """
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in _INGESTIBLE:
        return None

    text = _decode_for_extension(ext, data)
    if text is None:
        return None

    source_url = f"{source_scheme}://{cid}/{filename}"
    source_title = filename

    if ext == ".csv":
        passages = _csv_passages(text, source_url, source_title, filename)
    elif ext in {".html", ".htm"}:
        text = _html_to_text(text)
        passages = _prose_passages(text, source_url, source_title, filename)
    else:  # .txt / .md / .pdf
        passages = _prose_passages(text, source_url, source_title, filename)

    return ExtractedDoc(
        url=source_url,
        title=source_title,
        content=text,
        passages=passages,
        fetched_ok=True,
    )


def _decode_for_extension(ext: str, data: bytes) -> str | None:
    if ext == ".pdf":
        return _pdf_to_text(data)
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag.lower() in {"p", "div", "section", "article", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag.lower() in {"p", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)
            self.parts.append(" ")


def _html_to_text(text: str) -> str:
    parser = _TextHTMLParser()
    try:
        parser.feed(text)
        raw = "".join(parser.parts)
    except Exception:  # noqa: BLE001
        raw = re.sub(r"<[^>]+>", " ", text)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _pdf_to_text(data: bytes) -> str:
    text = _pdf_to_text_optional(data)
    if text.strip():
        return text
    # Lightweight fallback for simple/uncompressed PDFs: pull literal strings.
    decoded = data.decode("latin-1", errors="ignore")
    chunks = re.findall(r"\(([^()]*)\)\s*Tj", decoded, flags=re.DOTALL)
    if not chunks:
        chunks = re.findall(r"\(([^()]*)\)", decoded, flags=re.DOTALL)
    cleaned = []
    for chunk in chunks:
        chunk = chunk.replace(r"\(", "(").replace(r"\)", ")")
        chunk = chunk.replace(r"\n", " ").replace(r"\r", " ").replace(r"\t", " ")
        chunk = re.sub(r"\\[0-7]{1,3}", " ", chunk)
        if re.search(r"[A-Za-z0-9]", chunk):
            cleaned.append(chunk)
    return "\n\n".join(cleaned)


def _pdf_to_text_optional(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[reportMissingImports]

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:  # noqa: BLE001
        pass
    try:
        from pdfminer.high_level import extract_text  # type: ignore[reportMissingImports]

        return str(extract_text(io.BytesIO(data)))
    except Exception:  # noqa: BLE001
        return ""


# ── prose chunker ─────────────────────────────────────────────────────────────


def _prose_passages(
    text: str,
    source_url: str,
    source_title: str,
    filename: str,
) -> list[Passage]:
    """Split on blank lines; sub-split overly long paragraphs at sentences."""
    # Normalise Windows/Mac line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_chunks: list[str] = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= _MAX_CHUNK_CHARS:
            raw_chunks.append(para)
        else:
            # Sub-split at sentence boundaries (period / excl / question followed
            # by whitespace or end-of-string).
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf = ""
            for sent in sentences:
                if buf and len(buf) + len(sent) + 1 > _MAX_CHUNK_CHARS:
                    raw_chunks.append(buf.strip())
                    buf = sent
                else:
                    buf = (buf + " " + sent).strip() if buf else sent
            if buf.strip():
                raw_chunks.append(buf.strip())

    passages: list[Passage] = []
    char_pos = 0
    for i, chunk in enumerate(raw_chunks):
        start = text.find(chunk, char_pos)
        end = start + len(chunk) if start >= 0 else None
        char_pos = end or char_pos
        passages.append(
            Passage(
                id=f"up_{_slug(filename)}_{i}",
                source_url=source_url,
                source_title=source_title,
                text=chunk,
                char_start=start if start >= 0 else None,
                char_end=end,
            )
        )
    return passages


# ── CSV chunker ───────────────────────────────────────────────────────────────


def _csv_passages(
    text: str,
    source_url: str,
    source_title: str,
    filename: str,
) -> list[Passage]:
    """One passage per CSV row (header + row as a key=value description)."""
    try:
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            return []
        passages: list[Passage] = []
        for i, row in enumerate(reader):
            # Render as "key: value | key: value" — small, readable, citable.
            parts = [f"{k}: {v}" for k, v in row.items() if v is not None and str(v).strip()]
            if not parts:
                continue
            chunk = " | ".join(parts)
            passages.append(
                Passage(
                    id=f"up_{_slug(filename)}_r{i}",
                    source_url=source_url,
                    source_title=source_title,
                    text=chunk,
                )
            )
        return passages
    except csv.Error:
        # Not a valid CSV → treat as plain text.
        return _prose_passages(text, source_url, source_title, filename)


def _slug(filename: str) -> str:
    """Stable alphanumeric slug from a filename (no extension)."""
    stem = PurePosixPath(filename).stem
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")[:24]
