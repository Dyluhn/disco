"""Server-side chart rendering for the PDF export (DR report ``​```chart`` fences).

The Deep Research report sections embed charts as fenced ``​```chart`` blocks holding a
small JSON spec ({chart_type, title, x_label, y_label, data}).  The FRONTEND lifts
these into real Chart.js canvases (ChartBlock.tsx); the PDF path has no JavaScript,
so WeasyPrint would otherwise show the raw JSON as a code block.

This module renders the SAME spec to **inline SVG** (WeasyPrint renders SVG
natively, vector-crisp, no extra dependency), theme-aware via a palette derived
from the resolved brand Theme so a dark-mode PDF gets dark-mode charts.  When the
data can't be charted (unknown type / no numeric values) the caller falls back to
a styled table — never a raw code block.

Supported chart_type: bar, line, pie, scatter (the four ChartBlock handles).
Datum shapes: {label, value} (bar/line/pie) and {x, y, group} (scatter).
"""

from __future__ import annotations

import html as _html
import math
from dataclasses import dataclass
from typing import Any

_FALLBACK_SERIES = ("#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899")

_W, _H = 720.0, 420.0
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 72.0, 36.0, 88.0, 96.0
_MAX_POINTS = 24  # keep a printable density; note truncation when exceeded


@dataclass(frozen=True)
class Palette:
    """Colors pulled from the resolved Theme so charts honor light/dark mode."""

    text: str
    muted: str
    faint: str
    accent: str
    grid: str
    bg: str
    series: tuple[str, ...]
    font_ui: str


def palette_from_theme(theme: Any) -> Palette:
    accent = getattr(theme, "accent", "#4077a3")
    series = (
        accent,
        getattr(theme, "link", accent),
        getattr(theme, "verify_supported", "#397852"),
        getattr(theme, "verify_weak", "#a67f38"),
        getattr(theme, "verify_unsupported", "#b14e49"),
        getattr(theme, "text_muted", "#5a5853"),
    )
    return Palette(
        text=getattr(theme, "text", "#1a1813"),
        muted=getattr(theme, "text_muted", "#5a5853"),
        faint=getattr(theme, "text_faint", "#878682"),
        accent=accent,
        grid=getattr(theme, "hairline", "#dfdedb"),
        bg=getattr(theme, "bg", "#fcfcfa"),
        series=tuple(c for c in series if isinstance(c, str)) or _FALLBACK_SERIES,
        font_ui=getattr(theme, "font_ui", "system-ui,sans-serif"),
    )


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _esc(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""))


def _attr(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def _ellipsize(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _wrap_words(text: str, max_chars: int, max_lines: int) -> list[str]:
    words = str(text or "").split()
    if not words or max_lines <= 0:
        return []
    lines: list[str] = []
    cur = ""
    consumed = 0
    for i, word in enumerate(words):
        candidate = word if not cur else f"{cur} {word}"
        if len(candidate) <= max_chars:
            cur = candidate
            consumed = i + 1
            continue
        if cur:
            lines.append(cur)
            if len(lines) == max_lines:
                consumed = i
                break
            cur = word
            consumed = i + 1
        else:
            lines.append(_ellipsize(word, max_chars))
            cur = ""
            consumed = i + 1
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(_ellipsize(cur, max_chars))
    if consumed < len(words) and lines:
        remainder = " ".join(words[consumed:])
        lines[-1] = _ellipsize(f"{lines[-1]} {remainder}".strip(), max_chars)
    return lines[:max_lines]


def _text_lines(
    x: float,
    y: float,
    lines: list[str],
    *,
    anchor: str,
    size: float,
    fill: str,
    pal: Palette,
    weight: str | None = None,
) -> str:
    if not lines:
        return ""
    weight_attr = f' font-weight="{_attr(weight)}"' if weight else ""
    tspans = []
    for i, line in enumerate(lines):
        dy = "0" if i == 0 else f"{size * 1.18:.1f}"
        tspans.append(f'<tspan x="{x:.1f}" dy="{dy}">{_esc(line)}</tspan>')
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{_attr(anchor)}" '
        f'font-family="{_attr(pal.font_ui)}" font-size="{size:.1f}"'
        f'{weight_attr} fill="{_attr(fill)}">{"".join(tspans)}</text>'
    )


def _label_value_pairs(data: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """Coerce bar/line/pie data → [(label, value)], dropping non-numeric rows."""
    out: list[tuple[str, float]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        raw_val = d.get("value", d.get("y"))
        v = _num(raw_val)
        if v is None:
            continue
        label = d.get("label", d.get("x", ""))
        out.append((str(label), v))
    return out


def _frame(title: str, x_label: str, y_label: str, pal: Palette, inner: str) -> str:
    title_el = _text_lines(
        _W / 2,
        22,
        _wrap_words(title, 62, 3),
        anchor="middle",
        size=15,
        fill=pal.text,
        pal=pal,
        weight="600",
    )
    x_el = _text_lines(
        (_PAD_L + (_W - _PAD_R)) / 2,
        _H - 18,
        _wrap_words(x_label, 56, 2),
        anchor="middle",
        size=11,
        fill=pal.muted,
        pal=pal,
    )
    y_el = (
        f'<text transform="translate(15,{(_PAD_T + (_H - _PAD_B)) / 2:.0f}) rotate(-90)" '
        f'text-anchor="middle" font-family="{_attr(pal.font_ui)}" font-size="11" '
        f'fill="{_attr(pal.muted)}">{_esc(y_label)}</text>'
        if y_label
        else ""
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W:.0f} {_H:.0f}" '
        f'width="100%" role="img" style="background:transparent">'
        f"{title_el}{x_el}{y_el}{inner}</svg>"
    )


def _axes_grid(pal: Palette, vmax: float) -> tuple[str, float, float, float, float]:
    """Y gridlines + value ticks. Returns (svg, x0, y0, plot_w, plot_h)."""
    x0, y0 = _PAD_L, _H - _PAD_B
    plot_w, plot_h = _W - _PAD_L - _PAD_R, _H - _PAD_T - _PAD_B
    parts = [
        f'<line x1="{x0}" y1="{_PAD_T}" x2="{x0}" y2="{y0}" stroke="{pal.grid}" stroke-width="1"/>'
    ]
    for i in range(5):
        gy = y0 - plot_h * i / 4
        val = vmax * i / 4
        parts.append(
            f'<line x1="{x0}" y1="{gy:.1f}" x2="{x0 + plot_w:.1f}" '
            f'y2="{gy:.1f}" stroke="{pal.grid}" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{x0 - 6:.1f}" y="{gy + 3:.1f}" text-anchor="end" '
            f'font-family="{_attr(pal.font_ui)}" font-size="9" '
            f'fill="{_attr(pal.faint)}">{val:.0f}</text>'
        )
    return "".join(parts), x0, y0, plot_w, plot_h


def _render_bar(pairs: list[tuple[str, float]], title, x_label, y_label, pal: Palette) -> str:
    pairs = pairs[:_MAX_POINTS]
    vmax = max((v for _, v in pairs), default=0.0) or 1.0
    grid, x0, y0, plot_w, plot_h = _axes_grid(pal, vmax)
    n = len(pairs)
    slot = plot_w / n
    bw = min(slot * 0.7, 48)
    bars = []
    for i, (label, v) in enumerate(pairs):
        bh = plot_h * (v / vmax)
        bx = x0 + slot * i + (slot - bw) / 2
        by = y0 - bh
        bars.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
            f'fill="{_attr(pal.series[0])}" rx="2"/>'
        )
        label_chars = max(8, min(18, int(slot / 6.2)))
        bars.append(
            _text_lines(
                bx + bw / 2,
                y0 + 14,
                _wrap_words(label, label_chars, 2),
                anchor="middle",
                size=8,
                fill=pal.muted,
                pal=pal,
            )
        )
    return _frame(title, x_label, y_label, pal, grid + "".join(bars))


def _render_line(pairs: list[tuple[str, float]], title, x_label, y_label, pal: Palette) -> str:
    pairs = pairs[:_MAX_POINTS]
    vmax = max((v for _, v in pairs), default=0.0) or 1.0
    grid, x0, y0, plot_w, plot_h = _axes_grid(pal, vmax)
    n = len(pairs)
    step = plot_w / max(n - 1, 1)
    pts = [(x0 + step * i, y0 - plot_h * (v / vmax)) for i, (_, v) in enumerate(pairs)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{_attr(pal.series[0])}"/>' for x, y in pts
    )
    labels = "".join(
        _text_lines(
            pts[i][0],
            y0 + 14,
            _wrap_words(lbl, max(8, min(16, int(step / 6.2) if step else 16)), 2),
            anchor="middle",
            size=8,
            fill=pal.muted,
            pal=pal,
        )
        for i, (lbl, _) in enumerate(pairs)
    )
    line = (
        f'<polyline points="{poly}" fill="none" stroke="{_attr(pal.series[0])}" stroke-width="2"/>'
    )
    return _frame(title, x_label, y_label, pal, grid + line + dots + labels)


def _render_pie(pairs: list[tuple[str, float]], title, x_label, y_label, pal: Palette) -> str:
    pairs = [(lbl, v) for lbl, v in pairs if v > 0][: len(pal.series) * 2]
    total = sum(v for _, v in pairs)
    if total <= 0:
        return _frame(title, "", "", pal, "")
    cx, cy, r = 240.0, _PAD_T + 130, 110.0
    a0 = -math.pi / 2
    slices, legend = [], []
    for i, (label, v) in enumerate(pairs):
        frac = v / total
        a1 = a0 + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x2, y2 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        color = pal.series[i % len(pal.series)]
        slices.append(
            f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
            f'A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{_attr(color)}"/>'
        )
        ly = _PAD_T + 18 + i * 20
        legend.append(
            f'<rect x="480" y="{ly - 9:.0f}" width="11" height="11" rx="2" fill="{_attr(color)}"/>'
            f'<text x="497" y="{ly:.0f}" font-family="{_attr(pal.font_ui)}" '
            f'font-size="10" fill="{_attr(pal.text)}">'
            f"{_esc(_ellipsize(label, 24))} ({frac * 100:.0f}%)</text>"
        )
    return _frame(title, "", "", pal, "".join(slices) + "".join(legend))


def _scatter_points(data: list[dict[str, Any]]) -> list[tuple[float, float, str]]:
    """Coerce scatter data → [(x, y, group)], dropping non-numeric/incomplete
    rows and capping the printable density."""
    pts = [
        (_num(d.get("x")), _num(d.get("y")), str(d.get("group", "Default")))
        for d in data
        if isinstance(d, dict)
    ]
    return [(x, y, g) for x, y, g in pts if x is not None and y is not None][: _MAX_POINTS * 4]


def _scatter_bounds(
    pts: list[tuple[float, float, str]],
) -> tuple[float, float, float, float, float]:
    """Axis bounds + spans for scatter scaling. Returns (xmin, xr, ymin, yr, ymax)."""
    xs = [x for x, _, _ in pts]
    ys = [y for _, y, _ in pts]
    xmin, xmax = min(xs), max(xs) or 1.0
    ymin, ymax = min(ys), max(ys) or 1.0
    xr = (xmax - xmin) or 1.0
    yr = (ymax - ymin) or 1.0
    return xmin, xr, ymin, yr, ymax


def _scatter_dots_svg(
    pts: list[tuple[float, float, str]],
    groups: list[str],
    x0: float,
    y0: float,
    plot_w: float,
    plot_h: float,
    xmin: float,
    xr: float,
    ymin: float,
    yr: float,
    pal: Palette,
) -> str:
    dots = []
    for x, y, g in pts:
        px = x0 + plot_w * (x - xmin) / xr
        py = y0 - plot_h * (y - ymin) / yr
        c = pal.series[groups.index(g) % len(pal.series)]
        dots.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{_attr(c)}" fill-opacity="0.8"/>'
        )
    return "".join(dots)


def _scatter_legend_svg(groups: list[str], x0: float, pal: Palette) -> str:
    return "".join(
        f'<rect x="{x0 + 8 + i * 90:.0f}" y="{_PAD_T - 2:.0f}" '
        f'width="10" height="10" rx="2" '
        f'fill="{_attr(pal.series[i % len(pal.series)])}"/>'
        f'<text x="{x0 + 22 + i * 90:.0f}" y="{_PAD_T + 7:.0f}" '
        f'font-family="{_attr(pal.font_ui)}" font-size="9" fill="{_attr(pal.text)}">'
        f"{_esc(_ellipsize(str(g), 12))}</text>"
        for i, g in enumerate(groups)
    )


def _render_scatter(data: list[dict[str, Any]], title, x_label, y_label, pal: Palette) -> str:
    pts = _scatter_points(data)
    if not pts:
        return ""
    xmin, xr, ymin, yr, ymax = _scatter_bounds(pts)
    grid, x0, y0, plot_w, plot_h = _axes_grid(pal, ymax)
    groups = list(dict.fromkeys(g for _, _, g in pts))
    dots = _scatter_dots_svg(pts, groups, x0, y0, plot_w, plot_h, xmin, xr, ymin, yr, pal)
    legend = _scatter_legend_svg(groups, x0, pal)
    return _frame(title, x_label, y_label, pal, grid + dots + legend)


def render_chart_svg(spec: dict[str, Any], pal: Palette) -> str | None:
    """Render a chart spec to inline SVG, or None if it can't be charted (the caller
    then falls back to a data table). Never raises on bad data."""
    if not isinstance(spec, dict):
        return None
    ctype = str(spec.get("chart_type", "bar")).lower()
    data = spec.get("data")
    if not isinstance(data, list) or not data:
        return None
    title = str(spec.get("title", "") or "")
    x_label = str(spec.get("x_label", "") or "")
    y_label = str(spec.get("y_label", "") or "")
    try:
        if ctype == "scatter":
            svg = _render_scatter(data, title, x_label, y_label, pal)
            return svg or None
        pairs = _label_value_pairs(data)
        if not pairs:
            return None
        if ctype == "pie":
            return _render_pie(pairs, title, x_label, y_label, pal)
        if ctype == "line":
            return _render_line(pairs, title, x_label, y_label, pal)
        # default + "bar"
        return _render_bar(pairs, title, x_label, y_label, pal)
    except Exception:
        return None


def _chart_table_scatter_rows(
    data: list[Any], x_label: str, y_label: str
) -> tuple[list[str], list[list[str]]]:
    cols = ["Group", x_label or "X", y_label or "Y"]
    rows = [
        [_esc(d.get("group", "Default")), _esc(d.get("x")), _esc(d.get("y"))]
        for d in data
        if isinstance(d, dict)
    ]
    return cols, rows


def _chart_table_default_rows(
    data: list[Any], x_label: str, y_label: str
) -> tuple[list[str], list[list[str]]]:
    cols = [x_label or "Label", y_label or "Value"]
    rows = [
        [_esc(d.get("label", d.get("x", ""))), _esc(d.get("value", d.get("y", "")))]
        for d in data
        if isinstance(d, dict)
    ]
    return cols, rows


def _chart_table_head_body_html(cols: list[str], rows: list[list[str]]) -> tuple[str, str]:
    thead = "".join(f"<th>{c}</th>" for c in cols)
    tbody = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return thead, tbody


def render_chart_table(spec: dict[str, Any]) -> str:
    """Fallback: render the chart's data as a styled HTML table (mirrors
    ChartBlock.tsx's own TableView fallback). Always returns valid HTML."""
    data = spec.get("data") if isinstance(spec, dict) else None
    title = _esc(spec.get("title", "Chart")) if isinstance(spec, dict) else "Chart"
    if not isinstance(data, list) or not data:
        return f'<div class="chart-fallback"><em>{title}: (no chart data)</em></div>'
    ctype = str(spec.get("chart_type", "bar")).lower()
    x_label = _esc(spec.get("x_label", "Label"))
    y_label = _esc(spec.get("y_label", "Value"))
    if ctype == "scatter":
        cols, rows = _chart_table_scatter_rows(data, x_label, y_label)
    else:
        cols, rows = _chart_table_default_rows(data, x_label, y_label)
    thead, tbody = _chart_table_head_body_html(cols, rows)
    cap = f"<caption>{title}</caption>" if title else ""
    return (
        f'<table class="chart-table">{cap}<thead><tr>{thead}</tr></thead>'
        f"<tbody>{tbody}</tbody></table>"
    )
