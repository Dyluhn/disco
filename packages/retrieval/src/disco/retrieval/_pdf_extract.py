"""Bounded text extraction from an original PDF response, without a browser."""

from __future__ import annotations

import io
from collections.abc import Callable

from disco.core.host_egress import GuardedResponse
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from ._extraction_text import corrupted_text
from ._pdf_warnings import annotated_pdf_text, pdf_warnings
from .models import ExtractedDoc, Passage

MAX_PDF_PAGES = 200
MAX_PDF_PAGE_STREAM_BYTES = 5_000_000
MAX_PDF_TEXT_CHARS = 2_000_000


class _PdfLimit(ValueError):
    """A document exceeds the bundled text reader's finite work allowance."""


def _pdf_text(reader: PdfReader) -> str:
    if len(reader.pages) > MAX_PDF_PAGES:
        raise _PdfLimit(f"more than {MAX_PDF_PAGES} pages")
    pages: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages, 1):
        stream = page.get_contents()
        if stream is not None and len(stream.get_data()) > MAX_PDF_PAGE_STREAM_BYTES:
            raise _PdfLimit(f"page {index} exceeds {MAX_PDF_PAGE_STREAM_BYTES} decoded bytes")
        text = page.extract_text().strip()
        if text:
            rendered = f"## PDF page {index}\n\n{text}"
            total += len(rendered) + (2 if pages else 0)
            if total > MAX_PDF_TEXT_CHARS:
                raise _PdfLimit(f"more than {MAX_PDF_TEXT_CHARS} extracted characters")
            pages.append(rendered)
    return "\n\n".join(pages)


def _failed(url: str, reason: str) -> ExtractedDoc:
    return ExtractedDoc(
        url=url,
        title=url,
        content="",
        fetched_ok=False,
        status="error",
        error=(
            f"PDF extraction unavailable: {reason}. No partial source was admitted. "
            "Use a readable HTML version or a smaller, unencrypted text PDF of the primary work."
        ),
    )


def pdf_document(
    url: str,
    response: GuardedResponse,
    *,
    chunk: Callable[[str, str, str], list[Passage]],
) -> ExtractedDoc | None:
    """Parse MIME/magic-identified PDFs; None preserves the caller's HTML path.

    The caller owns the guarded, byte-limited fetch and runs parsing off its
    event loop. Read every page within finite bounds or admit no text. This is
    text extraction, not OCR or interpretation of figures. Parser warnings
    remain explicit in the source text; they cannot imply complete extraction.
    """
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime != "application/pdf" and not response.content.startswith(b"%PDF-"):
        return None
    if response.status_code != 200:
        return _failed(url, f"original source returned HTTP {response.status_code}")
    try:
        with pdf_warnings() as warnings:
            reader = PdfReader(io.BytesIO(response.content))
            if reader.is_encrypted:
                return _failed(url, "document is encrypted")
            content = _pdf_text(reader)
            title = str(reader.metadata.title or url) if reader.metadata else url
    except _PdfLimit as exc:
        return _failed(url, str(exc))
    except (PyPdfError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return _failed(url, f"malformed or unreadable document ({type(exc).__name__})")
    if not content or corrupted_text(content):
        return _failed(url, "no readable text layer; a scanned or image-only source may need OCR")
    content = annotated_pdf_text(content, warnings)
    if len(content) > MAX_PDF_TEXT_CHARS:
        return _failed(
            url, f"more than {MAX_PDF_TEXT_CHARS} extracted characters including warnings"
        )
    passages = chunk(url, title, content)
    if not passages:
        return _failed(url, "no usable text passages")
    return ExtractedDoc(
        url=url,
        title=title,
        content=content,
        passages=passages,
        fetched_ok=True,
        status="ok",
    )
