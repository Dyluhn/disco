"""Bounded, provenance-preserving extraction of text PDFs."""

from __future__ import annotations

import io

import pytest
from disco.core.host_egress import GuardedResponse
from disco.retrieval import _pdf_extract
from disco.retrieval._pdf_extract import pdf_document
from disco.retrieval.models import Passage
from pypdf import PdfReader, PdfWriter, _page
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

URL = "https://example.test/paper.pdf"


def _pdf_bytes(*pages: str, title: str | None = None, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({escaped}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    if title:
        writer.add_metadata({"/Title": title})
    if encrypted:
        writer.encrypt("correct horse battery staple")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _response(content: bytes, mime: str = "application/pdf") -> GuardedResponse:
    return GuardedResponse(URL, 200, {"content-type": mime}, content)


def _chunk(url: str, title: str, content: str) -> list[Passage]:
    return [Passage(id="pdf_p0", source_url=url, source_title=title, text=content)]


def test_pdf_document_preserves_metadata_and_all_page_qualifications() -> None:
    first = "The measured benefit applies under the tested deployment conditions."
    second = "The benefit does not generalize when the deployment conditions change."
    result = pdf_document(
        URL,
        _response(_pdf_bytes(first, second, title="Measured deployment study")),
        chunk=_chunk,
    )
    assert result is not None and result.fetched_ok
    assert result.url == URL
    assert result.title == "Measured deployment study"
    assert first in result.content and second in result.content
    assert result.passages[0].source_url == URL
    assert result.passages[0].source_title == result.title


def test_pdf_document_requires_pdf_mime_or_magic() -> None:
    content = _pdf_bytes("This is a real PDF despite a misleading response header.")
    result = pdf_document(URL, _response(content, "text/html"), chunk=_chunk)
    assert result is not None and result.fetched_ok
    assert pdf_document(URL, _response(b"ordinary HTML", "text/html"), chunk=_chunk) is None


@pytest.mark.parametrize(
    "content",
    [b"", b"%PDF-1.7\nnot a valid document"],
)
def test_pdf_document_returns_explicit_failure_for_empty_or_malformed(content: bytes) -> None:
    result = pdf_document(URL, _response(content), chunk=_chunk)
    assert result is not None
    assert not result.fetched_ok and not result.passages and not result.content
    assert result.status == "error"
    assert result.error and "No partial source was admitted" in result.error


def test_pdf_document_rejects_encrypted_documents() -> None:
    result = pdf_document(
        URL, _response(_pdf_bytes("Secret source text", encrypted=True)), chunk=_chunk
    )
    assert result is not None
    assert not result.fetched_ok
    assert result.error and "encrypted" in result.error


def test_pdf_document_rejects_empty_text_layer() -> None:
    result = pdf_document(URL, _response(_pdf_bytes("")), chunk=_chunk)
    assert result is not None
    assert not result.fetched_ok
    assert result.error and "no readable text layer" in result.error


def test_pdf_document_refuses_page_stream_over_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pdf_extract, "MAX_PDF_PAGE_STREAM_BYTES", 8)
    result = pdf_document(
        URL, _response(_pdf_bytes("stream content is over the test bound")), chunk=_chunk
    )
    assert result is not None
    assert not result.fetched_ok and not result.content and not result.passages
    assert result.error and "No partial source was admitted" in result.error


def test_pdf_document_refuses_page_count_over_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pdf_extract, "MAX_PDF_PAGES", 1)
    result = pdf_document(URL, _response(_pdf_bytes("first page", "second page")), chunk=_chunk)
    assert result is not None
    assert not result.fetched_ok and "more than 1 pages" in (result.error or "")


def test_pdf_document_refuses_output_over_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pdf_extract, "MAX_PDF_TEXT_CHARS", 20)
    result = pdf_document(
        URL,
        _response(_pdf_bytes("This page contains more extracted text than allowed.")),
        chunk=_chunk,
    )
    assert result is not None
    assert not result.fetched_ok and not result.content and not result.passages
    assert result.error and "No partial source was admitted" in result.error


def _pdf_with_repeated_form() -> bytes:
    writer = PdfWriter()
    writer.add_page(
        PdfReader(io.BytesIO(_pdf_bytes("A readable paragraph precedes the figure."))).pages[0]
    )
    page = writer.pages[0]
    form = DecodedStreamObject()
    form.set_data(b"BT /F1 12 Tf 50 650 Td (Text in a repeated figure.) Tj ET")
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = ArrayObject([NumberObject(x) for x in [0, 0, 612, 792]])
    form[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): page["/Resources"]["/Font"]}
    )
    page["/Resources"][NameObject("/XObject")] = DictionaryObject(
        {NameObject("/Fm"): writer._add_object(form)}
    )
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data() + b"\n/Fm Do /Fm Do /Fm Do")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_library_omission_warning_reaches_the_source_and_citable_passage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_page, "MAX_XFORM_INVOCATIONS_PER_EXTRACTION", 1)
    result = pdf_document(URL, _response(_pdf_with_repeated_form()), chunk=_chunk)
    assert result is not None and result.fetched_ok
    assert result.content.startswith("[PDF TEXT EXTRACTION NOTICE")
    assert "generated by Disco, not part of the source" in result.content
    assert "further form content is skipped" in result.content
    assert "A readable paragraph precedes the figure." in result.content
    assert result.passages[0].text == result.content
    assert result.passages[0].source_url == URL


def test_pdf_warning_provenance_also_obeys_the_output_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_page, "MAX_XFORM_INVOCATIONS_PER_EXTRACTION", 1)
    monkeypatch.setattr(_pdf_extract, "MAX_PDF_TEXT_CHARS", 200)
    result = pdf_document(URL, _response(_pdf_with_repeated_form()), chunk=_chunk)
    assert result is not None and not result.fetched_ok
    assert not result.content and not result.passages
    assert "including warnings" in (result.error or "")
