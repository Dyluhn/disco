"""``main``'s phase functions — extracted from :mod:`verify_export_track1_closeout`
to keep ``main`` itself a thin, low-complexity orchestrator. ``main`` remains in
the parent module with the exact same name, argv contract, and exit codes.

Note: the nonlive/closeout pytest-lane phases and the live+capture phase are
NOT here — they call ``_run_pytest_lane`` / ``_run_live_lane``, which stay
physically defined in the parent module (see that module's docstring), and a
``closeout_verify_parts`` module may never import its parent. Those phases
live as parent-local helper functions instead. This module holds everything
else: early-exit evidence, frozen-inventory loading, the docker/live-evidence
finalize step (built entirely on ``_evidence`` functions), and the final
evidence-payload assembly + write.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod

from ._evidence import _finalize_live_evidence, _write_docker_host_artifacts
from ._reports import (
    _CAPTURE_REPORT_FILE,
    _CLOSEOUT_REPORT_FILE,
    _LIVE_REPORT_FILE,
    _NONLIVE_REPORT_FILE,
    LaneResult,
)
from ._verdict import _not_passed_reasons

# ---- early-exit evidence -------------------------------------------------------


def _write_dirty_checkout_evidence(
    evidence_dir: Path, *, candidate_sha: str, clean: bool, author: bool
) -> None:
    evidence = {
        "schema": "export-track1-closeout-evidence/v1",
        "passed": False,
        "refused": "dirty_checkout",
        "candidate_sha": candidate_sha,
        "clean_tree": clean,
        "author_mode": author,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    (evidence_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )


def _load_frozen_inventories(
    stored: dict[str, object] | None,
) -> tuple[list[str], list[str], dict[str, object]]:
    frozen_inventory: list[str] = []
    frozen_capture_inventory: list[str] = []
    frontend_inventory: dict[str, object] = {}
    if stored is not None:
        raw_py = stored.get("python_closeout_inventory", [])
        if isinstance(raw_py, list):
            frozen_inventory = [str(x) for x in raw_py]
        raw_cap = stored.get("governed_capture_inventory", [])
        if isinstance(raw_cap, list):
            frozen_capture_inventory = [str(x) for x in raw_cap]
        raw_fe = stored.get("frontend_closeout_inventory", {})
        if isinstance(raw_fe, dict):
            frontend_inventory = raw_fe
    return frozen_inventory, frozen_capture_inventory, frontend_inventory


# ---- docker-host artifacts + live-evidence finalize (plan §9.2 / C9-02) --------


def _finalize_docker_and_live_evidence(
    evidence_dir: Path,
    tool_versions: dict[str, str],
    live: LaneResult,
    live_run_id: str,
) -> tuple[dict[str, str], bool, list[str]]:
    docker_available = tool_versions.get("docker", "unavailable") != "unavailable"
    # G13(a) / §9.2: the Docker-host artifact status is tied to the ACTUAL live lifecycle,
    # so no success-sounding status is written without a successful live lane.
    docker_host_artifacts = _write_docker_host_artifacts(
        evidence_dir, docker_available, live_lifecycle_ok=live.green is True
    )
    # C9-02: when the live lane genuinely ran (docker present + lane green), aggregate and
    # VALIDATE its per-fixture evidence, overwriting the honest placeholder markers with
    # real content. The validation gates `passed`. When the lane did not run (no docker /
    # lane failed), the honest markers above stand and this gate is vacuously satisfied
    # (the failed lane already forces passed:false).
    if docker_available and live.green is True:
        live_evidence_ok, live_evidence_reasons = _finalize_live_evidence(evidence_dir, live_run_id)
    else:
        live_evidence_ok, live_evidence_reasons = True, []
    return docker_host_artifacts, live_evidence_ok, live_evidence_reasons


# ---- final evidence assembly -----------------------------------------------------


@dataclass
class _RunOutcome:
    """Everything the final evidence payload needs, gathered by ``main`` across the
    phases above. A plain data holder — no behavior, no hidden state; every field is
    exactly one thing ``main`` already computed."""

    args_author: bool
    candidate_sha: str
    clean: bool
    frozen_ok: bool
    frozen_note: str
    tool_versions: dict[str, str]
    closeout_inventory: dict[str, object]
    command_inventory_ok: bool
    command_inventory_detail: dict[str, object]
    observed_command_ids: set[str]
    browser_summary: dict[str, object]
    hygiene_ok: bool
    hygiene_violations: list[dict[str, object]]
    live_evidence_ok: bool
    live_evidence_reasons: list[str]
    lanes: list[LaneResult]
    nonlive: LaneResult
    closeout: LaneResult
    frontend: LaneResult
    live: LaneResult
    capture: LaneResult
    docker_host_artifacts: dict[str, str]
    passed: bool


def _build_evidence_payload(outcome: _RunOutcome) -> dict[str, object]:
    browser_written = outcome.browser_summary.get("status") in {"executed", "browser_run_failed"}
    return {
        "schema": "export-track1-closeout-evidence/v1",
        "passed": outcome.passed,
        "author_mode": outcome.args_author,
        "candidate_sha": outcome.candidate_sha,
        "clean_tree": outcome.clean,
        "baseline_sha": manifest_mod.BASELINE_SHA,
        "acceptance_tag": manifest_mod.ACCEPTANCE_TAG,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool_versions": outcome.tool_versions,
        "frozen_manifest": {"ok": outcome.frozen_ok, "note": outcome.frozen_note},
        "closeout_inventory": outcome.closeout_inventory,
        "command_inventory": {
            **outcome.command_inventory_detail,
            "observed": sorted(outcome.observed_command_ids),
            "required": sorted(manifest_mod.REQUIRED_COMMAND_IDS),
            "descriptors": manifest_mod.COMMAND_INVENTORY,
        },
        "browser_e2e": outcome.browser_summary,
        "evidence_hygiene": {"ok": outcome.hygiene_ok, "violations": outcome.hygiene_violations},
        "live_evidence": {
            "ok": outcome.live_evidence_ok,
            "reasons": outcome.live_evidence_reasons,
        },
        "lanes": [asdict(lane) for lane in outcome.lanes],
        "evidence_files": {
            "pytest-nonlive.xml": "written",
            "pytest-closeout.xml": "written",
            "pytest-live.xml": "written",
            _NONLIVE_REPORT_FILE: (
                "written" if Path(outcome.nonlive.report_path or "").is_file() else "absent"
            ),
            _CLOSEOUT_REPORT_FILE: (
                "written" if Path(outcome.closeout.report_path or "").is_file() else "absent"
            ),
            _LIVE_REPORT_FILE: (
                "written" if Path(outcome.live.report_path or "").is_file() else "absent"
            ),
            _CAPTURE_REPORT_FILE: (
                "written" if Path(outcome.capture.report_path or "").is_file() else "absent"
            ),
            "frontend-vitest.json": (
                "written" if outcome.frontend.detail.get("npx_available") else "absent"
            ),
            "frontend-e2e.json": "written" if browser_written else "absent",
            "g11-typecheck.txt": "written",
            "docker-versions.txt": "written",
            "anti-bypass-scan.json": "written",
            **outcome.docker_host_artifacts,
        },
        "not_passed_reasons": _not_passed_reasons(
            author=outcome.args_author,
            clean=outcome.clean,
            frozen_ok=outcome.frozen_ok,
            lanes=outcome.lanes,
            hygiene_ok=outcome.hygiene_ok,
            command_inventory_ok=outcome.command_inventory_ok,
            command_inventory_detail=outcome.command_inventory_detail,
            live_evidence_reasons=outcome.live_evidence_reasons,
        ),
    }


def _write_evidence_report(evidence_dir: Path, outcome: _RunOutcome) -> None:
    payload = _build_evidence_payload(outcome)
    (evidence_dir / "evidence.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    lane_summary = " ".join(f"{lane.name}={lane.green}" for lane in outcome.lanes)
    print(
        f"evidence written to {evidence_dir}: passed={outcome.passed} "
        f"(clean={outcome.clean} author={outcome.args_author} frozen_ok={outcome.frozen_ok}) "
        f"[{lane_summary}]"
    )
