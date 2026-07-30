"""HTML render helpers for the AppKit regression dashboard.

Extracted from ``appkit_dashboard.render_html`` (PKG-08-VERIFY) so the render
logic is cohesive pure helpers rather than one large function. The parent
module owns the public ``render_html`` entry point; this module owns the
summary-line, table, and per-row render helpers that produce the exact same
HTML bytes.
"""

from __future__ import annotations

import html
from typing import Any

__all__ = ["render_row", "render_rows_table", "render_summary_line"]


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _failing_check_chips(summary: Any) -> str:
    """Render the first-failing-check chips line (empty when no failing checks)."""
    if not summary.failing_checks:
        return ""
    chips = " ".join(
        f"<code>{_e(name)}&times;{count}</code>"
        for name, count in summary.failing_checks.items()
    )
    return f'<div class="meta">first-failing checks: {chips}</div>'


def _reliability_line(reliability: dict[str, Any], total: int) -> str:
    """Render the reliability summary line (empty when no nonzero totals)."""
    totals = reliability.get("totals") if isinstance(reliability, dict) else None
    if not isinstance(totals, dict) or not total:
        return ""
    nonzero = {
        str(k): v for k, v in totals.items() if isinstance(v, int) and v > 0 and k != "runs"
    }
    if not nonzero:
        return ""
    chips = " ".join(
        f"<code>{_e(name)}&times;{count}</code>" for name, count in sorted(nonzero.items())
    )
    stall_rate = reliability.get("stall_rate")
    rate_text = (
        f"{float(stall_rate):.1%}" if isinstance(stall_rate, int | float) else "0.0%"
    )
    return f'<div class="meta">reliability: stall rate {rate_text}; {chips}</div>'


def render_summary_line(summary: Any) -> str:
    """Render the dashboard summary line (title + pass/fail counts + chips)."""
    line = (
        f"<h1>AppKit Verified-App Regression Dashboard</h1>"
        f'<div class="meta">generated {_e(summary.generated_at)} &middot; '
        f'<span class="pass">{summary.passed} passed</span> / '
        f'<span class="fail">{summary.failed} failed</span> '
        f"of {summary.total} AppKit run(s)</div>"
    )
    line += _failing_check_chips(summary)
    line += _reliability_line(summary.reliability, summary.total)
    return line


def _verify_cell(row: Any) -> str:
    """Render the verify_appkit_app cell, surfacing false-green explicitly."""
    if row.appkit_verify_passed is True:
        cell = '<span class="pass">pass</span>'
    elif row.appkit_verify_passed is False:
        cell = '<span class="fail">fail</span>'
    else:
        cell = '<span class="muted">— (not run)</span>'
    # Make the false-green case unmistakable: result.passed but verify did not pass.
    if row.passed and not row.green:
        cell += ' <span class="fail">(false-green: result passed)</span>'
    return cell


def _first_failing_cell(row: Any) -> str:
    """Render the first-failing-check cell."""
    if not row.first_failing_check:
        return '<span class="muted">—</span>'
    ff = f"<code>{_e(row.first_failing_check)}</code>"
    if row.failure_fingerprint:
        ff += f' <span class="muted">{_e(row.failure_fingerprint)}</span>'
    return ff


def _problems_cell(row: Any) -> str:
    """Render the validator-problems cell."""
    if not row.validator_problems:
        return '<span class="muted">none</span>'
    return "<ul>" + "".join(f"<li>{_e(p)}</li>" for p in row.validator_problems) + "</ul>"


def _evidence_cell(row: Any) -> str:
    """Render the evidence cell (dossier link + screenshot thumbnails)."""
    bits = [f'<a href="{_e(row.dossier_path)}">dossier</a>']
    for shot in row.ui_screenshots:
        s = _e(shot)
        bits.append(f'<a href="{s}"><img class="shot" src="{s}"></a>')
    return "<br>".join(bits)


def render_row(row: Any) -> str:
    """Render one dashboard table row as an HTML ``<tr>`` string."""
    passed_cls = "pass" if row.green else "fail"
    app_cell = _e(row.app_name) if row.app_name else '<span class="muted">—</span>'
    return (
        "<tr>"
        f"<td>{_e(row.run_id)}</td>"
        f"<td>{_e(row.scenario_id)}</td>"
        f"<td>{_e(row.conversation_id)}</td>"
        f"<td>{app_cell}</td>"
        f"<td>{_e(row.terminal_status)}</td>"
        f'<td class="{passed_cls}">{"PASS" if row.green else "FAIL"}</td>'
        f"<td>{_verify_cell(row)}</td>"
        f"<td>{_first_failing_cell(row)}</td>"
        f"<td>{_problems_cell(row)}</td>"
        f"<td>{_evidence_cell(row)}</td>"
        "</tr>"
    )


def render_rows_table(summary: Any) -> str:
    """Render the full rows table (header + body rows + empty-case notice)."""
    parts = [
        "<table><thead><tr>"
        "<th>run</th><th>scenario</th><th>conversation</th><th>app</th>"
        "<th>terminal</th><th>passed</th><th>verify_appkit_app</th>"
        "<th>first failing check</th><th>problems</th><th>evidence</th>"
        "</tr></thead><tbody>"
    ]
    for row in summary.rows:
        parts.append(render_row(row))
    parts.append("</tbody></table>")
    if not summary.rows:
        parts.append('<p class="muted">No AppKit dossiers found.</p>')
    return "".join(parts)
