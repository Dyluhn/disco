#!/usr/bin/env python3
"""verify_export_track1_closeout — the evidence-producing, non-bypass acceptance
verifier for the Export Track-1 Closeout (WO-C0, plan §1.2–§1.4).

It runs the campaign gates from a CLEAN checkout of one exact candidate SHA and
writes a machine-readable evidence directory OUTSIDE the checkout (CI uploads it as
an artifact on success AND failure). Exit code — never summary text — is truth.

What it enforces (this tranche):
  * Refuses to run on a dirty checkout UNLESS `--author` (acceptance-authoring)
    is given; `--author` can NEVER emit `passed: true`.
  * Runs the non-live lane and the focused closeout lane with `-o addopts=''` and
    `--junitxml`, parses the JUnit XML, and REJECTS any nonzero
    failed/errors/skipped count in the CLOSEOUT lane (xfail/xpass surface as
    skipped/failure in JUnit and are likewise rejected). Exit codes are captured.
  * Compares a `--collect-only` node-ID inventory of the closeout lane against the
    FROZEN inventory recorded in the acceptance manifest, so changing pytest
    discovery/config cannot hide a test.
  * Verifies the frozen-file SHA-256 manifest is byte-fresh (tamper check).
  * Writes `evidence.json` + every §1.4 evidence file. `passed: true` is emitted
    ONLY when every REQUIRED lane validated on a clean, non-author run.

HONEST SCAFFOLD: the live Docker lane and the full frontend (vitest/build/
Playwright) lane are not wired into this verifier yet — they are recorded with
`status: "not_wired_yet"` and can NEVER set `passed: true`. The live lane is a
final closeout requirement (plan §1.3); it is not "deferred", it is not-yet-wired
in THIS foundation tranche and is honestly reported as such.

Usage:
    uv run python scripts/verify_export_track1_closeout.py --all \\
        --evidence-dir /path/outside/checkout
    uv run python scripts/verify_export_track1_closeout.py --author \\
        --evidence-dir /tmp/evidence            # dirty tree OK; passed always false
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Reuse the single source of truth for the frozen-file set + inventory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_closeout_acceptance_manifest as manifest_mod  # noqa: E402

# ---- lane definitions (plan §3.2) ---------------------------------------------

_NONLIVE_PATHS = (
    "packages/core/tests",
    "packages/tools/tests",
    "packages/agent-server/tests",
)
_CLOSEOUT_PATHS = (
    "packages/core/tests/export_track1_closeout",
    "packages/tools/tests/export_track1_closeout",
    "packages/agent-server/tests/export_track1_closeout",
)
_CLOSEOUT_MARKER = "export_track1_closeout and not integration"

# Evidence files the manifest promises (plan §1.4). The XML/JSON lanes that are
# actually run are written by their lane; the rest are honest placeholders.
_PLACEHOLDER_EVIDENCE = (
    "pytest-live.xml",
    "frontend-vitest.json",
    "bundle-digests.json",
    "docker-versions.txt",
    "docker-inspect-sanitized.json",
    "compose-ps.json",
    "cleanup.txt",
)


@dataclass
class LaneResult:
    name: str
    status: str  # "ran" | "not_wired_yet" | "skipped"
    exit_code: int | None = None
    junit: dict[str, int] | None = None
    junit_path: str | None = None
    rejected: bool | None = None  # closeout lane: True if any disallowed count > 0
    rejection_reasons: list[str] = field(default_factory=list)


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output. Never raises on nonzero — the CALLER reads
    the returncode (plan §1.2: exit code is captured, not inferred)."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )


def _git(repo: Path, *args: str) -> str:
    proc = _run(["git", *args], cwd=repo)
    return proc.stdout.strip()


def _is_clean(repo: Path) -> bool:
    return _git(repo, "status", "--porcelain") == ""


def _tool_versions(repo: Path) -> dict[str, str]:
    versions: dict[str, str] = {}

    def _probe(key: str, cmd: list[str]) -> None:
        exe = cmd[0]
        if shutil.which(exe) is None:
            versions[key] = "unavailable"
            return
        proc = _run(cmd, cwd=repo)
        line = (proc.stdout or proc.stderr).strip().splitlines()
        versions[key] = line[0] if line else "unknown"

    _probe("python", [sys.executable, "--version"])
    _probe("pytest", [sys.executable, "-m", "pytest", "--version"])
    _probe("git", ["git", "--version"])
    _probe("node", ["node", "--version"])
    _probe("npm", ["npm", "--version"])
    _probe("docker", ["docker", "--version"])
    return versions


def _parse_junit(path: Path) -> dict[str, int]:
    """Read aggregate counts from a JUnit XML. Sums across testsuites so a
    multi-suite report is not under-counted."""
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if not path.is_file():
        return counts
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, "0") or "0")
    return counts


def _run_pytest_lane(
    repo: Path,
    evidence_dir: Path,
    *,
    name: str,
    paths: tuple[str, ...],
    marker: str,
    junit_filename: str,
) -> LaneResult:
    junit_path = evidence_dir / junit_filename
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-o",
        "addopts=",
        "-m",
        marker,
        f"--junitxml={junit_path}",
    ]
    proc = _run(cmd, cwd=repo)
    junit = _parse_junit(junit_path)
    return LaneResult(
        name=name,
        status="ran",
        exit_code=proc.returncode,
        junit=junit,
        junit_path=str(junit_path),
    )


def _collect_only_inventory(repo: Path, paths: tuple[str, ...], marker: str) -> list[str]:
    """Node IDs the closeout lane discovers, via `--collect-only -q`."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-o",
        "addopts=",
        "-m",
        marker,
        "--collect-only",
        "-q",
    ]
    proc = _run(cmd, cwd=repo)
    ids: list[str] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if "::" in line and not line.startswith(("=", "warning", "no tests")):
            ids.append(line)
    return ids


def _evaluate_closeout(lane: LaneResult, inventory_ok: bool, inventory_note: str) -> None:
    """Apply the closeout-lane rejection rules (plan §1.2/§3.2) in place."""
    reasons: list[str] = []
    if lane.exit_code != 0:
        reasons.append(f"closeout pytest exit code {lane.exit_code} (expected 0)")
    junit = lane.junit or {}
    for bad in ("failures", "errors", "skipped"):
        if junit.get(bad, 0) > 0:
            reasons.append(f"closeout lane has {junit[bad]} {bad} (must be 0)")
    if junit.get("tests", 0) == 0:
        reasons.append("closeout lane collected zero tests")
    if not inventory_ok:
        reasons.append(f"frozen node-ID inventory mismatch: {inventory_note}")
    lane.rejected = bool(reasons)
    lane.rejection_reasons = reasons


def _verify_frozen_manifest(repo: Path) -> tuple[bool, str]:
    """Recompute the frozen-file hashes and compare with the on-disk manifest."""
    manifest_path = repo / manifest_mod.MANIFEST_REL
    if not manifest_path.is_file():
        return False, "acceptance manifest is absent (run gen_closeout_acceptance_manifest.py)"
    try:
        stored = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        return False, f"acceptance manifest is not valid JSON: {exc}"
    stored_files = stored.get("files", {})
    fresh_files, _missing = manifest_mod._iter_frozen_files(repo)
    if stored_files != fresh_files:
        changed = sorted(
            set(stored_files) ^ set(fresh_files)
            | {k for k in set(stored_files) & set(fresh_files) if stored_files[k] != fresh_files[k]}
        )
        return False, f"frozen file hash drift on: {changed[:8]}"
    return True, "frozen files match the manifest"


def _write_placeholders(evidence_dir: Path) -> dict[str, str]:
    note = (
        "not_wired_yet — the live Docker lane and full frontend lane are not wired "
        "into the verifier in the WO-C0 foundation tranche. This file is a placeholder "
        "so the §1.4 evidence set is complete; it can never set evidence.json.passed=true."
    )
    written: dict[str, str] = {}
    for name in _PLACEHOLDER_EVIDENCE:
        path = evidence_dir / name
        if name.endswith(".json"):
            path.write_text(json.dumps({"status": "not_wired_yet", "note": note}, indent=2) + "\n")
        else:
            path.write_text(note + "\n")
        written[name] = "not_wired_yet"
    return written


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run every wired lane")
    parser.add_argument(
        "--author",
        action="store_true",
        help="acceptance-authoring mode: allow a dirty tree; NEVER emits passed:true",
    )
    parser.add_argument(
        "--evidence-dir",
        required=True,
        type=Path,
        help="evidence output directory (MUST be OUTSIDE the checkout)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=manifest_mod.repo_root(),
        help="the checkout root (defaults to this script's repository)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    repo = args.repo_root.resolve()
    evidence_dir = args.evidence_dir.resolve()

    # Evidence must live OUTSIDE the checkout (so a run cannot dirty the tree it
    # is attesting, and CI uploads a clean artifact).
    if repo == evidence_dir or repo in evidence_dir.parents:
        print(
            f"ERROR: --evidence-dir {evidence_dir} is inside the checkout {repo}; "
            "it must be outside (plan §1.4).",
            file=sys.stderr,
        )
        return 2
    evidence_dir.mkdir(parents=True, exist_ok=True)

    clean = _is_clean(repo)
    candidate_sha = _git(repo, "rev-parse", "HEAD")

    # Non-author runs REFUSE a dirty checkout (hard error, no lanes).
    if not clean and not args.author:
        print(
            "ERROR: refusing to run on a dirty checkout (git status --porcelain is "
            "non-empty). Re-run from a clean checkout, or use --author to produce "
            "authoring evidence (which can never pass).",
            file=sys.stderr,
        )
        evidence = {
            "schema": "export-track1-closeout-evidence/v1",
            "passed": False,
            "refused": "dirty_checkout",
            "candidate_sha": candidate_sha,
            "clean_tree": clean,
            "author_mode": args.author,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        (evidence_dir / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        return 2

    frozen_ok, frozen_note = _verify_frozen_manifest(repo)
    tool_versions = _tool_versions(repo)

    lanes: list[LaneResult] = []

    # --- non-live lane (plan §3.2, first command) ------------------------------
    nonlive = _run_pytest_lane(
        repo,
        evidence_dir,
        name="python-nonlive",
        paths=_NONLIVE_PATHS,
        marker="not integration",
        junit_filename="pytest-nonlive.xml",
    )
    lanes.append(nonlive)

    # --- focused closeout lane (plan §3.2, second command) ---------------------
    closeout = _run_pytest_lane(
        repo,
        evidence_dir,
        name="python-closeout",
        paths=_CLOSEOUT_PATHS,
        marker=_CLOSEOUT_MARKER,
        junit_filename="pytest-closeout.xml",
    )
    observed = _collect_only_inventory(repo, _CLOSEOUT_PATHS, _CLOSEOUT_MARKER)
    expected = list(manifest_mod.PYTHON_CLOSEOUT_INVENTORY)
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    inventory_ok = not missing and not extra
    inventory_note = f"missing={missing} extra={extra}" if not inventory_ok else "match"
    _evaluate_closeout(closeout, inventory_ok, inventory_note)
    lanes.append(closeout)

    # --- not-yet-wired lanes (honest) ------------------------------------------
    live = LaneResult(name="live-docker", status="not_wired_yet")
    frontend = LaneResult(name="frontend", status="not_wired_yet")
    lanes.extend([live, frontend])

    placeholders = _write_placeholders(evidence_dir)

    # `passed` is true ONLY on a clean, non-author run where every REQUIRED lane
    # validated. The live + frontend lanes are not_wired_yet, so `passed` can never
    # be true in this tranche — reported honestly, never masked.
    all_wired_required_ok = (
        frozen_ok
        and nonlive.exit_code == 0
        and closeout.rejected is False
        and live.status == "ran"
        and frontend.status == "ran"
    )
    passed = bool(all_wired_required_ok and clean and not args.author)

    evidence = {
        "schema": "export-track1-closeout-evidence/v1",
        "passed": passed,
        "author_mode": args.author,
        "candidate_sha": candidate_sha,
        "clean_tree": clean,
        "baseline_sha": manifest_mod.BASELINE_SHA,
        "acceptance_tag": manifest_mod.ACCEPTANCE_TAG,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool_versions": tool_versions,
        "frozen_manifest": {"ok": frozen_ok, "note": frozen_note},
        "closeout_inventory": {
            "ok": inventory_ok,
            "expected": expected,
            "observed": observed,
            "missing": missing,
            "extra": extra,
        },
        "lanes": [asdict(lane) for lane in lanes],
        "evidence_files": {
            "pytest-nonlive.xml": "written",
            "pytest-closeout.xml": "written",
            **placeholders,
        },
        "not_passed_reasons": _not_passed_reasons(
            args.author, clean, frozen_ok, nonlive, closeout, live, frontend
        ),
    }
    (evidence_dir / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")

    print(
        f"evidence written to {evidence_dir}: passed={passed} "
        f"(clean={clean} author={args.author} frozen_ok={frozen_ok} "
        f"nonlive_exit={nonlive.exit_code} closeout_rejected={closeout.rejected})"
    )

    # Author mode succeeds at AUTHORING (exit 0) even though passed is false.
    # A non-author --all fails (exit 1) whenever the harness is not fully green.
    if args.author:
        return 0
    return 0 if passed else 1


def _not_passed_reasons(
    author: bool,
    clean: bool,
    frozen_ok: bool,
    nonlive: LaneResult,
    closeout: LaneResult,
    live: LaneResult,
    frontend: LaneResult,
) -> list[str]:
    reasons: list[str] = []
    if author:
        reasons.append("author_mode forces passed:false")
    if not clean:
        reasons.append("checkout is dirty")
    if not frozen_ok:
        reasons.append("frozen manifest not verified")
    if nonlive.exit_code != 0:
        reasons.append(f"non-live lane exit {nonlive.exit_code}")
    if closeout.rejected:
        reasons.extend(closeout.rejection_reasons)
    if live.status != "ran":
        reasons.append(f"live lane {live.status}")
    if frontend.status != "ran":
        reasons.append(f"frontend lane {frontend.status}")
    return reasons


if __name__ == "__main__":
    raise SystemExit(main())
