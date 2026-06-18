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
from typing import Any

# Series palette — byte-identical to ChartBlock.tsx COLORS so PDF matches screen.
_SERIES = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]

_W, _H = 640.0, 360.0
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 54.0, 24.0, 44.0, 64.0
_MAX_POINTS = 24  # keep a printable density; note truncation when exceeded


class Palette:
    """Colors pulled from the resolved Theme so charts honor light/dark mode."""

    def __init__(self, text: str, muted: str, faint: str, accent: str, grid: str, surface: str):
        self.text = text
        self.muted = muted
        self.faint = faint
        self.accent = accent
        self.grid = grid
        self.surface = surface


def palette_from_theme(theme: Any) -> Palette:
    return Palette(
        text=getattr(theme, "text", "#1a1813"),
        muted=getattr(theme, "text_muted", "#5a5853"),
        faint=getattr(theme, "text_faint", "#878682"),
        accent=getattr(theme, "accent", "#4077a3"),
        grid=getattr(theme, "hairline", "#dfdedb"),
        surface=getattr(theme, "surface_1", "#f7f7f4"),
    )


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _esc(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""))


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
    title_el = (
        f'<text x="{_W / 2:.0f}" y="22" text-anchor="middle" '
        f'font-family="sans-serif" font-size="15" font-weight="600" fill="{pal.text}">{_esc(title)}</text>'
        if title
        else ""
    )
    x_el = (
        f'<text x="{(_PAD_L + (_W - _PAD_R)) / 2:.0f}" y="{_H - 14:.0f}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="11" fill="{pal.muted}">{_esc(x_label)}</text>'
        if x_label
        else ""
    )
    y_el = (
        f'<text transform="translate(15,{(_PAD_T + (_H - _PAD_B)) / 2:.0f}) rotate(-90)" '
        f'text-anchor="middle" font-family="sans-serif" font-size="11" fill="{pal.muted}">{_esc(y_label)}</text>'
        if y_label
        else ""
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W:.0f} {_H:.0f}" '
        f'width="100%" role="img">'
        f'<rect x="0" y="0" width="{_W:.0f}" height="{_H:.0f}" fill="{pal.surface}" rx="6"/>'
        f"{title_el}{x_el}{y_el}{inner}</svg>"
    )


def _axes_grid(pal: Palette, vmax: float) -> tuple[str, float, float, float, float]:
    """Y gridlines + value ticks. Returns (svg, x0, y0, plot_w, plot_h)."""
    x0, y0 = _PAD_L, _H - _PAD_B
    plot_w, plot_h = _W - _PAD_L - _PAD_R, _H - _PAD_T - _PAD_B
    parts = [f'<line x1="{x0}" y1="{_PAD_T}" x2="{x0}" y2="{y0}" stroke="{pal.grid}" stroke-width="1"/>']
    for i in range(5):
        gy = y0 - plot_h * i / 4
        val = vmax * i / 4
        parts.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x0 + plot_w:.1f}" y2="{gy:.1f}" stroke="{pal.grid}" stroke-width="0.5"/>')
        parts.append(
            f'<text x="{x0 - 6:.1f}" y="{gy + 3:.1f}" text-anchor="end" font-family="sans-serif" '
            f'font-size="9" fill="{pal.faint}">{val:.0f}</text>'
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
        bars.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{_SERIES[0]}" rx="2"/>')
        bars.append(
            f'<text x="{bx + bw / 2:.1f}" y="{y0 + 12:.1f}" text-anchor="middle" font-family="sans-serif" '
            f'font-size="8" fill="{pal.muted}">{_esc(label[:14])}</text>'
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
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{_SERIES[0]}"/>' for x, y in pts)
    labels = "".join(
        f'<text x="{pts[i][0]:.1f}" y="{y0 + 12:.1f}" text-anchor="middle" font-family="sans-serif" '
        f'font-size="8" fill="{pal.muted}">{_esc(lbl[:12])}</text>'
        for i, (lbl, _) in enumerate(pairs)
    )
    line = f'<polyline points="{poly}" fill="none" stroke="{_SERIES[0]}" stroke-width="2"/>'
    return _frame(title, x_label, y_label, pal, grid + line + dots + labels)


def _render_pie(pairs: list[tuple[str, float]], title, x_label, y_label, pal: Palette) -> str:
    pairs = [(lbl, v) for lbl, v in pairs if v > 0][:len(_SERIES) * 2]
    total = sum(v for _, v in pairs)
    if total <= 0:
        return _frame(title, "", "", pal, "")
    cx, cy, r = 210.0, _PAD_T + 130, 110.0
    a0 = -math.pi / 2
    slices, legend = [], []
    for i, (label, v) in enumerate(pairs):
        frac = v / total
        a1 = a0 + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x2, y2 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        color = _SERIES[i % len(_SERIES)]
        slices.append(
            f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{color}"/>'
        )
        ly = _PAD_T + 18 + i * 20
        legend.append(
            f'<rect x="430" y="{ly - 9:.0f}" width="11" height="11" rx="2" fill="{color}"/>'
            f'<text x="447" y="{ly:.0f}" font-family="sans-serif" font-size="10" fill="{pal.text}">'
            f"{_esc(label[:20])} ({frac * 100:.0f}%)</text>"
        )
    return _frame(title, "", "", pal, "".join(slices) + "".join(legend))


def _render_scatter(data: list[dict[str, Any]], title, x_label, y_label, pal: Palette) -> str:
    pts = [
        (_num(d.get("x")), _num(d.get("y")), str(d.get("group", "Default")))
        for d in data
        if isinstance(d, dict)
    ]
    pts = [(x, y, g) for x, y, g in pts if x is not None and y is not None][: _MAX_POINTS * 4]
    if not pts:
        return ""
    xs = [x for x, _, _ in pts]
    ys = [y for _, y, _ in pts]
    xmin, xmax = min(xs), max(xs) or 1.0
    ymin, ymax = min(ys), max(ys) or 1.0
    xr = (xmax - xmin) or 1.0
    yr = (ymax - ymin) or 1.0
    grid, x0, y0, plot_w, plot_h = _axes_grid(pal, ymax)
    groups = list(dict.fromkeys(g for _, _, g in pts))
    dots = []
    for x, y, g in pts:
        px = x0 + plot_w * (x - xmin) / xr
        py = y0 - plot_h * (y - ymin) / yr
        c = _SERIES[groups.index(g) % len(_SERIES)]
        dots.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{c}" fill-opacity="0.8"/>')
    legend = "".join(
        f'<rect x="{x0 + 8 + i * 90:.0f}" y="{_PAD_T - 2:.0f}" width="10" height="10" rx="2" fill="{_SERIES[i % len(_SERIES)]}"/>'
        f'<text x="{x0 + 22 + i * 90:.0f}" y="{_PAD_T + 7:.0f}" font-family="sans-serif" font-size="9" fill="{pal.text}">{_esc(str(g)[:12])}</text>'
        for i, g in enumerate(groups)
    )
    return _frame(title, x_label, y_label, pal, grid + "".join(dots) + legend)


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
        cols = ["Group", x_label or "X", y_label or "Y"]
        rows = [[_esc(d.get("group", "Default")), _esc(d.get("x")), _esc(d.get("y"))] for d in data if isinstance(d, dict)]
    else:
        cols = [x_label or "Label", y_label or "Value"]
        rows = [
            [_esc(d.get("label", d.get("x", ""))), _esc(d.get("value", d.get("y", "")))]
            for d in data
            if isinstance(d, dict)
        ]
    thead = "".join(f"<th>{c}</th>" for c in cols)
    tbody = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    cap = f"<caption>{title}</caption>" if title else ""
    return f'<table class="chart-table">{cap}<thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>'
