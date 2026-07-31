"""AppKit verified-app regression dashboard (EPIC M — M3).

Aggregates the disco-verify evidence dossiers (written by ``runner.run_scenario``)
into a single static, reviewable regression view of the AppKit golden path — WITHOUT
adding any product/database state. It is pure file IO over the evidence folders:

  inputs   ``<verify_root>/<run-id>/result.json``   (scenario + VerifyResult, per run)
           ``<verify_root>/<run-id>/events.jsonl``  (the verify_appkit_app verdict)
           ``<e2e_root>/<run-id>/.../*.png``         (optional UI evidence screenshots)
  outputs  ``<out_dir>/summary.json``                (machine-readable roll-up)
           ``<out_dir>/index.html``                  (human-reviewable dashboard)

Only AppKit runs are included — a dossier counts as AppKit when its scenario set
``appkit_mode`` or ``expect_appkit_verify`` (so the same disco-verify dossier tree can
hold non-AppKit scenarios without polluting this view). For each AppKit run the
dashboard surfaces: scenario id, conversation id, terminal status, overall pass/fail,
the ``verify_appkit_app`` verdict, the FIRST failing EPIC G check (the regression's
fingerprint), the validator problems, the app name (best-effort from ``app_create``),
the dossier path, and any matching UI screenshots.

Run::

    python -m disco.agent_server.verify.appkit_dashboard \\
        --verify-root test-record/disco-verify \\
        --out test-record/appkit-regression-dashboard

CI vs on-demand: this generator is DETERMINISTIC and browser-free, so its parser/render
is unit-tested on the PR tier; the *real* dossiers it summarizes are produced by the
live VM-201 nightly tier (M1 headless + M2 Playwright), then this step rolls them up and
the workflow uploads the dashboard as an artifact.

Layering: agent-server → core is allowed; this module imports only stdlib + pydantic.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, computed_field

from .reliability import aggregate_reliability_metrics

# EPIC G structural checks, in canonical order (matches verify_appkit_app).
_APPKIT_CHECK_ORDER: tuple[str, ...] = (
    "design_lint_clean",
    "schema_sql_valid",
    "drizzle_schema_valid",
    "worker_contract",
    "lead_form_posts",
    "local_api_roundtrip",
    "route_coverage",
    "section_coverage",
)


class AppKitRunRow(BaseModel):
    """One AppKit run's roll-up — one row in the dashboard."""

    run_id: str
    scenario_id: str
    conversation_id: str
    terminal_status: str
    passed: bool
    # The verify_appkit_app verdict outcome: True/False, or None when the verifier
    # never ran (a missing verdict is itself a regression the row makes visible).
    appkit_verify_passed: bool | None = None
    # The FIRST failing EPIC G check (canonical order) — the regression's signature.
    first_failing_check: str | None = None
    failure_fingerprint: str | None = None
    # Best-effort app name from the app_create observation (None when unavailable).
    app_name: str | None = None
    validator_problems: list[str] = Field(default_factory=list)
    reliability_metrics: dict[str, Any] = Field(default_factory=dict)
    # Dossier folder, relative to the dashboard out dir when possible (else absolute).
    dossier_path: str = ""
    # UI evidence screenshots (relative paths), matched to this run's conversation id.
    ui_screenshots: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def green(self) -> bool:
        """Overall GREEN only when the run passed AND the AppKit verifier actually passed.
        A dossier with ``result.passed=True`` but a MISSING (``None``) or FAILING (``False``)
        ``verify_appkit_app`` verdict is NOT green — that silent false-green is exactly the
        failure this dashboard must catch (EPIC M P1). The dashboard pass/fail and the
        process exit code key on this, not on ``passed`` alone."""
        return self.passed and self.appkit_verify_passed is True


class DashboardSummary(BaseModel):
    """The machine-readable roll-up written as ``summary.json``."""

    generated_at: str
    total: int
    passed: int
    failed: int
    # first-failing-check → count, so a recurring regression is obvious at a glance.
    failing_checks: dict[str, int] = Field(default_factory=dict)
    reliability: dict[str, Any] = Field(default_factory=dict)
    rows: list[AppKitRunRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _iter_events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict):
                events.append(evt)
    except OSError:
        return []
    return events


def _final_appkit_verdict(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The LAST successful verify_appkit_app structured verdict (authoritative)."""
    verdict: dict[str, Any] | None = None
    for evt in events:
        if evt.get("kind") != "observation":
            continue
        tr = evt.get("tool_result") or {}
        if tr.get("tool_name") != "verify_appkit_app" or not tr.get("success"):
            continue
        structured = tr.get("structured")
        if isinstance(structured, dict):
            verdict = structured
    return verdict


def _first_failing_check(verdict: dict[str, Any]) -> str | None:
    """First failing check by canonical EPIC G order (stable regardless of list order)."""
    checks = verdict.get("checks")
    if not isinstance(checks, list):
        return None
    failed = {
        str(c.get("name")) for c in checks if isinstance(c, dict) and c.get("passed") is False
    }
    for name in _APPKIT_CHECK_ORDER:
        if name in failed:
            return name
    # A failing check whose name we don't recognize still counts.
    return next(iter(sorted(failed)), None) if failed else None


def _app_name_from_events(events: list[dict[str, Any]]) -> str | None:
    """Best-effort app name from the first successful app_create observation."""
    for evt in events:
        if evt.get("kind") != "observation":
            continue
        tr = evt.get("tool_result") or {}
        if tr.get("tool_name") != "app_create" or not tr.get("success"):
            continue
        structured = tr.get("structured") or {}
        if not isinstance(structured, dict):
            continue
        for key in ("app_name", "name", "title", "slug", "app_kind"):
            val = structured.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _is_appkit_dossier(result_doc: dict[str, Any]) -> bool:
    scenario = result_doc.get("scenario") or {}
    return bool(scenario.get("appkit_mode") or scenario.get("expect_appkit_verify"))


def _collect_ui_screenshots(cid: str, e2e_root: Path | None, out_dir: Path) -> list[str]:
    """UI evidence screenshots whose run folder references this conversation id.

    A folder matches when any ``*.json`` in it (manifest/result/conversation files)
    contains the cid. Returns screenshot paths relative to ``out_dir`` when possible.
    """
    if e2e_root is None or not e2e_root.exists() or not cid:
        return []
    shots: list[str] = []
    for run_dir in sorted(p for p in e2e_root.iterdir() if p.is_dir()):
        # Does this UI run reference our conversation id?
        referenced = False
        for meta in run_dir.rglob("*.json"):
            try:
                if cid in meta.read_text(encoding="utf-8"):
                    referenced = True
                    break
            except OSError:
                continue
        if not referenced:
            continue
        for png in sorted(run_dir.rglob("*.png")):
            try:
                rel = png.relative_to(out_dir)
            except ValueError:
                rel = png
            shots.append(str(rel))
    return shots


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


# Pure projection/render helpers extracted into the parts package (PKG-08-VERIFY).
# Imported after the models and low-level helpers above are defined so the parts
# modules can import them from this module without a circular-init problem.
from .appkit_dashboard_parts import (  # noqa: E402
    RowProjectionPorts,
    build_row_for_dossier,
    render_rows_table,
    render_summary_line,
)

_ROW_PROJECTION_PORTS = RowProjectionPorts(
    row_factory=AppKitRunRow,
    read_json=_read_json,
    is_appkit_dossier=_is_appkit_dossier,
    iter_events=_iter_events,
    final_appkit_verdict=_final_appkit_verdict,
    first_failing_check=_first_failing_check,
    app_name_from_events=_app_name_from_events,
    collect_ui_screenshots=_collect_ui_screenshots,
)


def collect_appkit_rows(
    verify_root: Path, *, out_dir: Path, e2e_root: Path | None = None
) -> list[AppKitRunRow]:
    """Scan ``verify_root`` for AppKit dossiers and build a sorted list of rows."""
    rows: list[AppKitRunRow] = []
    if not verify_root.exists():
        return rows
    for run_dir in sorted(p for p in verify_root.iterdir() if p.is_dir()):
        row = build_row_for_dossier(
            run_dir,
            out_dir=out_dir,
            e2e_root=e2e_root,
            ports=_ROW_PROJECTION_PORTS,
        )
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: r.run_id)
    return rows


def build_summary(rows: list[AppKitRunRow]) -> DashboardSummary:
    failing_checks: dict[str, int] = {}
    for r in rows:
        if r.first_failing_check:
            failing_checks[r.first_failing_check] = failing_checks.get(r.first_failing_check, 0) + 1
    # P1: a run is GREEN only when BOTH result.passed AND the AppKit verify passed. A
    # passed-but-verify-missing/failing dossier counts as FAILED (no silent false-green),
    # which also drives main()'s non-zero exit via summary.failed.
    return DashboardSummary(
        generated_at=datetime.now(UTC).isoformat(),
        total=len(rows),
        passed=sum(1 for r in rows if r.green),
        failed=sum(1 for r in rows if not r.green),
        failing_checks=dict(sorted(failing_checks.items())),
        reliability=aggregate_reliability_metrics([r.reliability_metrics for r in rows]),
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_html(summary: DashboardSummary) -> str:
    """A self-contained, dependency-free HTML dashboard (no JS, no external CSS)."""
    head = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>AppKit Verified-App Regression Dashboard</title><style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1a1a1a}"
        "h1{font-size:20px}.meta{color:#666;font-size:13px;margin-bottom:16px}"
        "table{border-collapse:collapse;width:100%;font-size:13px}"
        "th,td{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}"
        "th{background:#f5f5f5}.pass{color:#0a7d28;font-weight:600}"
        ".fail{color:#c0291c;font-weight:600}.muted{color:#888}"
        "code{background:#f3f3f3;padding:1px 4px;border-radius:3px}"
        "ul{margin:4px 0;padding-left:18px}.shot{max-width:160px;margin:2px;border:1px solid #ccc}"
        "</style></head><body>"
    )
    return (
        head
        + render_summary_line(summary)
        + render_rows_table(summary)
        + "</body></html>"
    )


def generate_dashboard(
    verify_root: Path, out_dir: Path, *, e2e_root: Path | None = None
) -> DashboardSummary:
    """Collect rows, write ``summary.json`` + ``index.html`` under ``out_dir``,
    and return the summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = collect_appkit_rows(verify_root, out_dir=out_dir, e2e_root=e2e_root)
    summary = build_summary(rows)
    (out_dir / "summary.json").write_text(
        json.dumps(summary.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    (out_dir / "index.html").write_text(render_html(summary), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the AppKit regression dashboard (M3).")
    ap.add_argument("--verify-root", default="test-record/disco-verify")
    ap.add_argument("--out", default="test-record/appkit-regression-dashboard")
    ap.add_argument(
        "--e2e-root",
        default=None,
        help="optional UI evidence root (frontend/test-record/e2e-full) for screenshot links",
    )
    args = ap.parse_args()
    summary = generate_dashboard(
        Path(args.verify_root),
        Path(args.out),
        e2e_root=Path(args.e2e_root) if args.e2e_root else None,
    )
    print(
        f"AppKit dashboard: {summary.passed}/{summary.total} passed "
        f"→ {Path(args.out) / 'index.html'}"
    )
    # Non-zero exit if any AppKit run failed — usable as a gate step.
    sys.exit(1 if summary.failed else 0)


if __name__ == "__main__":
    main()
