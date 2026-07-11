"""C8 — chart and table slide layout helpers (PPTX + HTML).

C1 INTEGRATED: ChartSpec / TableSpec are imported from _deck_schema (C1).
This module provides chart/table rendering only — no schema duplication.

HTML rendering:
  Uses render_chart_svg / render_chart_table from disco.core.brand.chart_svg
  (legal downward import: tools → core).  Falls back to render_chart_table
  when the SVG renderer returns None (empty/bad data).

PPTX rendering:
  Native python-pptx charts for bar → COLUMN_CLUSTERED, line → LINE,
  pie → PIE, scatter → XY_SCATTER.  Falls back to a native PPTX table on
  empty/malformed data.

Layering: disco.tools → disco.core (legal downward import).
          Does NOT import _pptx_render — no circular dependency.
"""

from __future__ import annotations

import html as _html
from typing import TYPE_CHECKING, Any

from disco.core.brand.chart_svg import (
    palette_from_theme,
    render_chart_svg,
    render_chart_table,
)

# C1 integration: import from _deck_schema, not local definitions
from disco.tools.builtin._deck_schema import ChartSpec, TableSpec

if TYPE_CHECKING:
    from disco.core.brand.tokens import Theme

# ---------------------------------------------------------------------------
# EMU geometry (mirrored from _pptx_render — no import to avoid circular dep)
# ---------------------------------------------------------------------------

_SLIDE_W = 12_192_000
_SLIDE_H = 6_858_000
_MARGIN = 457_200
_CW = _SLIDE_W - 2 * _MARGIN   # 11 277 600
_CH = _SLIDE_H - 2 * _MARGIN   # 5 943 600
_TITLE_H = 914_400


# ---------------------------------------------------------------------------
# Tiny PPTX helpers (mirrored to avoid circular import with _pptx_render)
# ---------------------------------------------------------------------------

def _rgb(hex_color: str):  # type: ignore[return]
    """RGBColor from a #rrggbb string (lazy pptx import)."""
    from pptx.dml.color import RGBColor
    h = hex_color.lstrip("#")
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _first_font(stack: str) -> str:
    return stack.split(",")[0].strip().strip("'\"")


def _add_tb(slide: Any, left: int, top: int, w: int, h: int) -> Any:
    from pptx.util import Emu
    shape = slide.shapes.add_textbox(Emu(left), Emu(top), Emu(w), Emu(h))
    tf = shape.text_frame
    tf.word_wrap = True
    return tf


def _run(run: Any, text: str, font: str, pt: float, color: str, bold: bool = False) -> None:
    from pptx.util import Pt
    run.text = text
    run.font.name = font
    run.font.size = Pt(pt)
    run.font.color.rgb = _rgb(color)
    run.font.bold = bold


def _accent_bar(slide: Any, left: int, top: int, w: int, color: str) -> None:
    from pptx.util import Emu
    bar = slide.shapes.add_shape(1, Emu(left), Emu(top), Emu(w), Emu(91_440))
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(color)
    bar.line.fill.background()


def _set_bg(slide: Any, color: str) -> None:
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = _rgb(color)


def _title_strip(prs_slide: Any, title: str, theme: Theme) -> int:
    """Render title textbox + accent rule; return y-coord for content area."""
    from pptx.enum.text import PP_ALIGN
    _set_bg(prs_slide, theme.bg)
    tf = _add_tb(prs_slide, _MARGIN, _MARGIN, _CW, _TITLE_H)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    _run(r, title, _first_font(theme.font_ui), 28, theme.text, bold=True)
    bar_top = _MARGIN + _TITLE_H + 45_720
    _accent_bar(prs_slide, _MARGIN, bar_top, _CW, theme.accent)
    return bar_top + 91_440 + 91_440   # bar height (0.1 in) + 0.1 in gap


def _maybe_notes(prs_slide: Any, slide: Any) -> None:
    notes = getattr(slide, "notes", None)
    if notes:
        prs_slide.notes_slide.notes_text_frame.text = notes


# ---------------------------------------------------------------------------
# Bridge: ChartSpec → chart_svg.py dict format
# ---------------------------------------------------------------------------

def _to_svg_dict(spec: ChartSpec) -> dict[str, Any]:
    """Convert ChartSpec → {chart_type, data, title, x_label, y_label} for render_chart_svg."""
    data: list[dict[str, Any]] = []

    if spec.kind == "scatter":
        for s in spec.series:
            grp = str(s.get("name", "Default"))
            for pt in s.get("data", []):
                try:
                    if isinstance(pt, dict):
                        data.append({
                            "x": float(pt.get("x") or 0),
                            "y": float(pt.get("y") or 0),
                            "group": grp,
                        })
                    elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        data.append({
                            "x": float(pt[0]),  # type: ignore[arg-type]
                            "y": float(pt[1]),  # type: ignore[arg-type]
                            "group": grp,
                        })
                    # flat scalar — no positional meaning for scatter; skip
                except (TypeError, ValueError):
                    pass
    else:
        # bar / line / pie — flatten first series against labels
        if spec.series:
            first_vals = spec.series[0].get("data", [])
            for i, lbl in enumerate(spec.labels):
                if i < len(first_vals):
                    try:
                        data.append({"label": str(lbl), "value": float(first_vals[i])})  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        pass

    return {
        "chart_type": spec.kind,
        "data": data,
        "title": spec.title,
        "x_label": "",
        "y_label": "",
    }


# ---------------------------------------------------------------------------
# HTML content generators
# ---------------------------------------------------------------------------

def html_chart_content(title: str, spec: ChartSpec, theme: Theme) -> str:
    """Inner HTML for a chart slide.

    Embeds the SVG from render_chart_svg.  Falls back to render_chart_table
    when the SVG renderer returns None (empty or malformed data).
    """
    pal = palette_from_theme(theme)
    svg_dict = _to_svg_dict(spec)
    svg = render_chart_svg(svg_dict, pal)
    chart_html = svg if svg is not None else render_chart_table(svg_dict)
    return (
        f'<h2 class="slide-heading">{_html.escape(title)}</h2>\n'
        f'<div class="slide-rule"></div>\n'
        f'<div class="slide-chart">{chart_html}</div>'
    )


def html_table_content(title: str, spec: TableSpec, theme: Theme) -> str:
    """Inner HTML for a table slide — native HTML table, not chart fallback."""
    has_header = bool(spec.headers)
    rows = list(spec.rows) if spec.rows else []
    # Renderable with EITHER headers or rows — a headerless-but-populated table (the
    # comparison-table case) must still render, not collapse to "(no table data)".
    if not has_header and not rows:
        return (
            f'<h2 class="slide-heading">{_html.escape(title)}</h2>\n'
            f'<div class="slide-rule"></div>\n'
            f'<p class="slide-table-empty">(no table data)</p>'
        )
    thead = (
        f"<thead><tr>{''.join(f'<th>{_html.escape(h)}</th>' for h in spec.headers)}</tr></thead>"
        if has_header
        else ""
    )
    tbody_rows = "".join(
        "<tr>" + "".join(f"<td>{_html.escape(str(c))}</td>" for c in row) + "</tr>"
        for row in rows
    )
    table_html = (
        f'<table class="slide-table">'
        f"{thead}"
        f"<tbody>{tbody_rows}</tbody>"
        f"</table>"
    )
    return (
        f'<h2 class="slide-heading">{_html.escape(title)}</h2>\n'
        f'<div class="slide-rule"></div>\n'
        f'<div class="slide-table-wrap">{table_html}</div>'
    )


# ---------------------------------------------------------------------------
# PPTX helpers — chart slide internals
# ---------------------------------------------------------------------------

def _add_category_chart(
    prs_slide: Any, spec: ChartSpec, left: int, top: int, w: int, h: int
) -> bool:
    """Add a bar/line/pie chart via CategoryChartData. Returns True on success."""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Emu

    _TYPE_MAP = {
        "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "line": XL_CHART_TYPE.LINE,
        "pie": XL_CHART_TYPE.PIE,
    }
    xl_type = _TYPE_MAP.get(spec.kind, XL_CHART_TYPE.COLUMN_CLUSTERED)

    cd = CategoryChartData()
    cd.categories = spec.labels if spec.labels else [""]

    has_data = False
    for s in spec.series:
        name = str(s.get("name", ""))
        vals = s.get("data", [])
        if vals:
            try:
                cd.add_series(name, tuple(float(v) for v in vals))
                has_data = True
            except (TypeError, ValueError):
                pass

    if not has_data:
        return False

    prs_slide.shapes.add_chart(xl_type, Emu(left), Emu(top), Emu(w), Emu(h), cd)
    return True


def _add_scatter_chart(
    prs_slide: Any, spec: ChartSpec, left: int, top: int, w: int, h: int
) -> bool:
    """Add a scatter chart via XyChartData. Returns True on success."""
    from pptx.chart.data import XyChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Emu

    xy = XyChartData()
    has_data = False

    for s in spec.series:
        xy_series = xy.add_series(str(s.get("name", "")))
        for i, pt in enumerate(s.get("data", [])):
            try:
                if isinstance(pt, dict):
                    xy_series.add_data_point(
                        float(pt.get("x") or 0),  # type: ignore[arg-type]
                        float(pt.get("y") or 0),  # type: ignore[arg-type]
                    )
                elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    xy_series.add_data_point(float(pt[0]), float(pt[1]))  # type: ignore[arg-type]
                else:
                    xy_series.add_data_point(float(i), float(pt))  # type: ignore[arg-type]
                has_data = True
            except (TypeError, ValueError):
                pass

    if not has_data:
        return False

    prs_slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Emu(left), Emu(top), Emu(w), Emu(h), xy)
    return True


def _chart_fallback_pptx_table(
    prs_slide: Any, spec: ChartSpec, left: int, top: int, w: int, h: int, theme: Theme
) -> None:
    """Fallback: render chart data as a native PPTX table when chart render fails."""
    from pptx.util import Emu, Pt

    n_cols = 1 + len(spec.labels)  # "Series" col + one per label
    n_rows = len(spec.series) + 1  # header + one per series

    if n_rows < 2 or n_cols < 2:
        _chart_empty_placeholder(prs_slide, left, top, w, h, theme)
        return

    tbl = prs_slide.shapes.add_table(n_rows, n_cols, Emu(left), Emu(top), Emu(w), Emu(h))
    table = tbl.table

    # Header row
    table.cell(0, 0).text = "Series"
    for j, lbl in enumerate(spec.labels):
        if j + 1 < n_cols:
            table.cell(0, j + 1).text = str(lbl)

    for j in range(n_cols):
        cell = table.cell(0, j)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb(theme.accent)
        for para in cell.text_frame.paragraphs:
            for run in para.runs:
                run.font.color.rgb = _rgb("#ffffff")
                run.font.bold = True
                run.font.size = Pt(11)

    # Series rows — explicit theme fill/text (same dark-theme contrast fix as
    # layout_table_slide_pptx; the default light banding is never trusted).
    for i, s in enumerate(spec.series):
        table.cell(i + 1, 0).text = str(s.get("name", ""))
        for j, v in enumerate(s.get("data", [])):
            if j + 1 < n_cols:
                table.cell(i + 1, j + 1).text = str(v)
        for j in range(n_cols):
            cell = table.cell(i + 1, j)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(theme.surface_1)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(11)
                    run.font.color.rgb = _rgb(theme.text)


def _chart_empty_placeholder(
    prs_slide: Any, left: int, top: int, w: int, h: int, theme: Theme
) -> None:
    """Placeholder box when no chart/table data is available."""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu

    ph_h = min(h, 914_400)  # cap at 1 in so it doesn't fill the slide
    box = prs_slide.shapes.add_shape(1, Emu(left), Emu(top), Emu(w), Emu(ph_h))
    box.fill.solid()
    box.fill.fore_color.rgb = _rgb(theme.surface_2)
    box.line.color.rgb = _rgb(theme.hairline)
    tf = box.text_frame
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    _run(r, "[no data]", _first_font(theme.font_ui), 12, theme.text_faint)


# ---------------------------------------------------------------------------
# PPTX layout — chart slide
# ---------------------------------------------------------------------------

def layout_chart_slide_pptx(prs_slide: Any, slide: Any, theme: Theme) -> None:
    """Title-strip + native python-pptx chart.

    Supports bar → COLUMN_CLUSTERED, line → LINE, pie → PIE, scatter → XY_SCATTER.
    Falls back to a native PPTX table when data is empty or malformed so the
    slide always carries a real, editable shape (never a bare error).

    ``slide`` is duck-typed: must have ``.title`` (str), ``.chart`` (ChartSpec|None),
    and ``.notes`` (str|None).  Compatible with both C1 ``Slide`` and ``DeckSlide``.
    """
    content_top = _title_strip(prs_slide, getattr(slide, "title", ""), theme)
    chart_h = _SLIDE_H - content_top - _MARGIN
    spec: ChartSpec | None = getattr(slide, "chart", None)

    if spec is None or not spec.series:
        _chart_empty_placeholder(prs_slide, _MARGIN, content_top, _CW, chart_h, theme)
        _maybe_notes(prs_slide, slide)
        return

    ok = False
    try:
        if spec.kind == "scatter":
            ok = _add_scatter_chart(prs_slide, spec, _MARGIN, content_top, _CW, chart_h)
        else:
            ok = _add_category_chart(prs_slide, spec, _MARGIN, content_top, _CW, chart_h)
    except Exception:
        ok = False

    if not ok:
        _chart_fallback_pptx_table(
            prs_slide, spec, _MARGIN, content_top, _CW, chart_h, theme
        )
    _maybe_notes(prs_slide, slide)


# ---------------------------------------------------------------------------
# PPTX layout — table slide
# ---------------------------------------------------------------------------

def layout_table_slide_pptx(prs_slide: Any, slide: Any, theme: Theme) -> None:
    """Title-strip + native python-pptx table (real, editable table shape).

    Header row gets accent fill with white text.  Data rows use theme.text.

    ``slide`` is duck-typed: must have ``.title`` (str), ``.table`` (TableSpec|None),
    and ``.notes`` (str|None).  Compatible with both C1 ``Slide`` and ``DeckSlide``.
    """
    from pptx.util import Emu, Pt

    content_top = _title_strip(prs_slide, getattr(slide, "title", ""), theme)
    spec: TableSpec | None = getattr(slide, "table", None)
    tbl_h = _SLIDE_H - content_top - _MARGIN

    # A table is renderable when it has EITHER headers or rows. The old gate bailed on
    # empty `headers` alone and discarded a fully-populated `rows` (gauntlet 2026-07-07:
    # a 7-row comparison table with headers==[] rendered as "[no data]", losing every
    # row). Only fall through to the placeholder when there is genuinely nothing.
    has_header = bool(spec and spec.headers)
    data_rows = list(spec.rows) if spec and spec.rows else []
    n_cols = (
        len(spec.headers) if spec is not None and spec.headers
        else max((len(r) for r in data_rows), default=0)
    )
    if spec is None or n_cols == 0 or (not has_header and not data_rows):
        _chart_empty_placeholder(prs_slide, _MARGIN, content_top, _CW, tbl_h, theme)
        _maybe_notes(prs_slide, slide)
        return

    row_offset = 1 if has_header else 0
    n_rows = len(data_rows) + row_offset

    tbl = prs_slide.shapes.add_table(
        n_rows, n_cols,
        Emu(_MARGIN), Emu(content_top), Emu(_CW), Emu(tbl_h),
    )
    table = tbl.table

    # Header row — accent background, white bold text (only when headers supplied).
    if has_header:
        for j, hdr in enumerate(spec.headers):
            cell = table.cell(0, j)
            cell.text = str(hdr)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(theme.accent)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.color.rgb = _rgb("#ffffff")
                    run.font.bold = True
                    run.font.size = Pt(12)

    # Data rows. In the headerless case the first column carries the row labels
    # (dimension names in a comparison table), so bold it for scannability instead
    # of leaving a flat, hard-to-read grid.
    # Cell fill is set EXPLICITLY from the theme: python-pptx's default table style
    # is a light banded fill, so on a dark theme the near-white ``theme.text`` runs
    # were invisible on the default light cells (gauntlet e-web 2026-07-07 — data
    # rows unreadable). surface_1 + theme.text is self-consistent on any theme,
    # matching the two_by_two quadrant treatment.
    for i, row in enumerate(data_rows):
        for j in range(n_cols):
            val = str(row[j]) if j < len(row) else ""
            cell = table.cell(i + row_offset, j)
            cell.text = val
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(theme.surface_1)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(11)
                    run.font.color.rgb = _rgb(theme.text)
                    if not has_header and j == 0:
                        run.font.bold = True

    _maybe_notes(prs_slide, slide)
