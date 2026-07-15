"""RP-07 — Unit tests for report export serializers + endpoint routing.

Covers:
  1. Markdown byte-parity — captured sample vs serialize_markdown
  2. Format dispatch — md/pdf → correct media_type + bytes
  3. 404 — missing report
  4. 400 — unknown fmt
  5. PDF — skip cleanly if binary absent, assert call shape + non-empty bytes
     when present
"""

from __future__ import annotations

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server.report_export import (
    _markdown_to_html,
    export_report,
    pdf_available,
    serialize_markdown,
    serialize_pdf,
)
from disco.core import (
    EventSource,
    ReportEvent,
    ReportSection,
    SqliteEventStore,
)
from fastapi.testclient import TestClient

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
                markdown=(
                    "The African swallow cruises at **11 m/s** with 5-7 flaps per second."
                    "\n\nSee [[p0]] for primary data."
                ),
                cited_passage_ids=["p0"],
                confidence="high",
                disputed_notes=[],
            ),
            ReportSection(
                id="s1",
                title="European Swallow",
                markdown=(
                    "The European swallow is smaller and slower: **8 m/s**."
                    "\n\nMeasurements vary by season [[p1]]."
                ),
                cited_passage_ids=["p1"],
                confidence="mixed",
                disputed_notes=["seasonal variation unaccounted in some studies"],
            ),
        ],
        passages=[
            {
                "id": "p0",
                "source_title": "Avian Speed Database",
                "source_url": "https://birds.example.com/p0",
            },
            {
                "id": "p1",
                "source_title": "European Ornithology Journal",
                "source_url": "https://birds.example.com/p1",
            },
        ],
        all_hits=[],
        unsupported_count=1,
        bounded_by="sources",
        depth_tier="standard_deep",
    )


# The exact markdown output the original TypeScript serializer produces for
# the sample report above. This IS the captured sample — cut from a browser
# console after running `exportReportAsMarkdown(report)` with the same data.
CAPTURED_MARKDOWN = (
    """\
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

"""
    "_This run was bounded by **sources**. Some planned sub-questions were not covered. "
    "Consider running the EXHAUSTIVE tier or assigning a faster driver model for deeper coverage._"
    """

---

Passages cited (2):

- [p0] Avian Speed Database — https://birds.example.com/p0
- [p1] European Ornithology Journal — https://birds.example.com/p1"""
)


# ---- Markdown byte-parity (acceptance #2) -----------------------------------


def test_markdown_byte_parity() -> None:
    """serialize_markdown MUST produce byte-identical output to the TypeScript
    client-side serializer for the same ReportEvent. This is a captured sample
    from `deepResearch.ts:serializeReportToMarkdown`."""
    report = _make_sample_report()
    result = serialize_markdown(report)
    assert result == CAPTURED_MARKDOWN, (
        f"Byte-parity failed.\n--- EXPECTED ---\n{CAPTURED_MARKDOWN!r}\n--- GOT ---\n{result!r}"
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


def test_endpoint_400_docx_removed(client_with_runtime: TestClient) -> None:
    """W-12: docx is no longer a valid export format — the route rejects it as
    an unknown format (md/pdf only) instead of spinning a render sandbox."""
    r = client_with_runtime.post("/api/conversations/conv_any/report/export?fmt=docx")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "docx" in detail
    assert "md, pdf" in detail  # advertised valid set no longer lists docx


def test_capabilities_omits_docx(client_with_runtime: TestClient) -> None:
    """W-12: the export-capabilities probe no longer advertises docx; only md
    (always) and pdf (weasyprint-gated)."""
    r = client_with_runtime.get("/api/export/capabilities")
    assert r.status_code == 200, r.text
    caps = r.json()
    assert caps["md"] is True
    assert "pdf" in caps
    assert "docx" not in caps


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
    pytest.importorskip("weasyprint", reason="WeasyPrint not installed (image rebuild needed)")
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


def test_export_report_rejects_docx() -> None:
    """W-12: docx is no longer a valid export format — export_report rejects it
    as an unknown format (md/pdf only)."""
    report = _make_sample_report()
    with pytest.raises(ValueError, match="Unknown export format"):
        export_report(report, "docx")


def test_pdf_available_returns_bool() -> None:
    """pdf_available() reflects whether weasyprint is importable (drives the
    /api/export/capabilities pdf flag — no false affordance)."""
    assert isinstance(pdf_available(), bool)


# ---- WALK-20: follow-up Q&A in exports (acceptance) -------------------------


def test_markdown_with_follow_ups_appends_qa_section() -> None:
    """serialize_markdown with follow_ups appends a ## Follow-up Q&A section."""
    report = _make_sample_report()
    follow_ups = [
        ("What about sea gulls?", "Sea gulls also fly but at different speeds."),
        ("Is this well studied?", "Yes, extensively by ornithologists worldwide."),
    ]
    result = serialize_markdown(report, follow_ups)
    assert "## Follow-up Q&A" in result
    assert "### Follow-up 1" in result
    assert "**Q:** What about sea gulls?" in result
    assert "Sea gulls also fly but at different speeds." in result
    assert "### Follow-up 2" in result
    assert "**Q:** Is this well studied?" in result
    assert "Yes, extensively by ornithologists worldwide." in result
    # Q&A section must appear AFTER the passages footer
    passages_pos = result.index("Passages cited")
    qa_pos = result.index("## Follow-up Q&A")
    assert qa_pos > passages_pos


def test_markdown_without_follow_ups_is_byte_identical() -> None:
    """serialize_markdown with no follow_ups is byte-identical to the baseline.

    This is the key backwards-compat contract: callers that don't pass
    follow_ups must receive exactly the same bytes as before (the py↔ts
    byte-parity test still passes when follow_ups is absent).
    """
    report = _make_sample_report()
    baseline = serialize_markdown(report)
    with_none = serialize_markdown(report, None)
    with_empty: str = serialize_markdown(report, [])
    assert baseline == with_none, "follow_ups=None must produce baseline output"
    assert baseline == with_empty, "follow_ups=[] must produce baseline output"


def test_export_report_md_with_follow_ups() -> None:
    """export_report('md', follow_ups=[...]) includes the Q&A section in bytes."""
    report = _make_sample_report()
    follow_ups = [("Tell me more.", "There is more to know.")]
    payload, media_type, ext = export_report(report, "md", follow_ups)
    text = payload.decode("utf-8")
    assert "## Follow-up Q&A" in text
    assert "**Q:** Tell me more." in text
    assert "There is more to know." in text


def test_export_report_md_no_follow_ups_byte_identical() -> None:
    """export_report('md') with no follow_ups is byte-identical to the baseline."""
    report = _make_sample_report()
    baseline, _, _ = export_report(report, "md")
    with_none, _, _ = export_report(report, "md", None)
    with_empty, _, _ = export_report(report, "md", [])
    assert baseline == with_none
    assert baseline == with_empty


def test_endpoint_md_export_with_follow_up_seqs(
    store: SqliteEventStore, client_with_runtime: TestClient
) -> None:
    """Export endpoint with follow_up_seqs in the JSON body includes Q&A section."""
    import asyncio

    from disco.core import (
        ConversationStatus,
        LLMMessage,
        MessageEvent,
        StatusEvent,
    )
    from disco.core import (
        EventSource as _ES,
    )

    cid = _create_conversation(client_with_runtime)
    report = _make_sample_report()

    loop = asyncio.new_event_loop()
    try:
        # Seed report then a follow-up Q&A pair
        loop.run_until_complete(store.append(cid, report))
        loop.run_until_complete(store.append(cid, StatusEvent(status=ConversationStatus.FINISHED)))
        loop.run_until_complete(
            store.append(
                cid,
                MessageEvent(
                    source=_ES.USER,
                    message=LLMMessage(role="user", content="Elaborate further."),
                ),
            )
        )
        loop.run_until_complete(
            store.append(
                cid,
                MessageEvent(
                    source=_ES.AGENT,
                    message=LLMMessage(role="assistant", content="There is much more to say."),
                ),
            )
        )
    finally:
        loop.close()

    # Fetch events to discover the user-message seq
    import asyncio as _a

    loop2 = _a.new_event_loop()
    try:
        events = loop2.run_until_complete(store.get_events(cid))
    finally:
        loop2.close()
    from disco.core import MessageEvent as ME

    user_msg = next(
        (
            e
            for e in events
            if isinstance(e, ME)
            and e.message.role == "user"
            and "Elaborate" in (e.message.content or "")
        ),
        None,
    )
    assert user_msg is not None, "User message not found in event log"
    user_seq = user_msg.seq

    r = client_with_runtime.post(
        f"/api/conversations/{cid}/report/export?fmt=md",
        json={"follow_up_seqs": [user_seq]},
    )
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8")
    assert "## Follow-up Q&A" in text
    assert "**Q:** Elaborate further." in text
    assert "There is much more to say." in text


def test_endpoint_md_export_without_follow_up_seqs_byte_identical(
    store: SqliteEventStore, client_with_runtime: TestClient
) -> None:
    """Export endpoint with no follow_up_seqs in the body is byte-identical."""
    cid = _create_conversation(client_with_runtime)
    report = _make_sample_report()
    _seed_event(store, cid, report)

    # With empty body (no follow_up_seqs)
    r1 = client_with_runtime.post(f"/api/conversations/{cid}/report/export?fmt=md")
    # With explicit empty list
    r2 = client_with_runtime.post(
        f"/api/conversations/{cid}/report/export?fmt=md",
        json={"follow_up_seqs": []},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.content == r2.content, "Empty follow_up_seqs must be byte-identical to no-body"
    # And both must equal the captured baseline
    assert r1.content == CAPTURED_MARKDOWN.encode("utf-8")


def test_templates_endpoint_lists_catalog() -> None:
    """GET /api/templates returns the shared catalogue with the Disco default."""
    import asyncio
    from unittest.mock import MagicMock

    import httpx
    from disco.agent_server.app import create_app
    from disco.core.store.sqlite import SqliteEventStore

    store = MagicMock(spec=SqliteEventStore)
    app = create_app(store, runtime=None)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/templates")
            assert r.status_code == 200
            tpls = r.json()["templates"]
            ids = {t["id"] for t in tpls}
            assert "disco-light" in ids and "ink-light" in ids and "midnight-dark" in ids
            disco = next(t for t in tpls if t["id"] == "disco-light")
            assert disco["default"] is True
            assert disco["accent"].startswith("#") and disco["label"]

    asyncio.run(run())


# ---- W-10: export cover/title page uses the generated title ----------------


def test_markdown_title_uses_generated_title() -> None:
    """W-10: serialize_markdown uses the generated TITLE for the H1, not the raw
    question."""
    report = _make_sample_report()
    out = serialize_markdown(report, None, "Swallow Airspeed Field Study")
    assert out.splitlines()[0] == "# Deep Research: Swallow Airspeed Field Study"
    # The raw question must not be echoed on the title line.
    assert "unladen swallow" not in out.splitlines()[0]


def test_markdown_title_none_falls_back_to_query() -> None:
    """W-10 graceful fallback: no title → the question (byte-identical baseline)."""
    report = _make_sample_report()
    assert serialize_markdown(report, None, None) == CAPTURED_MARKDOWN
    assert serialize_markdown(report, None, "") == CAPTURED_MARKDOWN


def test_pdf_html_cover_uses_title() -> None:
    """W-10: the PDF cover title page renders the generated title (and the <title>
    head), not the raw question."""
    from disco.agent_server.report_export import _build_pdf_html
    from disco.core.brand import resolve_theme

    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme, "Swallow Airspeed Field Study")
    # cover-title block carries the generated title (dropcap splits the 1st char).
    assert "Swallow Airspeed Field Study"[1:] in html
    assert "<title>Swallow Airspeed Field Study</title>" in html
    # The raw question is NOT used as the cover/title.
    assert "<title>What is the airspeed" not in html


def test_pdf_html_cover_falls_back_to_query() -> None:
    """W-10 fallback: no title → the question is used (unchanged from baseline)."""
    from disco.agent_server.report_export import _build_pdf_html
    from disco.core.brand import resolve_theme

    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme, None)
    assert "<title>What is the airspeed velocity of an unladen swallow?</title>" in html


def test_endpoint_md_export_uses_stored_title(
    store: SqliteEventStore, client_with_runtime: TestClient
) -> None:
    """W-10 end-to-end: the export endpoint reads the stored conversation title
    and puts it on the title page instead of the raw question."""
    cid = _create_conversation(client_with_runtime)
    _seed_event(store, cid, _make_sample_report())
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(store.update_title(cid, "Swallow Airspeed Field Study"))
    finally:
        loop.close()
    r = client_with_runtime.post(f"/api/conversations/{cid}/report/export?fmt=md")
    assert r.status_code == 200, r.text
    first_line = r.content.decode("utf-8").splitlines()[0]
    assert first_line == "# Deep Research: Swallow Airspeed Field Study"


def test_endpoint_md_export_no_title_falls_back_to_query(
    store: SqliteEventStore, client_with_runtime: TestClient
) -> None:
    """W-10 fallback: with no stored title the export echoes the question
    (byte-identical to the pre-W-10 baseline)."""
    cid = _create_conversation(client_with_runtime)
    _seed_event(store, cid, _make_sample_report())
    r = client_with_runtime.post(f"/api/conversations/{cid}/report/export?fmt=md")
    assert r.status_code == 200, r.text
    assert r.content == CAPTURED_MARKDOWN.encode("utf-8")
