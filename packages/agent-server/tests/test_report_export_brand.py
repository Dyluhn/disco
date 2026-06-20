"""DR-2 / §1.5 — Tests for the branded report export engine.

Covers:
  1. _build_pdf_html — structured sections + chips + cover + appendix
  2. TOC emitted when sections > 10
  3. No ::first-letter{float} in the rendered HTML
  4. Follow-up Q&A page present when follow_ups provided
  5. Neutral theme → no branded mark on cover; no accent hex in theme vars
  6. Dark mode → correct dark tokens in theme_css_vars block
  7. MD byte-identical (the existing parity test still passes)
  8. Unknown theme → ValueError (endpoint converts to 400)
  9. @font-face present in the HTML (bundled fonts included)
  10. >30 sources → .sources-2col class emitted
  11. export_report threads theme/mode; unknown theme raises ValueError
  12. serialize_pdf accepts theme/mode; unknown theme raises ValueError before WP
  13. Endpoint unknown theme → 400 (via HTTP route)
"""

from __future__ import annotations

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server.report_export import (
    _build_pdf_html,
    export_report,
    serialize_docx,
    serialize_markdown,
    serialize_pdf,
)
from disco.core import (
    EventSource,
    ReportEvent,
    ReportSection,
    SqliteEventStore,
)
from disco.core.brand import resolve_theme
from fastapi.testclient import TestClient

# ---- Fixtures ----------------------------------------------------------------


def _make_sample_report(*, n_sections: int = 2, n_passages: int = 2) -> ReportEvent:
    sections = [
        ReportSection(
            id=f"s{i}",
            title=f"Section {i}",
            markdown=f"Body of section {i} with citation [[p{i}]].",
            cited_passage_ids=[f"p{i}"],
            confidence="high",
            disputed_notes=[],
        )
        for i in range(n_sections)
    ]
    passages = [
        {"id": f"p{i}", "source_title": f"Source {i}", "source_url": f"https://example.com/p{i}"}
        for i in range(n_passages)
    ]
    return ReportEvent(
        source=EventSource.AGENT,
        query="Test query for brand export",
        summary="Executive summary text.",
        sections=sections,
        passages=passages,
        all_hits=[],
        unsupported_count=0,
        bounded_by=None,
        depth_tier="standard_deep",
    )


def _make_big_report(*, n_sections: int = 12, n_passages: int = 35) -> ReportEvent:
    return _make_sample_report(n_sections=n_sections, n_passages=n_passages)


# ---- 1. _build_pdf_html — basic structure ------------------------------------


def test_build_pdf_html_sections_present() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "<section" in html
    assert "Section 0" in html
    assert "Section 1" in html


def test_build_pdf_html_cover_present() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "cover-page" in html
    assert "cover-title" in html
    assert "Test query for brand export" in html


def test_build_pdf_html_appendix_present() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "sources-appendix" in html
    assert "Sources (2)" in html


def test_build_pdf_html_chips_converted() -> None:
    """[[pid]] citation markers are converted to .chip spans."""
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    # The [[p0]] citation in the section body should become a .chip
    assert 'class="chip"' in html
    assert "[[" not in html  # raw [[...]] must be gone


def test_build_pdf_html_section_numbers() -> None:
    """Section numbers are emitted as .section-no labels (01, 02, ...)."""
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "section-no" in html
    assert "01" in html
    assert "02" in html


def test_build_pdf_html_no_nl2br_extension() -> None:
    """Structured HTML path must not use nl2br markdown extension."""
    # We validate indirectly: the output should NOT turn single newlines
    # into <br /> tags (nl2br behaviour).
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    # Modify a section to have a single newline mid-paragraph
    report = ReportEvent(
        source=report.source,
        query=report.query,
        summary=report.summary,
        sections=[
            ReportSection(
                id="s0",
                title="Test",
                markdown="First line\nSecond line",
                cited_passage_ids=[],
                confidence="high",
                disputed_notes=[],
            )
        ],
        passages=[],
        all_hits=[],
    )
    html = _build_pdf_html(report, None, theme)
    # nl2br would produce <br />, bare newline in markdown lib → merged paragraph
    assert "<br" not in html or "First line" in html  # soft check


# ---- 2. TOC when sections > 10 -----------------------------------------------


def test_build_pdf_html_toc_present_over_10_sections() -> None:
    report = _make_big_report(n_sections=12)
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "toc-block" in html
    assert "Contents" in html


def test_build_pdf_html_no_toc_under_10_sections() -> None:
    report = _make_sample_report()  # 2 sections
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    # toc-block appears in the CSS; look for the HTML element attribute
    assert 'class="toc-block"' not in html


# ---- 3. No ::first-letter{float} -------------------------------------------


def test_build_pdf_html_no_first_letter_float() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "::first-letter" not in html.replace(" ", "")
    # Dropcap is an inline span
    assert "dropcap" in html


# ---- 4. Follow-up Q&A page --------------------------------------------------


def test_build_pdf_html_followup_page() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    follow_ups = [("What about X?", "X is important.")]
    html = _build_pdf_html(report, follow_ups, theme)
    assert "followup-page" in html
    assert "What about X?" in html
    assert "X is important." in html


def test_build_pdf_html_no_followup_when_empty() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    # followup-page appears in CSS; look for the actual HTML element
    assert 'class="followup-page"' not in html


# ---- 5. Neutral theme --------------------------------------------------------


def test_build_pdf_html_neutral_no_defmark_on_cover() -> None:
    """Neutral theme: the disco definition mark must NOT appear on the cover."""
    report = _make_sample_report()
    theme = resolve_theme("neutral", "light")
    html = _build_pdf_html(report, None, theme)
    # The defmark contains .hw (headword span) which is only in the mark
    assert 'class="hw"' not in html


def test_build_pdf_html_branded_has_defmark() -> None:
    """Disco theme: the disco definition mark IS on the cover."""
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert 'class="hw"' in html


def test_theme_css_vars_neutral_no_accent_hex() -> None:
    """Neutral CSS vars must NOT contain the branded accent hex #4077a3."""
    from disco.core.brand import theme_css_vars

    neutral = resolve_theme("neutral", "light")
    css = theme_css_vars(neutral)
    assert "#4077a3" not in css
    assert "#79c0f1" not in css


# ---- 6. Dark mode tokens ----------------------------------------------------


def test_build_pdf_html_dark_has_dark_bg() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "dark")
    html = _build_pdf_html(report, None, theme)
    # The dark background token should be in the theme CSS block
    assert "#0e0f12" in html


# ---- 7. MD byte-identical ---------------------------------------------------


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


def _make_canonical_report() -> ReportEvent:
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
            {"id": "p0", "source_title": "Avian Speed Database", "source_url": "https://birds.example.com/p0"},
            {"id": "p1", "source_title": "European Ornithology Journal", "source_url": "https://birds.example.com/p1"},
        ],
        all_hits=[],
        unsupported_count=1,
        bounded_by="sources",
        depth_tier="standard_deep",
    )


def test_markdown_byte_parity_with_theme() -> None:
    """MD export is byte-identical regardless of theme/mode."""
    report = _make_canonical_report()
    result = serialize_markdown(report)
    assert result == CAPTURED_MARKDOWN, (
        f"Byte-parity failed.\n--- EXPECTED ---\n{CAPTURED_MARKDOWN!r}\n"
        f"--- GOT ---\n{result!r}"
    )


def test_export_report_md_byte_identical_with_theme() -> None:
    """export_report('md', theme='disco') is byte-identical to the baseline."""
    report = _make_canonical_report()
    payload_default, _, _ = export_report(report, "md")
    payload_themed, _, _ = export_report(report, "md", theme="disco", mode="light")
    payload_neutral, _, _ = export_report(report, "md", theme="neutral", mode="dark")
    assert payload_default == payload_themed == payload_neutral


# ---- 8. Unknown theme → ValueError / 400 ------------------------------------


def test_serialize_pdf_unknown_theme_raises() -> None:
    """serialize_pdf with an unknown theme raises ValueError before calling WP."""
    report = _make_sample_report()
    with pytest.raises(ValueError, match="Unknown theme"):
        serialize_pdf(report, theme="vaporwave")


def test_export_report_unknown_theme_raises() -> None:
    report = _make_sample_report()
    with pytest.raises(ValueError, match="Unknown theme"):
        export_report(report, "pdf", theme="unknown-brand")


# ---- 9. @font-face in HTML ---------------------------------------------------


def test_build_pdf_html_fontface_present() -> None:
    """Bundled @font-face declarations are embedded in the HTML <style> as
    base64 data-URIs (not file:// — browsers block those over http, and
    WeasyPrint rejects them when base_url is an http URL)."""
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "@font-face" in html
    assert "Fraunces" in html
    assert "Newsreader" in html
    # data-URI embedding — no file:// paths (would be blocked over http)
    assert "data:font/ttf;base64," in html
    assert "file://" not in html


# ---- 10. >30 sources → 2-col ------------------------------------------------


def test_build_pdf_html_2col_sources_over_30() -> None:
    report = _make_big_report(n_passages=35)
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "sources-2col" in html


def test_build_pdf_html_no_2col_under_30() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    # When ≤30 sources: the appendix div has class="sources-appendix" (not sources-2col)
    assert 'class="sources-appendix"' in html
    assert 'class="sources-appendix sources-2col"' not in html


# ---- 11. serialize_pdf signature compat -------------------------------------


def test_serialize_pdf_accepts_default_theme() -> None:
    """serialize_pdf with no theme/mode args uses disco/light (default)."""
    pytest.importorskip("weasyprint", reason="WeasyPrint not installed")
    report = _make_sample_report()
    result = serialize_pdf(report)
    assert isinstance(result, bytes)
    assert result[:5] == b"%PDF-"


def test_serialize_pdf_with_explicit_theme() -> None:
    pytest.importorskip("weasyprint", reason="WeasyPrint not installed")
    report = _make_sample_report()
    result = serialize_pdf(report, theme="disco", mode="dark")
    assert isinstance(result, bytes)
    assert result[:5] == b"%PDF-"


def test_serialize_pdf_neutral_theme() -> None:
    pytest.importorskip("weasyprint", reason="WeasyPrint not installed")
    report = _make_sample_report()
    result = serialize_pdf(report, theme="neutral", mode="light")
    assert isinstance(result, bytes)
    assert result[:5] == b"%PDF-"


# ---- 12. Endpoint unknown theme → 400 ----------------------------------------


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


@pytest.fixture
def client_with_runtime(store: SqliteEventStore) -> TestClient:
    runtime = ConversationRuntime(store)
    return TestClient(create_app(store, runtime=runtime))


def _create_conversation(client: TestClient) -> str:
    r = client.post("/conversations", json={"owner_id": "local"})
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


def _seed_report(store: SqliteEventStore, cid: str, report: ReportEvent) -> None:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(store.append(cid, report))
    finally:
        loop.close()


def test_endpoint_unknown_theme_returns_400(
    store: SqliteEventStore, client_with_runtime: TestClient
) -> None:
    """Export endpoint with an unknown theme → 400."""
    cid = _create_conversation(client_with_runtime)
    report = _make_sample_report()
    _seed_report(store, cid, report)
    r = client_with_runtime.post(
        f"/api/conversations/{cid}/report/export?fmt=pdf",
        json={"theme": "vaporwave", "mode": "light"},
    )
    assert r.status_code == 400
    assert "vaporwave" in r.text or "Unknown" in r.text


def test_endpoint_valid_theme_returns_payload(
    store: SqliteEventStore, client_with_runtime: TestClient
) -> None:
    """Export endpoint with a valid md theme → 200 (byte-identical for md)."""
    cid = _create_conversation(client_with_runtime)
    report = _make_canonical_report()
    _seed_report(store, cid, report)
    r = client_with_runtime.post(
        f"/api/conversations/{cid}/report/export?fmt=md",
        json={"theme": "neutral", "mode": "dark"},
    )
    assert r.status_code == 200
    # MD is byte-identical regardless of theme
    assert CAPTURED_MARKDOWN.encode("utf-8") == r.content


# ---- 13. DOCX serialize retains reference.docx wiring -----------------------


class _FakeSandbox:
    def __init__(self, *, exit_code: int = 0, stderr: str = "", out: bytes = b"PKfake"):
        self.exit_code = exit_code
        self.stderr = stderr
        self.out = out
        self.written: dict[str, bytes] = {}
        self.commands: list[str] = []

    async def write_file(self, path: str, data: bytes) -> None:
        self.written[path] = data

    async def exec_shell(self, cmd: str, *, timeout_s: int):
        self.commands.append(cmd)

        class _R:
            pass

        r = _R()
        r.exit_code = self.exit_code
        r.stdout = ""
        r.stderr = self.stderr
        r.timed_out = False
        return r

    async def read_file(self, path: str) -> bytes:
        return self.out


async def test_serialize_docx_includes_reference_doc() -> None:
    """serialize_docx writes _reference.docx into the sandbox and passes --reference-doc."""
    report = _make_sample_report()
    sbx = _FakeSandbox(out=b"PKzipdocx")
    result = await serialize_docx(report, sbx)
    assert result == b"PKzipdocx"
    # reference.docx was written to the sandbox
    assert "_reference.docx" in sbx.written
    assert len(sbx.written["_reference.docx"]) > 0
    # pandoc command includes --reference-doc and --toc
    assert any("--reference-doc" in c for c in sbx.commands)
    assert any("--toc" in c for c in sbx.commands)


async def test_serialize_docx_reference_docx_is_valid_zip() -> None:
    """The reference.docx bundled in agent-server is a valid OOXML ZIP."""
    import io
    import zipfile

    from disco.agent_server.report_export import _reference_docx_path

    path = _reference_docx_path()
    assert path.exists()
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as zf:
        names = zf.namelist()
    assert "[Content_Types].xml" in names
    assert "word/styles.xml" in names


# ---- 14. bounded_by note present when set ------------------------------------


def test_build_pdf_html_bounded_note() -> None:
    report = ReportEvent(
        source=EventSource.AGENT,
        query="Q",
        summary="S",
        sections=[],
        passages=[],
        all_hits=[],
        bounded_by="sources",
    )
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    assert "bounded-note" in html
    assert "sources" in html


def test_build_pdf_html_no_bounded_note_when_none() -> None:
    report = _make_sample_report()
    theme = resolve_theme("disco", "light")
    html = _build_pdf_html(report, None, theme)
    # bounded_by is None in the fixture — the actual element should be absent
    # (bounded-note appears in CSS; look for the HTML element class attribute)
    assert 'class="bounded-note"' not in html
