"""Report export serializers — MD / PDF / DOCX (RP-07 + DR-2 / §1.5).

Exports a finished deep-research ReportEvent to three formats:
  - Markdown (ported verbatim from deepResearch.ts:88-127 — byte-identical)
  - PDF via WeasyPrint — structured HTML from the ReportEvent model (sections,
    citations, follow-ups, appendix), branded via core.brand theme engine
  - DOCX via pandoc subprocess (md → docx) inside a sandbox, with a styled
    reference.docx bundled here

The PDF path was re-written for DR-2 / §1.5:
  _build_pdf_html(report, follow_ups, theme) → structured HTML (NOT flattened
  markdown).  Prepends font_face_css() + theme_css_vars(theme) +
  print_skeleton_css() from core.brand.  The disco definition mark appears on
  the cover when theme.branded.  No nl2br; dropcap is an inline <span>.

pandoc and WeasyPrint are imported/invoked lazily so the module imports even
when the binaries/libs are absent.  These run in the AGENT-SERVER process
(this is an export endpoint over a stored, server-generated ReportEvent —
there is no sandbox in this path), so PDF needs `weasyprint` importable and
DOCX needs `pandoc` on PATH wherever the agent-server runs.
`export_capabilities()` reports which formats are actually usable so the UI
never offers a button that 500s.

The MD path is UNCHANGED (byte-identical to the pre-DR-2 baseline).
"""

from __future__ import annotations

import html as _html
import importlib.resources
import io
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Literal, cast

import markdown as _md

from disco.core import ReportEvent
from disco.core.brand import (
    definition_mark_html,
    font_face_css,
    print_skeleton_css,
    resolve_theme,
    theme_css_vars,
    wordmark_html,
)
from disco.core.brand.tokens import Theme

from disco.core.brand.chart_svg import Palette, palette_from_theme, render_chart_svg, render_chart_table

logger = logging.getLogger(__name__)

# ---- Markdown serializer (ported verbatim from deepResearch.ts:88-127) ----


def serialize_markdown(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None = None,
) -> str:
    """Serialize a ReportEvent to markdown.

    Ported VERBATIM from `serializeReportToMarkdown` in
    frontend/src/api/deepResearch.ts:88-127. Same heading levels, citation
    rendering, section order, and bounded_by honesty footer. The byte-parity
    test asserts this against a captured sample from the client-side output.

    ``follow_ups`` is an optional list of (question, answer) pairs from
    post-report follow-up Q&A turns. When provided and non-empty, a
    ``## Follow-up Q&A`` section is appended after the passages footer.
    When absent or empty the output is byte-identical to the baseline
    (backwards-compatible — the byte-parity test still passes).
    """
    lines: list[str] = []
    lines.append(f"# Deep Research: {report.query}")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(report.summary or "*(no summary)*")
    lines.append("")
    for s in report.sections:
        lines.append(f"## {s.title}")
        lines.append("")
        if s.disputed_notes and len(s.disputed_notes) > 0:
            lines.append(f"_Conflicts noted: {'; '.join(s.disputed_notes)}_")
            lines.append("")
        lines.append(s.markdown)
        lines.append("")
    if report.bounded_by:
        lines.append("---")
        lines.append("")
        lines.append(
            f"_This run was bounded by **{report.bounded_by}**. Some planned "
            f"sub-questions were not covered. Consider running the EXHAUSTIVE "
            f"tier or assigning a faster driver model for deeper coverage._"
        )
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"Passages cited ({len(report.passages)}):")
    lines.append("")
    for p in report.passages:
        pid = str(p.get("id", "?"))
        title = str(p.get("source_title", ""))
        url = str(p.get("source_url", ""))
        lines.append(f"- [{pid}] {title} — {url}")
    # WALK-20: optional follow-up Q&A section — byte-identical output when
    # follow_ups is None or empty (the byte-parity contract is preserved).
    if follow_ups:
        lines.append("")
        lines.append("## Follow-up Q&A")
        for i, (question, answer) in enumerate(follow_ups, 1):
            lines.append("")
            lines.append(f"### Follow-up {i}")
            lines.append("")
            lines.append(f"**Q:** {question}")
            lines.append("")
            lines.append(answer)
    return "\n".join(lines)


# ---- Markdown → HTML helper (kept for backward-compat) ---------------------


def _markdown_to_html(md: str, title: str = "Deep Research Report") -> str:
    """Convert a markdown document to a self-contained HTML page.

    Kept for backward-compatibility and test coverage; the PDF path now calls
    _build_pdf_html instead.
    """
    body_html = _md.markdown(
        md,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format=cast("Literal['xhtml', 'html']", "html5"),
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_html.escape(title)}</title>
<style>
  @page {{ margin: 1in; size: A4; }}
  body {{
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #1a1a1a;
  }}
  h1 {{ font-size: 1.6em; margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 0.2em; }}
  h2 {{ font-size: 1.25em; margin-top: 1.4em; border-bottom: 1px solid #ccc; }}
  p {{ margin: 0.5em 0; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }}
  pre {{ white-space: pre-wrap; font-family: ui-monospace, Menlo, monospace; font-size: 0.85em;
         background: #f6f6f6; padding: 0.8em; border-radius: 4px; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 0.9em; }}
  ul, ol {{ margin: 0.5em 0; padding-left: 1.4em; }}
  a {{ color: #0b5cad; }}
  table {{ border-collapse: collapse; margin: 0.8em 0; width: 100%; font-size: 0.95em; }}
  th, td {{ border: 1px solid #ccc; padding: 0.35em 0.6em; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


# ---- Structured HTML builder (DR-2 / §1.5) ---------------------------------


_CITE_RE = re.compile(r"\[\[([^\]]+)\]\]")
# ```chart fences hold a JSON chart spec the frontend lifts into a Chart.js canvas;
# the PDF path renders them to inline SVG (or a table) instead of a raw code block.
_CHART_FENCE_RE = re.compile(r"```chart[^\n]*\n(.*?)```", re.DOTALL)


def _citation_map(report: ReportEvent) -> dict[str, int]:
    """passage-id → 1-based citation number, by passage order — the SAME mapping
    the frontend uses (sources.ts:citationNumbers). The PDF previously leaked the
    raw internal id (e.g. ``7a6ee0_p1``) inside the chip; this turns it into ``[1]``."""
    return {str(p.get("id")): i + 1 for i, p in enumerate(report.passages) if p.get("id")}


def _cite_chip(raw_id: str, cite_map: dict[str, int] | None) -> str:
    """Render a ``[[id]]`` citation as a numbered chip linked to the sources
    appendix. Unknown ids degrade to a neutral marker (never leak the raw hash)."""
    n = (cite_map or {}).get(raw_id.strip())
    if n is not None:
        return f'<a class="chip" href="#src-{n}">{n}</a>'
    return '<span class="chip chip-unknown">∗</span>'


def _render_chart_block(raw_json: str, pal: Palette | None) -> str:
    """Render one ```chart fence body to inline SVG, falling back to a data table
    (and, only if the JSON itself is unparseable, to a labeled preformatted block)."""
    try:
        spec = json.loads(raw_json)
    except (ValueError, TypeError):
        return f'<pre class="chart-raw">{_html.escape(raw_json.strip())}</pre>'
    svg = render_chart_svg(spec, pal) if pal is not None else None
    inner = svg if svg else render_chart_table(spec)
    return f'<figure class="chart-figure">{inner}</figure>'


def _render_md_segment(text: str, cite_map: dict[str, int] | None) -> str:
    """Citation chips + markdown for a non-chart text segment."""
    text = _CITE_RE.sub(lambda m: _cite_chip(m.group(1), cite_map), text)
    return _md.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format=cast("Literal['xhtml', 'html']", "html5"),
    )


def _render_section_body(
    text: str,
    cite_map: dict[str, int] | None = None,
    pal: Palette | None = None,
) -> str:
    """Render section markdown to HTML: numbered citation chips, and ```chart
    fences lifted into inline SVG charts (with a table fallback) instead of raw
    code blocks. Non-chart segments go through python-markdown; chart segments
    are spliced in so markdown can't mangle the SVG."""
    parts: list[str] = []
    last = 0
    for m in _CHART_FENCE_RE.finditer(text):
        parts.append(_render_md_segment(text[last : m.start()], cite_map))
        parts.append(_render_chart_block(m.group(1), pal))
        last = m.end()
    parts.append(_render_md_segment(text[last:], cite_map))
    return "".join(parts)


def _cover_subtitle_text(summary: str | None, limit: int = 240) -> str:
    """Cover subtitle text trimmed at a sentence/word boundary (the old code hard-
    sliced ``summary[:200]``, cutting mid-word). Collapses whitespace; appends an
    ellipsis when it had to cut."""
    # Strip [[id]] citation markers — the cover teaser shouldn't show raw refs.
    s = " ".join(_CITE_RE.sub("", summary or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in (". ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx >= limit * 0.5:
            return cut[: idx + 1].strip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + "…"


def _build_pdf_html(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None,
    theme: Theme,
) -> str:
    """Build a branded, structured HTML document from a ReportEvent.

    This is the DR-2 / §1.5 renderer.  It iterates the STRUCTURED model
    (sections, passages, follow_ups) and emits real <section>/<h2>/.chip
    markup instead of a flattened markdown string.  Does NOT use nl2br.

    The disco definition mark appears on the cover when ``theme.branded``.
    The dropcap is an inline ``<span class="dropcap">`` (NOT ::first-letter
    float — WeasyPrint asserts on that).

    TOC is emitted when sections > 10.
    Sources appendix uses 2-col class when passages > 30.
    """
    sections = report.sections
    passages = report.passages
    n_sections = len(sections)
    n_passages = len(passages)

    # Numbered-citation map (id → [N]) + theme palette for inline SVG charts.
    cite_map = _citation_map(report)
    pal = palette_from_theme(theme)

    # ---- running header ----
    running_header = (
        '<div id="running-header">'
        f'<span class="running-wordmark">{wordmark_html()}</span>'
        "</div>"
    )

    # ---- cover ----
    query_escaped = _html.escape(report.query)
    if query_escaped:
        first_char = query_escaped[0]
        rest_title = query_escaped[1:]
        title_html = f'<span class="dropcap">{first_char}</span>{rest_title}'
    else:
        title_html = query_escaped

    cover_mark = definition_mark_html("colophon") if theme.branded else ""
    depth_label = _html.escape(report.depth_tier or "standard_deep")
    src_count = str(n_passages)
    bounded_label = _html.escape(report.bounded_by or "—")

    cover_html = (
        '<div class="cover-page">'
        '<div class="cover-rule"></div>'
        f'<div class="cover-title">{title_html}</div>'
        f'<div class="cover-subtitle">{_html.escape(_cover_subtitle_text(report.summary))}</div>'
        '<div class="cover-meta">'
        f'<span><span class="meta-label">Depth</span>&nbsp;{depth_label}</span>'
        f'<span><span class="meta-label">Sources</span>&nbsp;{src_count}</span>'
        f'<span><span class="meta-label">Bounded by</span>&nbsp;{bounded_label}</span>'
        "</div>"
        f"{cover_mark}"
        "</div>"
    )

    # ---- TOC (when > 10 sections) ----
    toc_html = ""
    if n_sections > 10:
        toc_items = "".join(
            f'<li><span class="toc-no">{str(i + 1).zfill(2)}</span>'
            f"{_html.escape(s.title)}</li>"
            for i, s in enumerate(sections)
        )
        toc_html = (
            '<div class="toc-block">'
            '<div class="toc-heading">Contents</div>'
            f'<ul class="toc-list">{toc_items}</ul>'
            "</div>"
        )

    # ---- executive summary ----
    summary_html = (
        '<div class="exec-summary">'
        f"{_render_section_body(report.summary or '', cite_map, pal)}"
        "</div>"
    )

    # ---- sections ----
    sections_html_parts: list[str] = []
    for i, s in enumerate(sections):
        no_label = str(i + 1).zfill(2)
        conflict = ""
        if s.disputed_notes:
            notes = _html.escape("; ".join(s.disputed_notes))
            # disputed_notes is plain text (escaped), but it can carry [[id]]
            # citations too — convert them to numbered chips like the body, so a
            # conflict note never leaks a raw passage id.
            notes = _CITE_RE.sub(lambda m: _cite_chip(m.group(1), cite_map), notes)
            conflict = f'<div class="conflict-note">Conflicts noted: {notes}</div>'
        body = _render_section_body(s.markdown, cite_map, pal)
        sections_html_parts.append(
            f'<section id="s{i}">'
            f'<div class="section-no">{no_label}</div>'
            f'<h2 class="section-title">{_html.escape(s.title)}</h2>'
            f"{conflict}"
            f"{body}"
            "</section>"
        )
    sections_html = "\n".join(sections_html_parts)

    # ---- bounded-by note ----
    bounded_html = ""
    if report.bounded_by:
        bounded_html = (
            '<div class="bounded-note">This run was bounded by '
            f"<strong>{_html.escape(report.bounded_by)}</strong>. "
            "Some planned sub-questions were not covered. Consider running "
            "the EXHAUSTIVE tier or assigning a faster driver model for "
            "deeper coverage.</div>"
        )

    # ---- follow-up Q&A ----
    followup_html = ""
    if follow_ups:
        items = ""
        for j, (question, answer) in enumerate(follow_ups, 1):
            answer_html = _render_section_body(answer, cite_map, pal)
            items += (
                '<div class="followup-item">'
                f'<div class="followup-q">Q {j}: {_html.escape(question)}</div>'
                f'<div class="followup-a">{answer_html}</div>'
                "</div>"
            )
        followup_html = (
            '<div class="followup-page">'
            '<div class="followup-heading">Follow-up Q&amp;A</div>'
            '<div class="followup-rule"></div>'
            f"{items}"
            "</div>"
        )

    # ---- sources appendix ----
    two_col_class = " sources-2col" if n_passages > 30 else ""
    src_items = ""
    for i, p in enumerate(passages):
        n = i + 1  # same 1-based number as the inline citation chips
        ptitle = _html.escape(str(p.get("source_title", "")))
        url = _html.escape(str(p.get("source_url", "")))
        src_items += (
            f'<li id="src-{n}">'
            f'<span class="src-id">[{n}]</span>'
            f'<a href="{url}">{ptitle}</a>'
            f" &mdash; {url}"
            "</li>"
        )
    appendix_html = (
        f'<div class="sources-appendix{two_col_class}">'
        '<div class="sources-heading">'
        f"Sources ({n_passages})</div>"
        f'<ul class="sources-list">{src_items}</ul>'
        "</div>"
    )

    # ---- assemble ----
    brand_css = font_face_css() + theme_css_vars(theme) + print_skeleton_css()

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_html.escape(report.query)}</title>\n"
        "<style>\n"
        f"{brand_css}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f"{running_header}\n"
        f"{cover_html}\n"
        f"{toc_html}\n"
        f"{summary_html}\n"
        f"{sections_html}\n"
        f"{bounded_html}\n"
        f"{followup_html}\n"
        f"{appendix_html}\n"
        "</body>\n"
        "</html>"
    )


# ---- PDF serializer (structured HTML → WeasyPrint) -------------------------


def _lazy_import_weasyprint():
    """Import WeasyPrint lazily. Raises ImportError if not installed."""
    import weasyprint

    return weasyprint


def serialize_pdf(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None = None,
    theme: str = "disco",
    mode: str = "light",
) -> bytes:
    """Serialize a ReportEvent to PDF via WeasyPrint.

    Builds structured HTML from the report model via _build_pdf_html (DR-2/
    §1.5 — sections, citations, follow-ups, appendix; no nl2br; branded
    via the core.brand engine).  Loads OFL fonts via WeasyPrint
    FontConfiguration.

    ``theme`` and ``mode`` select the brand theme (defaults: disco/light).
    Raises ValueError for an unknown theme (caller converts to HTTP 400).
    Raises ImportError if weasyprint is not installed.
    Raises RuntimeError on rendering failure.
    """
    resolved = resolve_theme(theme, mode)
    weasyprint = _lazy_import_weasyprint()
    doc_html = _build_pdf_html(report, follow_ups, resolved)
    try:
        from weasyprint.text.fonts import FontConfiguration

        font_config = FontConfiguration()
        brand_css_str = font_face_css() + theme_css_vars(resolved) + print_skeleton_css()
        css = weasyprint.CSS(string=brand_css_str, font_config=font_config)
        wp_doc = weasyprint.HTML(string=doc_html)
        pdf = wp_doc.write_pdf(stylesheets=[css], font_config=font_config)
        assert pdf is not None  # target not provided → bytes
        return pdf
    except Exception as exc:
        raise RuntimeError(f"WeasyPrint PDF generation failed: {exc}") from exc


# ---- Reference DOCX generator -----------------------------------------------


def _reference_docx_path() -> Path:
    """Return the path to the bundled reference.docx.

    The file is stored alongside this module as a static asset.  If it doesn't
    exist (first run or clean checkout), _generate_reference_docx() creates it.
    """
    pkg = importlib.resources.files("disco.agent_server")
    ref_path = Path(str(pkg)) / "reference.docx"
    if not ref_path.exists():
        _generate_reference_docx(ref_path)
    return ref_path


def _generate_reference_docx(dest: Path) -> None:
    """Generate a minimal styled reference.docx for pandoc --reference-doc.

    Creates a ZIP-based OOXML document with brand-inspired Heading1/Heading2/
    Normal/Title styles.  Pandoc reads the named styles and applies them to the
    converted document.  Uses only Python stdlib (zipfile + io).
    """
    content_types = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

    rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

    word_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
    Target="styles.xml"/>
</Relationships>"""

    document = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Disco Research Report</w:t></w:r></w:p>
  </w:body>
</w:document>"""

    # styles.xml — named styles pandoc uses (Heading1/2/3, Normal, Title, etc.)
    styles = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
        <w:sz w:val="22"/><w:szCs w:val="22"/>
        <w:color w:val="1A1813"/>
      </w:rPr>
    </w:rPrDefault>
  </w:docDefaults>

  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:pPr><w:spacing w:after="160" w:line="280" w:lineRule="auto"/></w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
      <w:sz w:val="22"/><w:szCs w:val="22"/>
      <w:color w:val="1A1813"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:spacing w:before="360" w:after="120"/>
      <w:outlineLvl w:val="0"/>
    </w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
      <w:b/><w:sz w:val="36"/><w:szCs w:val="36"/>
      <w:color w:val="1A1813"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:spacing w:before="280" w:after="80"/>
      <w:outlineLvl w:val="1"/>
    </w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
      <w:b/><w:sz w:val="28"/><w:szCs w:val="28"/>
      <w:color w:val="4077A3"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:spacing w:before="200" w:after="60"/>
      <w:outlineLvl w:val="2"/>
    </w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
      <w:b/><w:sz w:val="24"/><w:szCs w:val="24"/>
      <w:color w:val="5A5853"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
      <w:b/><w:sz w:val="48"/><w:szCs w:val="48"/>
      <w:color w:val="1A1813"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="80" w:after="200"/></w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
      <w:i/><w:sz w:val="24"/><w:szCs w:val="24"/>
      <w:color w:val="5A5853"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="BodyText">
    <w:name w:val="Body Text"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="120"/></w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>
      <w:sz w:val="22"/><w:szCs w:val="22"/>
      <w:color w:val="1A1813"/>
    </w:rPr>
  </w:style>

  <w:style w:type="character" w:styleId="Hyperlink">
    <w:name w:val="Hyperlink"/>
    <w:rPr>
      <w:color w:val="39688E"/>
      <w:u w:val="single"/>
    </w:rPr>
  </w:style>
</w:styles>"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/_rels/document.xml.rels", word_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)
    dest.write_bytes(buf.getvalue())


# ---- DOCX serializer (md → pandoc, INSIDE a sandbox) ----------------------


async def serialize_docx(
    report: ReportEvent,
    sandbox: Any,
    follow_ups: list[tuple[str, str]] | None = None,
) -> bytes:
    """Serialize a ReportEvent to DOCX via `pandoc` INSIDE a sandbox container.

    pandoc ships in the sandbox image (NOT the host), so the runtime creates a
    transient render sandbox and passes it here: the report markdown is written
    into the jailed workspace, pandoc renders it to .docx in-box, the bytes are
    read back, and the runtime destroys the throwaway sandbox. No host install,
    jailed like marp.

    DR-2 / §11.3: passes --reference-doc (a styled reference.docx bundled
    here) and --toc to pandoc for a branded, bookmarked output.
    Raises RuntimeError on failure.
    """
    md = serialize_markdown(report, follow_ups)
    await sandbox.write_file("_export.md", md.encode("utf-8"))
    # Write the reference.docx into the sandbox workspace
    ref_flag = ""
    try:
        ref_bytes = _reference_docx_path().read_bytes()
        await sandbox.write_file("_reference.docx", ref_bytes)
        ref_flag = "--reference-doc=_reference.docx"
    except Exception as exc:
        logger.warning("Could not load reference.docx: %s — proceeding without it", exc)
    toc_flag = "--toc"
    parts = ["pandoc _export.md -f markdown -t docx"]
    if ref_flag:
        parts.append(ref_flag)
    parts.append(toc_flag)
    parts.append("-o _export.docx")
    pandoc_cmd = " ".join(parts)
    res = await sandbox.exec_shell(pandoc_cmd, timeout_s=60)
    if getattr(res, "timed_out", False):
        raise RuntimeError("pandoc timed out after 60s")
    if res.exit_code != 0:
        detail = (res.stderr or "").strip() or f"exit code {res.exit_code}"
        raise RuntimeError(f"pandoc failed in the sandbox: {detail}")
    return await sandbox.read_file("_export.docx")


# ---- Format dispatch -------------------------------------------------------


_EXPORT_FORMATS = frozenset({"md", "pdf", "docx"})

MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown;charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

EXTENSIONS: dict[str, str] = {
    "md": ".md",
    "pdf": ".pdf",
    "docx": ".docx",
}


def export_report(
    report: ReportEvent,
    fmt: str,
    follow_ups: list[tuple[str, str]] | None = None,
    theme: str = "disco",
    mode: str = "light",
) -> tuple[bytes, str, str]:
    """Export a ReportEvent to MD or PDF (both rendered in-process).

    DOCX is NOT handled here — it renders inside a transient sandbox (pandoc
    ships in the sandbox image, not the host), so the runtime calls
    `serialize_docx` directly with a sandbox instance.  Returns (payload,
    media_type, suffix).  Raises ValueError for unknown formats or unknown
    theme (caller converts to 400).

    ``follow_ups`` is forwarded to the serializer so selected follow-up Q&A
    pairs appear in the exported document (WALK-20).

    ``theme`` / ``mode`` select the brand theme for PDF (default disco/light).
    MD output is byte-identical regardless of theme.  Unknown theme raises
    ValueError.
    """
    if fmt not in _EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {fmt!r}. Valid: md, pdf, docx")

    if fmt == "md":
        payload = serialize_markdown(report, follow_ups).encode("utf-8")
    elif fmt == "pdf":
        payload = serialize_pdf(report, follow_ups, theme=theme, mode=mode)
    elif fmt == "docx":
        raise ValueError("docx is rendered in a sandbox — call serialize_docx(report, sandbox)")
    else:
        raise ValueError(f"Unknown export format: {fmt!r}")  # pragma: no cover

    media_type = MEDIA_TYPES[fmt]
    ext = EXTENSIONS[fmt]
    return payload, media_type, ext


def pdf_available() -> bool:
    """True if PDF export can run in-process (weasyprint + its pango/cairo libs)."""
    try:
        _lazy_import_weasyprint()
        return True
    except Exception:
        return False
