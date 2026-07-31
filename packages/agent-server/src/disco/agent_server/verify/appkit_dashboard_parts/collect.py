"""Per-dossier row projection for the AppKit regression dashboard.

Extracted from ``appkit_dashboard.collect_appkit_rows`` (PKG-08-VERIFY) so the
row-building logic is a cohesive pure helper rather than an inline loop body.
The parent module owns the public ``collect_appkit_rows`` entry point; this
module owns the per-dossier projection that builds one ``AppKitRunRow`` from a
single evidence dossier directory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..reliability import run_reliability_metrics

JsonObject = dict[str, Any]
EventList = list[JsonObject]


@dataclass(frozen=True)
class RowProjectionPorts:
    """Facade-owned compatibility seams required by the private projection."""

    row_factory: Callable[..., Any]
    read_json: Callable[[Path], JsonObject | None]
    is_appkit_dossier: Callable[[JsonObject], bool]
    iter_events: Callable[[Path], EventList]
    final_appkit_verdict: Callable[[EventList], JsonObject | None]
    first_failing_check: Callable[[JsonObject], str | None]
    app_name_from_events: Callable[[EventList], str | None]
    collect_ui_screenshots: Callable[[str, Path | None, Path], list[str]]


__all__ = ["RowProjectionPorts", "build_row_for_dossier"]


def _verdict_fields(
    verdict: JsonObject | None,
    first_failing_check: Callable[[JsonObject], str | None],
) -> tuple[bool | None, str | None, str | None]:
    """Extract (appkit_passed, first_failing_check, fingerprint) from a verdict.

    Returns ``(None, None, None)`` when the verifier never ran (verdict is
    None) — a missing verdict is itself a regression the row makes visible.
    """
    if verdict is None:
        return None, None, None
    appkit_passed = bool(verdict.get("passed"))
    if appkit_passed:
        return True, None, None
    first_fail = first_failing_check(verdict)
    fp = verdict.get("failure_fingerprint")
    fingerprint = str(fp) if fp else None
    return False, first_fail, fingerprint


def _reliability_metrics(
    result: JsonObject, events: EventList
) -> dict[str, Any]:
    """Use the result's reliability_metrics when present, else fold from events."""
    raw = result.get("reliability_metrics")
    if isinstance(raw, dict):
        return cast(dict[str, Any], raw)
    return dict(run_reliability_metrics(events))


def _dossier_rel_path(run_dir: Path, out_dir: Path) -> str:
    """Dossier path relative to out_dir when possible (else absolute)."""
    try:
        return str(run_dir.resolve().relative_to(out_dir.resolve()))
    except ValueError:
        return str(run_dir.resolve())


def build_row_for_dossier(
    run_dir: Path,
    *,
    out_dir: Path,
    ports: RowProjectionPorts,
    e2e_root: Path | None = None,
) -> Any | None:
    """Build one dashboard row from a single evidence dossier directory.

    Returns ``None`` when the dossier is absent or not an AppKit scenario, so
    the caller can ``continue`` without building a row. This is the per-dossier
    projection extracted from ``collect_appkit_rows`` — the caller handles
    directory iteration and final sort.
    """
    result_doc = ports.read_json(run_dir / "result.json")
    if result_doc is None or not ports.is_appkit_dossier(result_doc):
        return None
    scenario = result_doc.get("scenario") or {}
    result = result_doc.get("result") or {}
    cid = str(result_doc.get("conversation_id", ""))
    events = ports.iter_events(run_dir / "events.jsonl")
    verdict = ports.final_appkit_verdict(events)
    appkit_passed, first_fail, fingerprint = _verdict_fields(
        verdict, ports.first_failing_check
    )
    return ports.row_factory(
        run_id=str(result_doc.get("run_id", run_dir.name)),
        scenario_id=str(result.get("scenario_id", scenario.get("id", "?"))),
        conversation_id=cid,
        terminal_status=str(result.get("terminal_status", "UNKNOWN")),
        passed=bool(result.get("passed", False)),
        appkit_verify_passed=appkit_passed,
        first_failing_check=first_fail,
        failure_fingerprint=fingerprint,
        app_name=ports.app_name_from_events(events),
        validator_problems=[str(p) for p in (result.get("validator_problems") or [])],
        reliability_metrics=_reliability_metrics(result, events),
        dossier_path=_dossier_rel_path(run_dir, out_dir),
        ui_screenshots=ports.collect_ui_screenshots(cid, e2e_root, out_dir),
    )
