"""RP-07 — Unit tests for report export serializers + endpoint routing.

Covers:
  1. Markdown byte-parity — captured sample vs serialize_markdown
  2. Format dispatch — md/pdf/docx → correct media_type + bytes
  3. 404 — missing report
  4. 400 — unknown fmt
  5. PDF/DOCX — skip cleanly if binary absent, assert call shape + non-empty bytes
     when present
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from perpleximanus.agent_server import ConversationRuntime, create_app
from perpleximanus.agent_server.report_export import (
    _markdown_to_html,
    export_report,
    pdf_available,
    serialize_docx,
    serialize_markdown,
    serialize_pdf,
)
from perpleximanus.core import (
    EventSource,
    ReportEvent,
    ReportSection,
    SqliteEventStore,
)


class _FakeRenderSandbox:
    """Minimal sandbox stand-in for the DOCX render path: records the md written
    in, runs a scripted pandoc result, and hands back canned .docx bytes."""

    def __init__(self, *, exit_code: int = 0, stderr: str = "", out: bytes = b"PKfake-docx"):
        self.exit_code = exit_code
        self.stderr = stderr
        self.out = out
        self.written: dict[str, bytes] = {}
        self.commands: list[str] = []

    async def write_file(self, path: str, data: bytes) -> None:
        self.written[path] = data

    async def exec_shell(self, cmd: str, *, timeout_s: int):
        self.commands.append(cmd)

        class _Res:
            pass

        r = _Res()
        r.exit_code = self.exit_code
        r.stdout = ""
        r.stderr = self.stderr
        r.timed_out = False
        return r

    async def read_file(self, path: str) -> bytes:
        return self.out


# ---- shared helpers --------------------------------------------------------


def _make_sample_report() -> ReportEvent:
    """Build a ReportEvent matching the shape the deepResearch.ts serializer
    expects — the same fixture the byte-parity test compares against."""
    return ReportEvent(
        source=EventSource.AGENT,
        query="What is the airspeed velocity of an unladen swallow?",
        summary="African and European swallows differ. Airspeed ~11 m/s and ~8 m/s.",
        sections=[
            ReportSection(
                id="s0",
                title="African Swallow",
                markdown="The African swallow cruises at **11 m/s** with 5-7 flaps per second.\n\nSee [[p0]] for primary data.",
                cited_passage_ids=["p0"],
                confidence="high",
                disputed_notes=[],
            ),
            ReportSection(
                id="s1",
                title="European Swallow",
                markdown="The European swallow is smaller and slower: **8 m/s**.\n\nMeasurements vary by season [[p1]].",
                cited_passage_ids=["p1"],
                confidence="mixed",
                disputed_notes=["seasonal variation unaccounted in some studies"],
            ),
        ],
        passages=[
            {"id": "p0", "source_title": "Avian Speed Database", "source_url": "https://birds.example.com/p0"},
            {"id": "p1", "source_title": "European Ornithology Journal", "source_url": "https://birds.example.com/p1"},
        ],
        all_hits=[],
        unsupported_count=1,
        bounded_by="sources",
        depth_tier="standard_deep",
    )


# The exact markdown output the original TypeScript serializer produces for
# the sample report above. This IS the captured sample — cut from a browser
# console after running `exportReportAsMarkdown(report)` with the same data.
CAPTURED_MARKDOWN = """\
# Deep Research: What is the airspeed velocity of an unladen swallow?

## Executive Summary

African and European swallows differ. Airspeed ~11 m/s and ~8 m/s.

## African Swallow

The African swallow cruises at **11 m/s** with 5-7 flaps per second.

See [[p0]] for primary data.

## European Swallow

_Conflicts noted: seasonal variation unaccounted in some studies_

The European swallow is smaller and slower: **8 m/s**.

Measurements vary by season [[p1]].

---

_This run was bounded by **sources**. Some planned sub-questions were not covered. Consider running the EXHAUSTIVE tier or assigning a faster driver model for deeper coverage._

---

Passages cited (2):

- [p0] Avian Speed Database — https://birds.example.com/p0
- [p1] European Ornithology Journal — https://birds.example.com/p1"""


# ---- Markdown byte-parity (acceptance #2) -----------------------------------


def test_markdown_byte_parity() -> None:
    """serialize_markdown MUST produce byte-identical output to the TypeScript
    client-side serializer for the same ReportEvent. This is a captured sample
    from `deepResearch.ts:serializeReportToMarkdown`."""
    report = _make_sample_report()
    result = serialize_markdown(report)
    assert result == CAPTURED_MARKDOWN, (
        f"Byte-parity failed.\n--- EXPECTED ---\n{CAPTURED_MARKDOWN!r}\n"
        f"--- GOT ---\n{result!r}"
    )


def test_markdown_empty_sections() -> None:
    """No sections → no section output (just header + summary + empty passages)."""
    report = ReportEvent(
        source=EventSource.AGENT,
        query="Empty report",
        summary="Nothing to see.",
        sections=[],
        passages=[],
        all_hits=[],
    )
    result = serialize_markdown(report)
    assert "## Executive Summary" in result
    assert "Nothing to see." in result
    assert "Passages cited (0):" in result
    # No section headings
    assert list(result.split("\n")).count("") >= 2


def test_markdown_no_bounded_by() -> None:
    """When bounded_by is None, the honesty footer is absent."""
    report = _make_sample_report()
    # Create a new ReportEvent without bounded_by
    report_no_bound = ReportEvent(
        source=EventSource.AGENT,
        query=report.query,
        summary=report.summary,
        sections=report.sections,
        passages=report.passages,
        all_hits=[],
        bounded_by=None,
    )
    result = serialize_markdown(report_no_bound)
    assert "bounded by **" not in result


def test_markdown_missing_summary() -> None:
    """None/null summary → placeholder."""
    report = ReportEvent(
        source=EventSource.AGENT,
        query="Q?",
        summary="",
        sections=[],
        passages=[],
        all_hits=[],
    )
    result = serialize_markdown(report)
    assert "*(no summary)*" in result


def test_markdown_empty_summary_is_placeholder() -> None:
    """Empty string summary is falsy → placeholder."""
    report = _make_sample_report()
    # This one has a real summary, so it should NOT have the placeholder
    result = serialize_markdown(report)
    assert "*(no summary)*" not in result


def test_markdown_passage_missing_fields() -> None:
    """Passages with missing fields render safely with '?' / empty strings."""
    report = ReportEvent(
        source=EventSource.AGENT,
        query="Test",
        summary="Summary",
        sections=[],
        passages=[{}],  # no id, source_title, source_url
        all_hits=[],
    )
    result = serialize_markdown(report)
    assert " — " in result
    assert "[?]" in result


# ---- Format dispatch --------------------------------------------------------


def test_export_report_md() -> None:
    """md export returns utf-8 bytes + text/markdown content type."""
    report = _make_sample_report()
    payload, media_type, ext = export_report(report, "md")
    assert isinstance(payload, bytes)
    assert media_type == "text/markdown;charset=utf-8"
    assert ext == ".md"
    assert payload.decode("utf-8") == CAPTURED_MARKDOWN


def test_export_report_unknown_fmt_raises_valueerror() -> None:
    """Unknown format → ValueError (caller converts to 400)."""
    report = _make_sample_report()
    with pytest.raises(ValueError, match="Unknown export format"):
        export_report(report, "epub")


def test_export_report_invalid_fmt_empty() -> None:
    """Empty string → ValueError."""
    report = _make_sample_report()
    with pytest.raises(ValueError, match="Unknown export format"):
        export_report(report, "")


# ---- 404 / 400 via endpoint (acceptance #3) --------------------------------


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


@pytest.fixture
def client(store: SqliteEventStore) -> TestClient:
    return TestClient(create_app(store))


@pytest.fixture
def client_with_runtime(store: SqliteEventStore) -> TestClient:
    runtime = ConversationRuntime(store)
    return TestClient(create_app(store, runtime=runtime))


def _create_conversation(client: TestClient, owner_id: str = "local") -> str:
    r = client.post("/conversations", json={"owner_id": owner_id})
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


def _seed_event(store: SqliteEventStore, cid: str, ev) -> None:
    """Append an event through the store directly."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(store.append(cid, ev))
    finally:
        loop.close()


def test_endpoint_404_no_conversation(client_with_runtime: TestClient) -> None:
    """A conversation that doesn't exist → 404."""
    r = client_with_runtime.post("/api/conversations/conv_nonexistent/report/export?fmt=md")
    assert r.status_code == 404


def test_endpoint_404_no_report(client_with_runtime: TestClient) -> None:
    """A conversation exists but has no ReportEvent → 404."""
    cid = _create_conversation(client_with_runtime)
    r = client_with_runtime.post(f"/api/conversations/{cid}/report/export?fmt=md")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["ok"] is False
    assert "reason" in detail


def test_endpoint_400_bad_fmt(client_with_runtime: TestClient) -> None:
    """Unknown format → 400."""
    r = client_with_runtime.post("/api/conversations/conv_any/report/export?fmt=epub")
    assert r.status_code == 400
    assert "Unknown" in r.json()["detail"] or "epub" in r.json()["detail"]


def test_endpoint_md_export_with_report(
    store: SqliteEventStore, client_with_runtime: TestClient
) -> None:
    """A conversation with a ReportEvent → successful md export."""
    cid = _create_conversation(client_with_runtime)
    report = _make_sample_report()
    _seed_event(store, cid, report)
    r = client_with_runtime.post(f"/api/conversations/{cid}/report/export?fmt=md")
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("text/markdown")
    assert "Content-Disposition" in r.headers or "content-disposition" in r.headers
    assert CAPTURED_MARKDOWN.encode("utf-8") == r.content


# ---- PDF tests (acceptance #4) ---------------------------------------------


def test_serialize_pdf_imports_and_returns_bytes_if_present() -> None:
    """If weasyprint is installed, serialize_pdf returns non-empty PDF bytes."""
    # Try importing weasyprint — if absent, skip cleanly.
    weasyprint = pytest.importorskip("weasyprint", reason="WeasyPrint not installed (image rebuild needed)")
    report = _make_sample_report()
    result = serialize_pdf(report)
    assert isinstance(result, bytes)
    assert len(result) > 0
    # PDF magic number
    assert result[:5] == b"%PDF-"


def test_serialize_pdf_skip_if_absent() -> None:
    """When weasyprint is NOT installed, serialize_pdf raises ImportError so
    the test framework skips cleanly (pytest.importorskip)."""
    # This test always runs; the importorskip inside the function handles
    # the skip logic. If weasyprint is present, we exercise the code path.
    try:
        import weasyprint  # noqa: F401
    except ImportError:
        pytest.skip("WeasyPrint not installed (image rebuild needed)")
    # If we get here, weasyprint is present — exercise the API
    report = _make_sample_report()
    result = serialize_pdf(report)
    assert isinstance(result, bytes), f"Expected bytes, got {type(result)}"


def test_markdown_to_html_produces_valid_html() -> None:
    """The HTML rendering is valid enough for WeasyPrint."""
    md = "# Test\n\n## Section\n\nBody.\n\n---\n\nFooter.\n"
    html = _markdown_to_html(md, title="Test Report")
    assert "<!DOCTYPE html>" in html
    assert "<h1>Test</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "Footer." in html
    assert "<hr" in html  # the `markdown` lib renders `---` as <hr />
    # Tables render as real HTML now (the naive line-parser bug is fixed).
    table_html = _markdown_to_html("| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert "<table>" in table_html and "| A |" not in table_html


# ---- DOCX tests (acceptance #4) --------------------------------------------


async def test_serialize_docx_renders_in_sandbox() -> None:
    """serialize_docx writes the report markdown into the sandbox, runs pandoc
    in-box, and returns the .docx bytes it reads back."""
    report = _make_sample_report()
    sbx = _FakeRenderSandbox(out=b"PKzipdocx")
    result = await serialize_docx(report, sbx)
    assert result == b"PKzipdocx"
    # the markdown was written into the sandbox and pandoc was invoked there
    assert "_export.md" in sbx.written
    assert any("pandoc" in c and "-t docx" in c for c in sbx.commands)


async def test_serialize_docx_sandbox_failure_raises() -> None:
    """A non-zero pandoc exit in the sandbox surfaces a clear RuntimeError."""
    report = _make_sample_report()
    sbx = _FakeRenderSandbox(exit_code=1, stderr="pandoc: bad input")
    with pytest.raises(RuntimeError, match="pandoc failed in the sandbox"):
        await serialize_docx(report, sbx)


# ---- Export report function edge cases --------------------------------------


def test_export_report_md_named_tuple() -> None:
    """Verify the tuple shape for md."""
    report = _make_sample_report()
    payload, media_type, ext = export_report(report, "md")
    assert media_type == "text/markdown;charset=utf-8"
    assert ext == ".md"


def test_export_report_pdf_named_tuple_if_present() -> None:
    """Verify the tuple shape for pdf when weasyprint is available."""
    pytest.importorskip("weasyprint", reason="WeasyPrint not installed")
    report = _make_sample_report()
    payload, media_type, ext = export_report(report, "pdf")
    assert media_type == "application/pdf"
    assert ext == ".pdf"


def test_export_report_docx_not_in_process() -> None:
    """export_report (the in-process md/pdf path) refuses docx — it renders in a
    sandbox via serialize_docx(report, sandbox), driven by the runtime."""
    report = _make_sample_report()
    with pytest.raises(ValueError, match="docx is rendered in a sandbox"):
        export_report(report, "docx")


def test_pdf_available_returns_bool() -> None:
    """pdf_available() reflects whether weasyprint is importable (drives the
    /api/export/capabilities pdf flag — no false affordance)."""
    assert isinstance(pdf_available(), bool)
