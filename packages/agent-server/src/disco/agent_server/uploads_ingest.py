"""Text-upload → Passage chunks — v1 scope: .txt / .md / .csv only.

Converts raw upload bytes into ``ExtractedDoc``/``Passage`` lists that the
upload route can ingest into the per-conversation upload corpus
(``upload:{cid}``). The chunker is intentionally simple — paragraph-split for
prose files, row-per-chunk for CSV — so it has no external dependencies and
works fully offline.

PDF / binary types are explicitly excluded from v1 ingestion; they still land
in the sandbox ``uploads/`` dir for the agent to read, but they are NOT indexed
as retrievable Passages here.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import PurePosixPath

from disco.retrieval.models import ExtractedDoc, Passage

# Extensions we accept for corpus ingestion in v1.
_INGESTIBLE = frozenset({".txt", ".md", ".csv"})

# Maximum characters per prose paragraph chunk — long paragraphs are split at
# sentence boundaries to keep passages to a readable grain (~400 chars).
_MAX_CHUNK_CHARS = 600


def parse_upload_to_doc(filename: str, data: bytes, cid: str) -> ExtractedDoc | None:
    """Convert upload bytes into an ``ExtractedDoc`` with embedded Passages.

    Returns ``None`` for non-text extensions (PDF, .zip, etc.) so the caller
    can skip corpus ingestion without an error (the file still lands in the
    sandbox).  Returns an ``ExtractedDoc`` with ``fetched_ok=True`` for
    .txt/.md/.csv content, even when the passage list is empty (e.g. empty
    file).

    The ``source_url`` on each passage is ``upload://{cid}/{filename}`` so
    the UI can distinguish upload-sourced citations from live-web citations.
    ``corpus_id`` is set to ``f"upload:{cid}"`` by the ingestion call-site
    (``DefaultCorpusService.ingest`` copies it onto each passage), not here.
    """
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in _INGESTIBLE:
        return None

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    source_url = f"upload://{cid}/{filename}"
    source_title = filename

    if ext == ".csv":
        passages = _csv_passages(text, source_url, source_title, filename)
    else:  # .txt / .md
        passages = _prose_passages(text, source_url, source_title, filename)

    return ExtractedDoc(
        url=source_url,
        title=source_title,
        content=text,
        passages=passages,
        fetched_ok=True,
    )


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
