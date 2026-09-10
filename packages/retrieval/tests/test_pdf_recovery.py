"""A browser's failed PDF request can recover original evidence exactly once."""

import io
import json

import httpx
import pytest
from disco.core.host_egress import EgressDenied, GuardedResponse
from disco.retrieval import _pdf_recovery, _transport_retry
from disco.retrieval._crawl4ai import Crawl4aiExtractionProvider
from disco.retrieval._pdf_recovery import recover_pdf_sources
from disco.retrieval._retrieval_cache import RetrievalCache
from disco.retrieval.bundled_providers import chunk_passages
from disco.retrieval.models import ExtractedDoc
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

URL = "https://example.com/download/original-study"
LATE = "The measured benefit applies only under the tested deployment conditions."


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    for text in ["Original measured study with a useful first page of evidence.", LATE]:
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
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": "Original measured study"})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _failure(url: str = URL, error: str = "HTTP 500", status: str = "error") -> ExtractedDoc:
    return ExtractedDoc(
        url=url, title=url, content="", fetched_ok=False, status=status, error=error
    )


async def test_failed_crawler_recovers_full_pdf_once_then_uses_normal_cache(tmp_path, monkeypatch):
    crawler_calls, original_calls = [], []

    def crawler(request):
        crawler_calls.append(json.loads(request.content))
        return httpx.Response(500, text="browser could not render the download")

    async def original(url, **kwargs):
        original_calls.append((url, kwargs))
        return GuardedResponse(url, 200, {"content-type": "application/pdf"}, _pdf_bytes())

    monkeypatch.setattr(_transport_retry, "validate_untrusted_url", lambda url: None)
    monkeypatch.setattr(_pdf_recovery, "guarded_get", original)
    provider = Crawl4aiExtractionProvider(
        "http://crawler",
        transport=httpx.MockTransport(crawler),
        cache=RetrievalCache(tmp_path / "cache.sqlite"),
    )
    result = await provider.extract(URL)
    assert result.fetched_ok and result.status == "ok"
    assert result.url == URL and result.title == "Original measured study"
    assert LATE in result.content
    assert any(LATE in passage.text for passage in result.passages)
    assert all(passage.source_url == URL for passage in result.passages)
    assert len(original_calls) == 1
    assert original_calls[0][1]["timeout_s"] <= 20
    calls_before = len(crawler_calls)
    assert await provider.extract(URL) == result
    assert len(crawler_calls) == calls_before and len(original_calls) == 1


async def test_pdf_probe_does_not_replace_html_failures_or_probe_denials(monkeypatch):
    calls = []
    docs = {
        "html": _failure(),
        "missing": _failure("https://example.com/missing", "HTTP 404", "not_found"),
        "account": _failure("https://example.com/account", "auth rejected"),
        "quota": _failure("https://example.com/quota", "rate limit"),
        "policy": _failure("http://127.0.0.1/private", "private address denied"),
    }

    async def original(url, **kwargs):
        calls.append(url)
        return GuardedResponse(url, 200, {"content-type": "text/html"}, b"<p>not a PDF</p>")

    monkeypatch.setattr(_pdf_recovery, "guarded_get", original)
    recovered = await recover_pdf_sources(docs, timeout_s=90, max_chars=None, chunk=chunk_passages)
    assert recovered == {"html": docs["html"]}
    assert recovered["html"] is docs["html"]
    assert calls == [URL]


@pytest.mark.parametrize("failure", [EgressDenied("response too large"), OSError("unavailable")])
async def test_unavailable_original_pdf_probe_preserves_browser_failure(monkeypatch, failure):
    doc = _failure()

    async def original(url, **kwargs):
        raise failure

    monkeypatch.setattr(_pdf_recovery, "guarded_get", original)
    recovered = await recover_pdf_sources(
        {URL: doc}, timeout_s=90, max_chars=None, chunk=chunk_passages
    )
    assert recovered[URL] is doc


async def test_pdf_configured_text_limit_fails_without_partial_evidence(monkeypatch):
    async def original(url, **kwargs):
        return GuardedResponse(url, 200, {"content-type": "application/pdf"}, _pdf_bytes())

    monkeypatch.setattr(_pdf_recovery, "guarded_get", original)
    recovered = await recover_pdf_sources(
        {URL: _failure()}, timeout_s=90, max_chars=30, chunk=chunk_passages
    )
    result = recovered[URL]
    assert not result.fetched_ok and not result.content and not result.passages
    assert "no partial source was admitted" in result.error


async def test_non_success_original_response_does_not_become_evidence(monkeypatch):
    async def original(url, **kwargs):
        return GuardedResponse(url, 403, {"content-type": "application/pdf"}, _pdf_bytes())

    doc = _failure()
    monkeypatch.setattr(_pdf_recovery, "guarded_get", original)
    recovered = await recover_pdf_sources(
        {URL: doc}, timeout_s=90, max_chars=None, chunk=chunk_passages
    )
    assert recovered[URL] is doc
