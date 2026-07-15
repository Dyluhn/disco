"""Report export serializers — MD / PDF (RP-07 + DR-2 / §1.5).

Exports a finished deep-research ReportEvent to two formats:
  - Markdown (ported verbatim from deepResearch.ts:88-127 — byte-identical)
  - PDF via WeasyPrint — structured HTML from the ReportEvent model (sections,
    citations, follow-ups, appendix), branded via core.brand theme engine

The PDF path was re-written for DR-2 / §1.5:
  _build_pdf_html(report, follow_ups, theme) → structured HTML (NOT flattened
  markdown).  Prepends font_face_css() + theme_css_vars(theme) +
  print_skeleton_css() from core.brand.  The disco definition mark appears on
  the cover when theme.branded.  No nl2br; dropcap is an inline <span>.

WeasyPrint is imported/invoked lazily so the module imports even when the
libs are absent.  PDF renders in the AGENT-SERVER process (this is an export
endpoint over a stored, server-generated ReportEvent — there is no sandbox in
this path), so it needs `weasyprint` importable wherever the agent-server runs.
`export_capabilities()` reports which formats are actually usable so the UI
never offers a button that 500s.

The MD path is UNCHANGED (byte-identical to the pre-DR-2 baseline).
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from typing import Literal, cast

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
from disco.core.brand.chart_svg import (
    Palette,
    palette_from_theme,
    render_chart_svg,
    render_chart_table,
)
from disco.core.brand.tokens import Theme
from disco.core.events import report_truncation
from disco.core.think import strip_think_spans

logger = logging.getLogger(__name__)

# ---- Markdown serializer (ported verbatim from deepResearch.ts:88-127) ----


def serialize_markdown(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None = None,
    title: str | None = None,
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

    ``title`` — W-10: a real generated conversation title for the H1 title
    line, instead of echoing the raw user question. When ``None``/empty it
    falls back to ``report.query`` (byte-identical to the baseline — the
    byte-parity test passes ``title=None``).
    """
    display_title = (title or "").strip() or report.query
    lines: list[str] = []
    lines.append(f"# Deep Research: {display_title}")
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
    _trunc = report_truncation(report.bounded_by)
    if _trunc:
        lines.append("---")
        lines.append("")
        lines.append(
            f"_This run was bounded by **{_trunc}**. Some planned "
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
            lines.append(strip_think_spans(answer))
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
_TABLE_ALIGN_RE = re.compile(r"^:?-{3,}:?$")


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


def _split_table_row(line: str) -> list[str]:
    """Split a pipe-table row while preserving escaped pipes inside cells."""
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]

    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in row:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    cells.append("".join(buf).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(_TABLE_ALIGN_RE.fullmatch(c.strip()) for c in cells)


def _strip_wrapping_paragraph(rendered: str) -> str:
    rendered = rendered.strip()
    if rendered.startswith("<p>") and rendered.endswith("</p>"):
        return rendered[3:-4]
    return rendered


def _render_table_cell(cell: str, cite_map: dict[str, int] | None) -> str:
    cell = _CITE_RE.sub(lambda m: _cite_chip(m.group(1), cite_map), cell)
    return _strip_wrapping_paragraph(
        _md.markdown(
            cell,
            extensions=["sane_lists"],
            output_format=cast("Literal['xhtml', 'html']", "html5"),
        )
    )


def _render_table_html(
    header: list[str],
    rows: list[list[str]],
    cite_map: dict[str, int] | None,
) -> str:
    max_cols = max(len(header), *(len(row) for row in rows))
    if len(header) < max_cols:
        # Common report-output shape: the first data column is a label column
        # but the model omitted the leading blank header cell.
        header = [""] * (max_cols - len(header)) + header
    header = header[:max_cols]
    padded_rows = [(row + [""] * max_cols)[:max_cols] for row in rows]

    ths = []
    for cell in header:
        empty_cls = ' class="empty"' if not cell.strip() else ""
        ths.append(f'<th scope="col"{empty_cls}>{_render_table_cell(cell, cite_map)}</th>')
    body_rows = []
    for row in padded_rows:
        tds = "".join(f"<td>{_render_table_cell(cell, cite_map)}</td>" for cell in row)
        body_rows.append(f"<tr>{tds}</tr>")
    return (
        '<table class="report-table">'
        f"<thead><tr>{''.join(ths)}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )


def _render_markdown_tables(text: str, cite_map: dict[str, int] | None) -> str:
    """Normalize pipe tables before Python-Markdown can shift ragged headers."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines) and "|" in lines[i] and _is_table_separator(lines[i + 1]):
            header = _split_table_row(lines[i])
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                if _is_table_separator(lines[j]):
                    break
                rows.append(_split_table_row(lines[j]))
                j += 1
            if rows:
                out.append(_render_table_html(header, rows, cite_map))
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _render_md_segment(text: str, cite_map: dict[str, int] | None) -> str:
    """Citation chips + markdown for a non-chart text segment."""
    text = _render_markdown_tables(text, cite_map)
    text = _CITE_RE.sub(lambda m: _cite_chip(m.group(1), cite_map), text)
    return _md.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format=cast("Literal['xhtml', 'html']", "html5"),
    )


_RAW_RESOURCE_TAG_RE = re.compile(
    r"(?is)<\s*(?:img|link|iframe|object|embed|source|video|audio|script|style)\b[^>]*>.*?"
    r"(?:<\s*/\s*(?:iframe|object|embed|video|audio|script|style)\s*>)?"
)
_RAW_HTML_TAG_RE = re.compile(r"(?is)<\s*/?\s*[a-zA-Z!][^>]*>")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_CSS_URL_RE = re.compile(r"(?is)url\(\s*[^)]*\)")


def _sanitize_report_markdown(text: str) -> str:
    text = _MD_IMAGE_RE.sub(lambda m: _html.escape(m.group(1)), text)
    text = _RAW_RESOURCE_TAG_RE.sub("", text)
    text = _CSS_URL_RE.sub("", text)
    return _RAW_HTML_TAG_RE.sub("", text)


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
    text = _sanitize_report_markdown(text)
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


def _cover_meta_html(report: ReportEvent, n_passages: int) -> str:
    items = [
        '<span><span class="meta-label">Depth</span>&nbsp;'
        f"{_html.escape(report.depth_tier or 'standard_deep')}</span>",
        f'<span><span class="meta-label">Sources</span>&nbsp;{n_passages}</span>',
    ]
    _trunc = report_truncation(report.bounded_by)
    if _trunc:
        items.append(
            f'<span><span class="meta-label">Bounded by</span>&nbsp;{_html.escape(_trunc)}</span>'
        )
    return "".join(items)


def _build_pdf_html(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None,
    theme: Theme,
    title: str | None = None,
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
        f'<div id="running-header"><span class="running-wordmark">{wordmark_html()}</span></div>'
    )

    # ---- cover ----
    # W-10: the cover title page uses the real generated title, falling back to
    # the raw question only when no title is available.
    display_title = (title or "").strip() or report.query
    title_escaped = _html.escape(display_title)
    if title_escaped:
        first_char = title_escaped[0]
        rest_title = title_escaped[1:]
        title_html = f'<span class="dropcap">{first_char}</span>{rest_title}'
    else:
        title_html = title_escaped

    cover_mark = definition_mark_html("colophon") if theme.branded else ""
    cover_meta = _cover_meta_html(report, n_passages)

    cover_html = (
        '<div class="cover-page">'
        '<div class="cover-rule"></div>'
        f'<div class="cover-title">{title_html}</div>'
        f'<div class="cover-subtitle">{_html.escape(_cover_subtitle_text(report.summary))}</div>'
        '<div class="cover-meta">'
        f"{cover_meta}"
        "</div>"
        f"{cover_mark}"
        "</div>"
    )

    # ---- TOC (when > 10 sections) ----
    toc_html = ""
    if n_sections > 10:
        toc_items = "".join(
            f'<li><span class="toc-no">{str(i + 1).zfill(2)}</span>{_html.escape(s.title)}</li>'
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
            '<header class="section-heading">'
            f'<div class="section-no">{no_label}</div>'
            f'<h2 class="section-title">{_html.escape(s.title)}</h2>'
            "</header>"
            f"{conflict}"
            f"{body}"
            "</section>"
        )
    sections_html = "\n".join(sections_html_parts)

    # ---- bounded-by note ----
    bounded_html = ""
    _trunc = report_truncation(report.bounded_by)
    if _trunc:
        bounded_html = (
            '<div class="bounded-note">This run was bounded by '
            f"<strong>{_html.escape(_trunc)}</strong>. "
            "Some planned sub-questions were not covered. Consider running "
            "the EXHAUSTIVE tier or assigning a faster driver model for "
            "deeper coverage.</div>"
        )

    # ---- follow-up Q&A ----
    followup_html = ""
    if follow_ups:
        items = ""
        for j, (question, answer) in enumerate(follow_ups, 1):
            answer_html = _render_section_body(strip_think_spans(answer), cite_map, pal)
            items += (
                '<div class="followup-item">'
                f'<div class="followup-q">Q {j}: {_html.escape(question)}</div>'
                f'<div class="followup-a">{answer_html}</div>'
                "</div>"
            )
        followup_html = (
            '<div class="followup-page">'
            '<div class="followup-head">'
            '<div class="followup-heading">Follow-up Q&amp;A</div>'
            '<div class="followup-rule"></div>'
            "</div>"
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
        (
            f'<div class="sources-appendix{two_col_class}">'
            '<div class="sources-heading">'
            f"Sources ({n_passages})</div>"
            f'<ul class="sources-list">{src_items}</ul>'
            "</div>"
        )
        if n_passages
        else ""
    )

    # ---- assemble ----
    brand_css = font_face_css() + theme_css_vars(theme) + print_skeleton_css()

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title_escaped}</title>\n"
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


def _report_pdf_url_fetcher(url: str, *args, **kwargs):
    if str(url).startswith("data:"):
        from weasyprint import URLFetcher

        # We embed the licensed fonts as data URIs and reject every external
        # scheme. Pin redirect behavior off explicitly: WeasyPrint 69 removed
        # the old default fetcher's redirect semantics for security reasons.
        return URLFetcher(allowed_protocols={"data"}, allow_redirects=False).fetch(
            url, *args, **kwargs
        )
    raise ValueError("external PDF resource fetch blocked")


def serialize_pdf(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None = None,
    theme: str = "disco",
    mode: str = "light",
    title: str | None = None,
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
    # _build_pdf_html already injects all brand CSS as an inline <style> block;
    # passing the same CSS again as a WeasyPrint stylesheet doubled every rule
    # and @font-face declaration, causing glitchy PDF output.  We rely solely
    # on the inline CSS here and pass only font_config so WeasyPrint's Pango
    # engine picks up the embedded OFL font families.
    doc_html = _build_pdf_html(report, follow_ups, resolved, title)
    try:
        from weasyprint.text.fonts import FontConfiguration

        font_config = FontConfiguration()
        wp_doc = weasyprint.HTML(string=doc_html, url_fetcher=_report_pdf_url_fetcher)
        pdf = wp_doc.write_pdf(font_config=font_config)
        assert pdf is not None  # target not provided → bytes
        return pdf
    except Exception as exc:
        raise RuntimeError(f"WeasyPrint PDF generation failed: {exc}") from exc


# ---- Format dispatch -------------------------------------------------------


_EXPORT_FORMATS = frozenset({"md", "pdf"})

MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown;charset=utf-8",
    "pdf": "application/pdf",
}

EXTENSIONS: dict[str, str] = {
    "md": ".md",
    "pdf": ".pdf",
}


def export_report(
    report: ReportEvent,
    fmt: str,
    follow_ups: list[tuple[str, str]] | None = None,
    theme: str = "disco",
    mode: str = "light",
    title: str | None = None,
) -> tuple[bytes, str, str]:
    """Export a ReportEvent to MD or PDF (both rendered in-process).

    Returns (payload, media_type, suffix).  Raises ValueError for unknown
    formats or unknown theme (caller converts to 400).

    ``follow_ups`` is forwarded to the serializer so selected follow-up Q&A
    pairs appear in the exported document (WALK-20).

    ``theme`` / ``mode`` select the brand theme for PDF (default disco/light).
    MD output is byte-identical regardless of theme.  Unknown theme raises
    ValueError.
    """
    if fmt not in _EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {fmt!r}. Valid: md, pdf")

    if fmt == "md":
        payload = serialize_markdown(report, follow_ups, title).encode("utf-8")
    elif fmt == "pdf":
        payload = serialize_pdf(report, follow_ups, theme=theme, mode=mode, title=title)
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
