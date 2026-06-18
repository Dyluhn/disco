"""PDF export polish — the four Deep-Research-report PDF fixes (2026-06-18):

  ① ```chart fences render to inline SVG (or a table), never a raw JSON code block
  ② cover subtitle trims at a word/sentence boundary (no mid-word cut)
  ③ citations show numbered chips [N] (linked to the appendix), never raw passage ids
  ④ light/dark mode produces distinct, theme-aware output
"""

from __future__ import annotations

import pytest
from disco.agent_server.chart_svg import (
    palette_from_theme,
    render_chart_svg,
    render_chart_table,
)
from disco.agent_server.report_export import (
    _build_pdf_html,
    _cover_subtitle_text,
    _render_section_body,
    pdf_available,
    serialize_pdf,
)
from disco.core import EventSource, ReportEvent, ReportSection
from disco.core.brand import resolve_theme

_LIGHT = resolve_theme("disco", "light")
_PAL = palette_from_theme(_LIGHT)

_CHART = (
    '```chart\n{"chart_type":"bar","title":"T","x_label":"X","y_label":"Y",'
    '"data":[{"label":"A","value":3},{"label":"B","value":7}]}\n```'
)


def _report_with_chart() -> ReportEvent:
    return ReportEvent(
        source=EventSource.AGENT,
        query="Q?",
        summary="A summary.",
        sections=[
            ReportSection(
                id="s0",
                title="Sec",
                markdown=f"Intro [[7a6ee0_p1]].\n\n{_CHART}\n\nOutro [[unknown_x]].",
                cited_passage_ids=["7a6ee0_p1"],
                confidence="high",
                disputed_notes=["a conflict citing [[df537b_p2]]"],
            )
        ],
        passages=[
            {"id": "7a6ee0_p1", "source_title": "S1", "source_url": "https://e.com/1"},
            {"id": "df537b_p2", "source_title": "S2", "source_url": "https://e.com/2"},
        ],
        all_hits=[],
        unsupported_count=0,
        bounded_by=None,
        depth_tier="standard_deep",
    )


# ---- chart_svg module -------------------------------------------------------


@pytest.mark.parametrize("ctype", ["bar", "line", "pie", "scatter"])
def test_chart_svg_renders_each_type(ctype: str):
    if ctype == "scatter":
        data = [{"x": 1, "y": 2, "group": "g"}, {"x": 3, "y": 4, "group": "h"}]
    else:
        data = [{"label": "A", "value": 3}, {"label": "B", "value": 7}]
    svg = render_chart_svg({"chart_type": ctype, "title": "T", "data": data}, _PAL)
    assert svg is not None and svg.startswith("<svg") and svg.endswith("</svg>")


def test_chart_svg_none_on_unrenderable():
    assert render_chart_svg({"chart_type": "bar", "data": []}, _PAL) is None
    assert render_chart_svg({"chart_type": "bar", "data": [{"label": "x"}]}, _PAL) is None
    assert render_chart_svg("not a dict", _PAL) is None  # type: ignore[arg-type]


def test_chart_table_always_html():
    html = render_chart_table({"chart_type": "bar", "title": "T", "data": [{"label": "A", "value": 1}]})
    assert "<table" in html and "chart-table" in html and "A" in html


# ---- ① charts in section body ----------------------------------------------


def test_chart_fence_becomes_svg_not_code_block():
    out = _render_section_body(f"text\n\n{_CHART}\n\nmore", cite_map={}, pal=_PAL)
    assert "<svg" in out
    assert "chart-figure" in out
    assert '"chart_type"' not in out  # the raw JSON must not survive as text
    assert "<code>" not in out or "chart_type" not in out


def test_chart_fence_falls_back_to_table_when_unrenderable():
    bad = '```chart\n{"chart_type":"bar","data":[{"label":"only"}]}\n```'
    out = _render_section_body(bad, cite_map={}, pal=_PAL)
    assert "chart-table" in out and "<svg" not in out


# ---- ② cover subtitle -------------------------------------------------------


def test_cover_subtitle_no_midword_cut():
    long = "Structured multi-component lifestyle interventions " * 12
    out = _cover_subtitle_text(long, limit=120)
    assert len(out) <= 121
    assert out.endswith("…") or out[-1] in ".!?"
    # never ends mid-word (the char before any ellipsis is a full word)
    assert not out.rstrip("…").endswith(" ")
    assert "  " not in out  # whitespace collapsed


def test_cover_subtitle_short_passthrough():
    assert _cover_subtitle_text("Short summary.") == "Short summary."


def test_cover_subtitle_strips_citations():
    assert "[[" not in _cover_subtitle_text("Lead finding [[7a6ee0_p1]] holds.")


# ---- ③ numbered citations ---------------------------------------------------


def test_citation_becomes_numbered_chip_not_raw_id():
    rep = _report_with_chart()
    html = _build_pdf_html(rep, None, _LIGHT)
    assert "[[7a6ee0_p1]]" not in html  # no raw id anywhere
    assert "7a6ee0_p1]" not in html
    assert '<a class="chip" href="#src-1">1</a>' in html  # numbered + linked
    assert 'id="src-1"' in html and 'id="src-2"' in html  # appendix anchors
    assert "[1]" in html and "[2]" in html  # appendix numbers


def test_unknown_citation_does_not_leak_raw_id():
    out = _render_section_body("see [[totally_unknown]] here", cite_map={"x": 1}, pal=_PAL)
    assert "totally_unknown" not in out
    assert "chip-unknown" in out


def test_disputed_note_citation_is_converted():
    rep = _report_with_chart()
    html = _build_pdf_html(rep, None, _LIGHT)
    # the disputed_note cited df537b_p2 (passage #2) — must be a chip, not raw
    assert "df537b_p2]" not in html
    assert "Conflicts noted" in html


# ---- ④ light / dark ---------------------------------------------------------


def test_light_and_dark_html_differ():
    rep = _report_with_chart()
    light = _build_pdf_html(rep, None, resolve_theme("disco", "light"))
    dark = _build_pdf_html(rep, None, resolve_theme("disco", "dark"))
    assert light != dark
    # the dark theme's page background token appears in dark, not light
    assert "#0e0f12" in dark and "#0e0f12" not in light


@pytest.mark.skipif(not pdf_available(), reason="weasyprint not installed")
def test_pdf_renders_both_modes():
    rep = _report_with_chart()
    light = serialize_pdf(rep, None, theme="disco", mode="light")
    dark = serialize_pdf(rep, None, theme="disco", mode="dark")
    assert light[:4] == b"%PDF" and dark[:4] == b"%PDF"
    assert len(light) > 1000 and len(dark) > 1000
