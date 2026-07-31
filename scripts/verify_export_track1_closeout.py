#!/usr/bin/env python3
"""verify_export_track1_closeout — the evidence-producing, non-bypass acceptance
verifier for the Export Track-1 Closeout (WO-C0, plan §1.2–§1.4 / §3 / §4).

It runs the campaign gates from a CLEAN checkout of one exact candidate SHA and
writes a machine-readable evidence directory OUTSIDE the checkout (CI uploads it as
an artifact on success AND failure). Exit code — never summary text — is truth.

Lanes (all wired; each is honestly recorded, none is ``not_wired_yet``):

  * ``python-nonlive`` — the full non-integration suite (plan §3.2, first command).
  * ``python-closeout`` — the focused closeout lane (plan §3.2, second command).
    JUnit is parsed and ANY nonzero failed/errors/skipped (xfail/xpass surface as
    those) is rejected; a fresh ``--collect-only`` node-ID inventory is compared with
    the FROZEN inventory read FROM THE MANIFEST JSON, so changing pytest discovery
    cannot hide a test.
  * ``frontend`` — from ``frontend/``: the frozen closeout vitest (``--reporter=json``),
    ``npm run typecheck:build``, and ``npx vite build`` (plan §3.3). Green iff vitest
    has zero failed/pending/todo AND both typecheck and build exit 0 AND the discovered
    test-file set equals the frozen ``frontend_closeout_inventory`` AND, per file, the
    EXECUTED (passed|failed) leaf-title set equals that file's frozen ``tests`` list
    (a dropped/renamed/skipped/unreported/extra title makes the lane non-green — filename
    parity alone is insufficient). The Playwright
    candidate/needs_review/not_web e2e proofs cannot run offline; they are frozen and
    RECORDED honestly, never faked, and never block ``passed`` here (see LIMITATION).
  * ``live-docker`` — the live clean-room lane (plan §3.4). On a host WITHOUT a real
    Docker engine the tests FAIL (never skip). Green iff exit 0 with zero
    failed/errors/skipped.
  * ``anti-bypass-scan`` — a real AST/lexical scan (plan §4.4 / §4.9) proving the
    frozen tests seam the system only at the allowlisted config/env seams and that
    production source carries no closeout test-switch. Green iff zero violations.

Non-bypass guarantees preserved: it refuses a dirty checkout UNLESS ``--author``;
``--author`` can NEVER emit ``passed: true``; the frozen-file SHA-256 manifest is a
byte-fresh tamper check; ``evidence.json`` is written outside the checkout.
``passed: true`` requires a clean, non-author run where the frozen manifest verified
and EVERY lane is green.

Usage:
    uv run python scripts/verify_export_track1_closeout.py --all \\
        --evidence-dir /path/outside/checkout
    uv run python scripts/verify_export_track1_closeout.py --author \\
        --evidence-dir /tmp/evidence            # dirty tree OK; passed always false
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod
from closeout_verify_parts._evidence import _EVIDENCE_FIXTURE_SUBDIR, _scan_evidence_hygiene
from closeout_verify_parts._evidence import (
    _EVIDENCE_HYGIENE_SENTINELS as _EVIDENCE_HYGIENE_SENTINELS,
)
from closeout_verify_parts._evidence import _HYGIENE_SCAN_FILES as _HYGIENE_SCAN_FILES
from closeout_verify_parts._evidence import (
    _write_docker_host_artifacts as _write_docker_host_artifacts,
)
from closeout_verify_parts._frontend import _diff_frontend_titles as _diff_frontend_titles
from closeout_verify_parts._frontend import _parse_playwright_e2e as _parse_playwright_e2e
from closeout_verify_parts._frontend import _parse_vitest as _parse_vitest
from closeout_verify_parts._frontend import _run_browser_e2e, _run_frontend_lane
from closeout_verify_parts._lanes import (
    _run_capture_lane,
    _run_g11_compile_lane,
    _write_docker_versions,
)
from closeout_verify_parts._manifest import _load_manifest, _verify_frozen_manifest
from closeout_verify_parts._orchestration import (
    _finalize_docker_and_live_evidence,
    _load_frozen_inventories,
    _RunOutcome,
    _write_dirty_checkout_evidence,
    _write_evidence_report,
)
from closeout_verify_parts._process import _git, _is_clean, _run, _tool_versions
from closeout_verify_parts._reports import (
    _CLOSEOUT_REPORT_FILE,
    _CLOSEOUT_REPORT_PLUGIN,
    _LIVE_REPORT_FILE,
    _NONLIVE_BASELINE_ALLOWLIST,
    _NONLIVE_REPORT_FILE,
    LaneResult,
    _finalize_pytest_lane,
    _load_structured_report,
    _parse_junit,
)
from closeout_verify_parts._reports import (
    _evaluate_structured_pytest_report as _evaluate_structured_pytest_report,
)
from closeout_verify_parts._reports import _junit_reasons as _junit_reasons
from closeout_verify_parts._scanners import (
    _check_command_inventory,
    _frozen_frontend_test_files,
    _frozen_python_test_files,
    _load_suppression_baseline,
    _observed_command_ids,
    _production_src_files,
    _scan_campaign_diff_suppressions,
    _scan_frontend_test,
    _scan_production_line,
    _scan_python_test,
)
from closeout_verify_parts._verdict import _build_arg_parser, _final_verdict
from closeout_verify_parts._verdict import _not_passed_reasons as _not_passed_reasons

# ---- private implementation re-imports -----------------------------------------
# This module is the state-free public entry point / export facade. Cohesive private
# implementation lives under closeout_verify_parts and is re-imported above so every
# public symbol keeps its import path. Three exceptions stay physically defined in
# THIS file rather than in a parts module — _pytest_env, _run_pytest_lane,
# _run_live_lane, and _run_scanner_lane — because committed release_remediation tests
# assert on this file's own SOURCE TEXT (the -p/--closeout-report-json plugin wiring,
# the os.pathsep PYTHONPATH extension, and the anti-bypass scanner's
# --src-prefix=a//--dst-prefix=b/ diff invocation) as wiring guards pinning the
# governed lane invocations to the entry-point file. A closeout_verify_parts module
# may never import this parent module.

# ``scripts/`` is directly this file's parent directory.
_SCRIPTS_DIR = Path(__file__).resolve().parent


def _pytest_env() -> dict[str, str]:
    """The child environment for a governed pytest lane: this process's env with ``scripts/``
    prepended to ``PYTHONPATH`` so ``-p closeout_pytest_report`` imports by bare module name
    inside the ``python -m pytest`` subprocess (whose sys.path carries cwd, not scripts/)."""
    env = dict(os.environ)
    scripts = str(_SCRIPTS_DIR)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = scripts + (os.pathsep + existing if existing else "")
    return env


# ---- pytest lanes (kept here — see module docstring for why) -------------------


def _run_pytest_lane(
    repo: Path,
    evidence_dir: Path,
    *,
    name: str,
    paths: tuple[str, ...],
    marker: str,
    junit_filename: str,
    report_filename: str,
) -> LaneResult:
    junit_path = evidence_dir / junit_filename
    report_path = evidence_dir / report_filename
    # STALE-EVIDENCE-DIR defense: unlink any prior report/junit at these FIXED paths BEFORE
    # the run. If a governed test hard-crashes the interpreter with exit code 0 (e.g.
    # os._exit(0)) so pytest_sessionfinish never overwrites them, an ABSENT report is what
    # remains — and the fail-closed path fires (absent report -> non-green) — instead of a
    # reused --evidence-dir's earlier GREEN report being read and the lane falsely greened.
    junit_path.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-o",
        "addopts=",
        "-m",
        marker,
        "-p",
        _CLOSEOUT_REPORT_PLUGIN,
        "--closeout-report-json",
        str(report_path),
        f"--junitxml={junit_path}",
    ]
    proc = _run(cmd, cwd=repo, env=_pytest_env())
    return LaneResult(
        name=name,
        status="ran",
        exit_code=proc.returncode,
        junit=_parse_junit(junit_path),
        junit_path=str(junit_path),
        report_path=str(report_path),
    )


def _run_nonlive_phase(repo: Path, evidence_dir: Path) -> LaneResult:
    nonlive = _run_pytest_lane(
        repo,
        evidence_dir,
        name="python-nonlive",
        paths=manifest_mod.NONLIVE_TEST_PATHS,
        marker=manifest_mod.NONLIVE_MARKER,
        junit_filename="pytest-nonlive.xml",
        report_filename=_NONLIVE_REPORT_FILE,
    )
    # A pytest exit code of 0 is NOT proof the lane is clean — pytest exits 0 with
    # SKIPPED/xfailed tests, and a non-strict xpass or a vanished test is invisible to the
    # exit code AND to JUnit. Gate on the structured per-test report (plan §9.3): the lane is
    # green ONLY when every governed-selected test truly PASSED.
    nonlive_summary = _finalize_pytest_lane(
        nonlive,
        report=_load_structured_report(Path(nonlive.report_path or "")),
        baseline_allowlist=_NONLIVE_BASELINE_ALLOWLIST,
    )
    nonlive.detail = {"structured": nonlive_summary}
    return nonlive


def _run_closeout_phase(
    repo: Path, evidence_dir: Path, frozen_inventory: list[str]
) -> tuple[LaneResult, dict[str, object]]:
    closeout = _run_pytest_lane(
        repo,
        evidence_dir,
        name="python-closeout",
        paths=manifest_mod.CLOSEOUT_TEST_DIRS,
        marker=manifest_mod.CLOSEOUT_MARKER_NONLIVE,
        junit_filename="pytest-closeout.xml",
        report_filename=_CLOSEOUT_REPORT_FILE,
    )
    observed = manifest_mod.collect_python_closeout_ids(repo)
    expected = sorted(frozen_inventory)
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    inventory_ok = bool(expected) and not missing and not extra
    inventory_note = f"missing={missing} extra={extra}" if not inventory_ok else "match"
    inventory_reasons = (
        [] if inventory_ok else [f"frozen node-ID inventory mismatch: {inventory_note}"]
    )
    closeout_summary = _finalize_pytest_lane(
        closeout,
        report=_load_structured_report(Path(closeout.report_path or "")),
        extra_reasons=inventory_reasons,
    )
    closeout.detail = {
        "inventory_ok": inventory_ok,
        "expected_count": len(expected),
        "observed_count": len(observed),
        "missing": missing,
        "extra": extra,
        "structured": closeout_summary,
    }
    closeout_inventory: dict[str, object] = {
        "ok": inventory_ok,
        "expected": expected,
        "observed": observed,
        "missing": missing,
        "extra": extra,
    }
    return closeout, closeout_inventory


# ---- live Docker lane (plan §3.4; kept here — see module docstring for why) ----


def _run_live_lane(repo: Path, evidence_dir: Path, run_id: str) -> LaneResult:
    docker_versions = _write_docker_versions(repo, evidence_dir)
    junit_path = evidence_dir / "pytest-live.xml"
    report_path = evidence_dir / _LIVE_REPORT_FILE
    # STALE-EVIDENCE-DIR defense (see _run_pytest_lane): unlink any prior report/junit at
    # these fixed paths BEFORE the run so an exit-0 crash that never writes leaves an ABSENT
    # report and the fail-closed path fires, rather than a reused dir's earlier green result.
    junit_path.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)
    # C9-02 P4-2: CLEAR the per-fixture evidence dir before the run and bind every record
    # to this run's nonce, so a stale family from a prior run in a reused evidence dir can
    # never satisfy completeness.
    fixtures_dir = evidence_dir / _EVIDENCE_FIXTURE_SUBDIR
    if fixtures_dir.exists():
        shutil.rmtree(fixtures_dir)
    # C9-02: the live lane emits GENUINE per-fixture evidence into the evidence dir
    # (the support module reads CLOSEOUT_EVIDENCE_DIR); the verifier aggregates and
    # validates it below before `passed:true`.
    live_env = _pytest_env()
    live_env["CLOSEOUT_EVIDENCE_DIR"] = str(evidence_dir)
    live_env["CLOSEOUT_EVIDENCE_RUN_ID"] = run_id
    proc = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            manifest_mod.LIVE_TEST_FILE,
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
        env=live_env,
    )
    lane = LaneResult(
        name="live-docker",
        status="ran",
        exit_code=proc.returncode,
        junit=_parse_junit(junit_path),
        junit_path=str(junit_path),
        report_path=str(report_path),
        detail={"docker_versions": docker_versions},
    )
    # Same fail-open class as the other pytest lanes: JUnit alone cannot see an xpass or a
    # within-selection disappearance, so gate on the structured report too (plan §9.3). On a
    # host WITHOUT Docker the live tests FAIL (never skip), so the lane stays non-green here.
    summary = _finalize_pytest_lane(lane, report=_load_structured_report(report_path))
    lane.detail["structured"] = summary
    return lane


# ---- anti-bypass scanner (plan §4.4 / §4.9; kept here — see module docstring) ---


def _run_scanner_lane(repo: Path, evidence_dir: Path) -> LaneResult:
    """The anti-bypass static scan (plan §4.4 / §4.9).

    §4.4 operational reading (reviewer-ratifiable interpretation, quoted verbatim by
    the manifest ``note`` and ``anti-bypass-scan.json``):

        A closeout acceptance test may seam the system ONLY at (1) the config loader —
        ``monkeypatch.setattr(cfg_store, 'load', ...)``, the same injection the settings
        PUT performs — and (2) the environment — ``monkeypatch.setenv``/``delenv`` (e.g.
        ``DISCO_DATA_DIR``). No other ``monkeypatch.setattr`` target is permitted; the
        code under test (detect / spec / emit / release-route / tool / store) is never
        patched. ``unittest.mock`` / ``MagicMock`` / ``Mock()`` / ``create_autospec`` /
        ``patch()`` are forbidden entirely. In the frontend, ``vi.fn()`` is allowed ONLY
        as a callback/prop value (e.g. ``onDownload={vi.fn()}``) or a locally-declared
        const passed as one; ``vi.mock`` / ``vi.spyOn`` / ``vi.stubGlobal`` /
        ``vi.stubEnv`` / ``jest.mock`` (module replacement) are forbidden. Production
        source may not reference closeout fixture names, the C8 secret/build sentinels,
        ``CLOSEOUT_SEED``, a force-verdict switch, or ``PYTEST_CURRENT_TEST`` outside the
        ratified pre-existing auth/workflow baseline.

    A lexical/AST scan cannot prove the absence of a semantically-equivalent indirect
    branch; the independent diff review (plan §4.9) is the required backstop. Python is
    AST-parsed (never regex-only); the frontend + production scans are lexical.
    """
    violations: list[dict[str, object]] = []

    py_files = _frozen_python_test_files(repo)
    for path in py_files:
        rel = path.relative_to(repo).as_posix()
        violations.extend(_scan_python_test(path.read_text(encoding="utf-8"), rel))

    fe_files = _frozen_frontend_test_files(repo)
    for path in fe_files:
        rel = path.relative_to(repo).as_posix()
        violations.extend(_scan_frontend_test(path.read_text(encoding="utf-8"), rel))

    prod_files = _production_src_files(repo)
    for path in prod_files:
        rel = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            violations.extend(_scan_production_line(rel, lineno, line))

    # G18 (plan §9.4 / criterion 4): scan the campaign diff (BASELINE_SHA..HEAD) for
    # NEWLY-ADDED suppression directives and reject any not in the owner-approved baseline.
    suppression_baseline, baseline_note = _load_suppression_baseline(repo)
    # Force a/ b/ prefixes so the diff carries them even if this host's git has
    # diff.noprefix=true (or a noprefix alias); a prefix-less diff would otherwise leave
    # the parser's cur_file None and silently no-op the whole scan (fail-open). The parser
    # is also hardened to anchor on the `diff --git` header (see _DIFF_GIT_HEADER_RE).
    diff_text = _run(
        [
            "git",
            "diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            f"{manifest_mod.BASELINE_SHA}..HEAD",
        ],
        cwd=repo,
    ).stdout
    diff_violations = _scan_campaign_diff_suppressions(diff_text, suppression_baseline)
    violations.extend(diff_violations)

    def _violation_sort_key(v: dict[str, object]) -> tuple[str, int, str]:
        line = v["line"]
        return (str(v["file"]), line if isinstance(line, int) else 0, str(v["rule"]))

    violations.sort(key=_violation_sort_key)
    clean = not violations
    report = {
        "status": "ran",
        "clean": clean,
        "violation_count": len(violations),
        "operational_reading": manifest_mod.OPERATIONAL_READING,
        "production_scan_note": (
            "PYTEST_CURRENT_TEST is allowlisted only at the ratified pre-existing "
            "auth/workflow sites; any other production reference — especially in the "
            "release/export subsystem — is a violation. A lexical/AST scan cannot prove "
            "the absence of a semantically-equivalent indirect branch; the independent "
            "diff review (plan §4.9) is the required backstop."
        ),
        "scanned": {
            "python_test_files": len(py_files),
            "frontend_test_files": len(fe_files),
            "production_src_files": len(prod_files),
        },
        "campaign_diff_suppressions": {
            "baseline_note": baseline_note,
            "baseline_rel": manifest_mod.SUPPRESSION_BASELINE_REL,
            "baseline_ratified_count": len(suppression_baseline),
            "diff_range": f"{manifest_mod.BASELINE_SHA}..HEAD",
            "new_suppression_violations": len(diff_violations),
            "scan_note": (
                "every ADDED line in the campaign diff (real code/config only; the two "
                "gate-definition scripts and the release_remediation meta-tests are "
                "exempt) is scanned for new suppressions; a hit not in the owner-approved "
                "baseline fails this lane (plan §9.4 / criterion 4)"
            ),
        },
        "violations": violations,
    }
    (evidence_dir / "anti-bypass-scan.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return LaneResult(
        name="anti-bypass-scan",
        status="ran",
        green=clean,
        detail={
            "violation_count": len(violations),
            "new_suppression_violations": len(diff_violations),
            "scanned": report["scanned"],
            "artifact": str(evidence_dir / "anti-bypass-scan.json"),
        },
    )


# ---- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser(__doc__).parse_args(argv)
    repo = args.repo_root.resolve()
    evidence_dir = args.evidence_dir.resolve()

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

    if not clean and not args.author:
        print(
            "ERROR: refusing to run on a dirty checkout (git status --porcelain is "
            "non-empty). Re-run from a clean checkout, or use --author to produce "
            "authoring evidence (which can never pass).",
            file=sys.stderr,
        )
        _write_dirty_checkout_evidence(
            evidence_dir, candidate_sha=candidate_sha, clean=clean, author=args.author
        )
        return 2

    stored, load_note = _load_manifest(repo)
    frozen_ok, frozen_note = _verify_frozen_manifest(repo, stored, load_note)
    frozen_inventory, frozen_capture_inventory, frontend_inventory = _load_frozen_inventories(
        stored
    )

    tool_versions = _tool_versions(repo)

    nonlive = _run_nonlive_phase(repo, evidence_dir)
    closeout, closeout_inventory = _run_closeout_phase(repo, evidence_dir, frozen_inventory)

    # BROWSER e2e (Firefox) runs BEFORE the frontend lane so the structured report exists
    # in evidence_dir for _run_frontend_lane to parse and gate on (plan §9.1-§9.3).
    browser_summary = _run_browser_e2e(repo, evidence_dir)
    frontend = _run_frontend_lane(repo, evidence_dir, frontend_inventory)
    # G11 dedicated compile lane (plan §9.1): a SEPARATE top-level lane so it never
    # perturbs the frozen G13(b) _run_frontend_lane behavior.
    g11 = _run_g11_compile_lane(repo, evidence_dir)

    live_run_id = secrets.token_hex(16)
    live = _run_live_lane(repo, evidence_dir, live_run_id)
    # C9-03: the governed A/E capture lane is a SEPARATE required lane with a frozen exact
    # node inventory — the seven capture regressions can no longer be omitted from an
    # authoritative run without making certification red.
    capture = _run_capture_lane(repo, evidence_dir, frozen_capture_inventory, env=_pytest_env())

    scanner = _run_scanner_lane(repo, evidence_dir)
    lanes: list[LaneResult] = [nonlive, closeout, frontend, g11, live, capture, scanner]

    docker_host_artifacts, live_evidence_ok, live_evidence_reasons = (
        _finalize_docker_and_live_evidence(evidence_dir, tool_versions, live, live_run_id)
    )

    # G19 / §9 criterion 3: the observed command inventory must EXACTLY equal the frozen
    # required inventory (omitted OR substitute/extra commands rejected).
    observed_command_ids = _observed_command_ids(repo)
    command_inventory_ok, command_inventory_detail = _check_command_inventory(
        observed_command_ids, manifest_mod.REQUIRED_COMMAND_IDS
    )

    # EVIDENCE HYGIENE (WO-B): after every text artifact is written and BEFORE `passed`
    # is computed, prove no registered planted-credential sentinel reached the evidence.
    # Fails CLOSED and folds into `passed` exactly like `frozen_ok`/`clean`, never
    # weakening an existing gate (it can only make passing harder).
    hygiene_ok, hygiene_violations = _scan_evidence_hygiene(evidence_dir)

    all_lanes_green = all(lane.green is True for lane in lanes)
    passed = _final_verdict(
        frozen_ok=frozen_ok,
        all_lanes_green=all_lanes_green,
        clean=clean,
        author=args.author,
        hygiene_ok=hygiene_ok,
        command_inventory_ok=command_inventory_ok,
        live_evidence_ok=live_evidence_ok,
    )

    outcome = _RunOutcome(
        args_author=args.author,
        candidate_sha=candidate_sha,
        clean=clean,
        frozen_ok=frozen_ok,
        frozen_note=frozen_note,
        tool_versions=tool_versions,
        closeout_inventory=closeout_inventory,
        command_inventory_ok=command_inventory_ok,
        command_inventory_detail=command_inventory_detail,
        observed_command_ids=observed_command_ids,
        browser_summary=browser_summary,
        hygiene_ok=hygiene_ok,
        hygiene_violations=hygiene_violations,
        live_evidence_ok=live_evidence_ok,
        live_evidence_reasons=live_evidence_reasons,
        lanes=lanes,
        nonlive=nonlive,
        closeout=closeout,
        frontend=frontend,
        live=live,
        capture=capture,
        docker_host_artifacts=docker_host_artifacts,
        passed=passed,
    )
    _write_evidence_report(evidence_dir, outcome)

    if args.author:
        return 0
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
