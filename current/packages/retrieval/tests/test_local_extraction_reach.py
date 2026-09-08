"""What the bundled (keyless) reader can actually reach.

Three measured losses on the 2026-09-07 keyless runs: publisher walls that refuse
a bot-shaped request, report PDFs bigger than the default fetch limit, and
anti-bot refusals that were never re-read out of the Internet Archive.

The header and byte-limit tests drive the REAL host egress layer over a fake
socket, so they assert the bytes that would go on the wire and the limit that is
really enforced, rather than the arguments handed to a stand-in.
"""

from __future__ import annotations

import io

import httpx
import pytest
from disco.core import host_egress
from disco.retrieval._extraction_fallbacks import reset_extraction_pacing
from disco.retrieval._transport_retry import _extraction_error_class
from disco.retrieval.bundled_providers import LocalExtractionProvider
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

URL = "https://publisher.example/science/article/pii/S0000000000000000"
WAYBACK = "https://web.archive.org/web/2id_/" + URL
ARTICLE = (
    "Closed-loop pumped storage hydropower was evaluated against the tested basin "
    "over a full water year, and the round-trip efficiency held within the range "
    "the feasibility study assumed."
)
PAGE = (
    "<html><head><title>Pumped storage feasibility</title></head>"
    f"<body><p>{ARTICLE}</p></body></html>"
)


class _WireSocket:
    """The socket the egress layer writes to, so a test can read what it sent."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.sent = b""

    def makefile(self, *args: object) -> io.BytesIO:
        return io.BytesIO(self.payload)

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, body: bytes, content_type: str) -> _WireSocket:
    """Answer the next guarded fetch with ``body`` and hand back the wire record."""
    head = (
        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: {content_type}\r\n\r\n"
    )
    sock = _WireSocket(head.encode() + body)
    monkeypatch.setattr(host_egress, "_connect_validated", lambda *args: sock)
    return sock


def _pdf_bytes(padding: int) -> bytes:
    """One readable text page, padded to ``padding`` bytes by an unreferenced stream."""
    writer = PdfWriter()
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
    contents = DecodedStreamObject()
    contents.set_data(f"BT /F1 12 Tf 50 700 Td ({ARTICLE}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(contents)
    filler = DecodedStreamObject()
    filler.set_data(b"0" * padding)
    writer._add_object(filler)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def test_local_extract_sends_browser_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """ScienceDirect, MDPI, doi.org and friends answered 403 to the old agent."""
    sock = _serve(monkeypatch, PAGE.encode(), "text/html; charset=utf-8")
    doc = await LocalExtractionProvider().extract(URL)
    assert doc.fetched_ok and ARTICLE in doc.content
    sent = sock.sent.decode()
    assert "compatible; disco/1.0" not in sent
    assert "\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64)" in sent
    assert "Chrome/" in sent and "Safari/537.36\r\n" in sent
    assert "\r\nAccept: text/html,application/xhtml+xml" in sent
    assert "\r\nAccept-Language: en-US,en;q=0.9\r\n" in sent
    # The egress layer decodes gzip and deflate and nothing else, so those are
    # the only codings the browser headers may advertise, and exactly once.
    assert sent.count("Accept-Encoding:") == 1
    assert "\r\nAccept-Encoding: gzip, deflate\r\n" in sent


async def test_local_extract_reads_pdf_over_five_megabytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The denied government PDFs measured 6.1, 8.9 and 10.2 MB; the cap was 5 MB."""
    body = _pdf_bytes(6_000_000)
    assert len(body) > 5_000_000
    _serve(monkeypatch, body, "application/pdf")
    doc = await LocalExtractionProvider().extract(URL)
    assert doc.fetched_ok and not doc.error
    assert ARTICLE in doc.content
    assert doc.passages and len(doc.passages) <= 12


async def test_local_extract_refuses_a_pdf_past_the_extraction_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raised cap is still a cap: 25 MB is the bundled reader's whole appetite."""
    _serve(monkeypatch, _pdf_bytes(26_000_000), "application/pdf")
    doc = await LocalExtractionProvider().extract(URL)
    assert not doc.fetched_ok
    assert doc.status == "error" and "byte limit" in doc.error


async def test_local_extract_recovers_anti_bot_page_from_wayback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publisher's 403 is a wall, not a fact about the page; the archive kept it."""
    fetched: list[str] = []

    async def fetch(url: str, **kwargs: object) -> httpx.Response:
        fetched.append(url)
        request = httpx.Request("GET", url)
        if url == WAYBACK:
            return httpx.Response(
                200, request=request, headers={"content-type": "text/html"}, text=PAGE
            )
        return httpx.Response(403, request=request, headers={"content-type": "text/html"}, text="")

    monkeypatch.setattr("disco.retrieval.bundled_providers.guarded_get", fetch)
    reset_extraction_pacing()
    docs = await LocalExtractionProvider().extract_many([URL])
    reset_extraction_pacing()

    assert fetched == [URL, WAYBACK]
    assert len(docs) == 1
    recovered = docs[0]
    # The citation must point at the source, not at the archive that kept a copy.
    assert recovered.url == URL
    assert recovered.fetched_ok and ARTICLE in recovered.content
    assert recovered.passages and all(p.source_url == URL for p in recovered.passages)


async def test_a_local_403_is_the_anti_bot_class_the_archive_fallback_looks_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`recover_blocked_pages` only retries "anti_bot"; a bare 403 must land there."""

    async def fetch(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            403, request=httpx.Request("GET", url), headers={"content-type": "text/html"}, text=""
        )

    monkeypatch.setattr("disco.retrieval.bundled_providers.guarded_get", fetch)
    doc = await LocalExtractionProvider().extract(URL)
    assert doc.status == "blocked" and not doc.fetched_ok
    assert _extraction_error_class(doc) == "anti_bot"


async def test_an_archive_miss_leaves_the_original_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive refuses some publishers outright; that must not erase the reason."""

    async def fetch(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            403, request=httpx.Request("GET", url), headers={"content-type": "text/html"}, text=""
        )

    monkeypatch.setattr("disco.retrieval.bundled_providers.guarded_get", fetch)
    reset_extraction_pacing()
    docs = await LocalExtractionProvider().extract_many([URL])
    reset_extraction_pacing()

    assert [doc.url for doc in docs] == [URL]
    assert docs[0].status == "blocked" and "403" in docs[0].error
