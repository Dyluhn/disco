"""Top-level lane runners (G11 compile, governed A/E capture) — extracted from
:mod:`verify_export_track1_closeout`.

Note: ``_run_live_lane`` and ``_run_scanner_lane`` stay physically defined in
the parent module, not here — two release_remediation tests assert on the
parent file's own SOURCE TEXT (the ``--closeout-report-json`` / ``os.pathsep``
/ ``--src-prefix=a/`` ``--dst-prefix=b/`` wiring guards), pinning the governed
lane invocations to the entry-point file. See ``verify_export_track1_closeout.py``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod

from ._process import _run
from ._reports import (
    _CAPTURE_REPORT_FILE,
    _CLOSEOUT_REPORT_PLUGIN,
    LaneResult,
    _finalize_pytest_lane,
    _load_structured_report,
    _parse_junit,
)

# ---- G11 dedicated compile lane (plan §9.1) -----------------------------------


def _run_g11_compile_lane(repo: Path, evidence_dir: Path) -> LaneResult:
    """The dedicated G11 nullable-binding compile gate: ``npx tsc -p
    tsconfig.closeout-g11.json --noEmit`` (plan §9.1). A separate top-level lane (folded
    into all_lanes_green) so it never perturbs the frozen G13(b) _run_frontend_lane
    behavior. Green iff tsc exits 0. Firefox/vitest transpile without type-checking, so
    this dedicated compile is the only proof of the version_seq/tree_digest nullability
    contract."""
    frontend_dir = repo / "frontend"
    lane = LaneResult(name="g11-typecheck", status="ran")
    artifact = evidence_dir / "g11-typecheck.txt"
    if shutil.which("npx") is None:
        lane.green = False
        lane.detail = {
            "npx_available": False,
            "note": "npx unavailable; G11 compile lane cannot run",
        }
        artifact.write_text("npx unavailable; G11 compile lane did not run\n", encoding="utf-8")
        return lane
    proc = _run(
        ["npx", "tsc", "-p", "tsconfig.closeout-g11.json", "--noEmit"],
        cwd=frontend_dir,
    )
    lane.exit_code = proc.returncode
    lane.green = proc.returncode == 0
    lane.detail = {
        "npx_available": True,
        "command": manifest_mod.COMMAND_INVENTORY["g11_typecheck"],
        "exit_code": proc.returncode,
    }
    artifact.write_text(
        f"exit_code: {proc.returncode}\n\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n",
        encoding="utf-8",
    )
    return lane


# ---- governed A/E capture-regression lane (C9-03) ------------------------------


def _write_docker_versions(repo: Path, evidence_dir: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    if shutil.which("docker") is None:
        versions["docker"] = "unavailable"
        versions["compose"] = "unavailable"
    else:
        dproc = _run(["docker", "--version"], cwd=repo)
        versions["docker"] = (dproc.stdout or dproc.stderr).strip() or "unknown"
        cproc = _run(["docker", "compose", "version"], cwd=repo)
        versions["compose"] = (cproc.stdout or cproc.stderr).strip() or "unknown"
    lines = [f"docker: {versions['docker']}", f"compose: {versions['compose']}", ""]
    (evidence_dir / "docker-versions.txt").write_text("\n".join(lines), encoding="utf-8")
    return versions


def _run_capture_lane(
    repo: Path,
    evidence_dir: Path,
    frozen_capture_inventory: list[str],
    *,
    env: dict[str, str],
) -> LaneResult:
    """The governed A/E capture-regression lane (C9-03): run the seven capture tests
    under the live marker and require BOTH every governed-selected test to PASS AND the
    observed node inventory to EXACTLY equal the frozen ``governed_capture_inventory``
    (a missing / renamed / skipped / extra / unexecuted capture test is fatal). Same
    fail-closed structure as the other governed pytest lanes.

    ``env`` is the caller's constructed subprocess environment (the parent's
    ``_pytest_env()`` — ``scripts/`` prepended to ``PYTHONPATH`` so ``-p
    closeout_pytest_report`` resolves by bare module name); passed in rather than
    built here so this module never needs to import the parent."""
    junit_path = evidence_dir / "pytest-capture.xml"
    report_path = evidence_dir / _CAPTURE_REPORT_FILE
    junit_path.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)
    proc = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            manifest_mod.CAPTURE_TEST_FILE,
            "-o",
            "addopts=",
            "-m",
            manifest_mod.CLOSEOUT_MARKER_LIVE,
            "-ra",
            "-p",
            _CLOSEOUT_REPORT_PLUGIN,
            "--closeout-report-json",
            str(report_path),
            f"--junitxml={junit_path}",
        ],
        cwd=repo,
        env=env,
    )
    lane = LaneResult(
        name="live-capture",
        status="ran",
        exit_code=proc.returncode,
        junit=_parse_junit(junit_path),
        junit_path=str(junit_path),
        report_path=str(report_path),
    )
    observed = manifest_mod.collect_capture_node_ids(repo)
    expected = sorted(frozen_capture_inventory)
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    inventory_ok = bool(expected) and not missing and not extra
    inventory_reasons = (
        []
        if inventory_ok
        else [f"governed capture inventory mismatch: missing={missing} extra={extra}"]
    )
    summary = _finalize_pytest_lane(
        lane,
        report=_load_structured_report(report_path),
        extra_reasons=inventory_reasons,
    )
    lane.detail = {
        "inventory_ok": inventory_ok,
        "expected_count": len(expected),
        "observed_count": len(observed),
        "missing": missing,
        "extra": extra,
        "structured": summary,
    }
    return lane
