"""Frontend (vitest + typecheck + build) and browser (Firefox e2e) lanes —
extracted from :mod:`verify_export_track1_closeout`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod

from ._process import _run
from ._reports import LaneResult


def _parse_vitest(
    path: Path,
) -> tuple[dict[str, int], set[str], dict[str, set[str]], bool]:
    """Return ``(counts, test-file basenames, per-file executed leaf titles, parsed-ok)``.
    Vitest ``--reporter=json`` is Jest-shaped: numTotalTests / numFailedTests /
    numPendingTests / numTodoTests, and ``testResults[].name`` (absolute file path) with
    ``testResults[].assertionResults[]`` leaf nodes (each ``.title`` + ``.status``).

    The per-file title map records ONLY EXECUTED leaves — ``status`` in
    {"passed", "failed"}. A skipped / pending / todo leaf is EXCLUDED, so it reads
    downstream as an UNREPORTED (missing) title and cannot silently satisfy the frozen
    inventory. The leaf ``.title`` (NOT ``.fullName``) is used so it matches the
    source-parsed ``it(...)`` string in ``frontend_closeout_inventory``. A file that
    reports any leaf gets a set entry even when that set ends up empty (all leaves
    skipped), so a wholly-skipped file still surfaces as missing titles."""
    counts = {"total": -1, "passed": -1, "failed": -1, "pending": -1, "todo": -1}
    if not path.is_file():
        return counts, set(), {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return counts, set(), {}, False
    if not isinstance(data, dict):
        return counts, set(), {}, False
    counts = {
        "total": int(data.get("numTotalTests", 0)),
        "passed": int(data.get("numPassedTests", 0)),
        "failed": int(data.get("numFailedTests", 0)),
        "pending": int(data.get("numPendingTests", 0)),
        "todo": int(data.get("numTodoTests", 0)),
    }
    files: set[str] = set()
    titles: dict[str, set[str]] = {}
    results = data.get("testResults", [])
    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            basename = Path(name).name
            files.add(basename)
            executed = titles.setdefault(basename, set())
            assertions = entry.get("assertionResults", [])
            if isinstance(assertions, list):
                for assertion in assertions:
                    if not isinstance(assertion, dict):
                        continue
                    title = assertion.get("title")
                    status = assertion.get("status")
                    if isinstance(title, str) and status in {"passed", "failed"}:
                        executed.add(title)
    return counts, files, titles, True


def _diff_frontend_titles(
    frozen_inventory: dict[str, object], observed_titles: dict[str, set[str]]
) -> tuple[bool, dict[str, object]]:
    """Compare the FROZEN per-file ``it(...)`` title inventory against the per-file set of
    EXECUTED (passed|failed) leaf titles vitest reported. Returns ``(ok, detail)``:
    ``ok`` is False if ANY frozen file has a missing / renamed / skipped / unreported
    title, or reports an extra/unexpected executed title. This is the SINGLE SOURCE OF
    TRUTH the frontend lane uses, and is pure/importable so the committed regression can
    call it WITHOUT running vitest.

    File-SET drift (a frozen file absent from vitest, or a surprise extra file) is the
    caller's basename comparison; here titles are compared per frozen file (a missing
    file yields an empty observed set, so its titles read as missing too — a belt-and-
    braces overlap that only ever makes a lane HARDER to green). A frozen entry WITHOUT a
    ``tests`` list carries NO title constraint: the real manifest always emits one, so
    this only leaves a synthetic inventory that omits it (e.g. the G13 isolation mirror)
    unconstrained. Titles are plain human-readable ``it(...)`` strings, never credential
    material, so echoing mismatches is safe."""
    per_file: dict[str, dict[str, list[str]]] = {}
    ok = True
    for basename, meta in frozen_inventory.items():
        if not isinstance(meta, dict) or "tests" not in meta:
            continue
        raw_tests = meta.get("tests")
        if not isinstance(raw_tests, list):
            continue
        expected = {str(title) for title in raw_tests}
        observed = observed_titles.get(basename, set())
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        if missing or extra:
            ok = False
            per_file[basename] = {"missing": missing, "extra": extra}
    detail: dict[str, object] = {"title_mismatches": per_file}
    return ok, detail


def _frontend_vitest_ok(parsed_ok: bool, counts: dict[str, int]) -> bool:
    """Vitest itself is green: parsed, zero failed/pending/todo, at least one test."""
    return (
        parsed_ok
        and counts["failed"] == 0
        and counts["pending"] == 0
        and counts["todo"] == 0
        and counts["total"] > 0
    )


def _frontend_lane_green(
    *,
    vitest_ok: bool,
    file_set_ok: bool,
    titles_ok: bool,
    typecheck_exit: int,
    build_exit: int,
    browser_ok: bool,
) -> bool:
    """The frontend lane's overall verdict: every constituent check must be green — an
    un-run / failed / partial browser proof can never be a green, gating input to
    ``passed:true`` (G13(b) / plan §9.1-§9.3)."""
    return bool(
        vitest_ok
        and file_set_ok
        and titles_ok
        and typecheck_exit == 0
        and build_exit == 0
        and browser_ok
    )


def _run_frontend_lane(
    repo: Path, evidence_dir: Path, frozen_inventory: dict[str, object]
) -> LaneResult:
    frontend_dir = repo / "frontend"
    detail: dict[str, object] = {}
    lane = LaneResult(name="frontend", status="ran", detail=detail)

    if shutil.which("npx") is None or shutil.which("npm") is None:
        detail["npx_available"] = False
        detail["note"] = "node/npm/npx unavailable on this host; frontend lane cannot run"
        lane.green = False
        return lane
    detail["npx_available"] = True

    vitest_json = evidence_dir / "frontend-vitest.json"
    vproc = _run(
        [
            "npx",
            "vitest",
            "run",
            manifest_mod.FRONTEND_VITEST_DIR.split("/", 1)[1],
            "--reporter=json",
            f"--outputFile={vitest_json}",
        ],
        cwd=frontend_dir,
    )
    counts, discovered, observed_titles, parsed_ok = _parse_vitest(vitest_json)
    frozen_files = set(frozen_inventory.keys())
    missing_files = sorted(frozen_files - discovered)
    extra_files = sorted(discovered - frozen_files)
    file_set_ok = parsed_ok and not missing_files and not extra_files
    # Per-file EXECUTED-title comparison (single source of truth: _diff_frontend_titles).
    # Filename-set equality is NOT sufficient — a renamed/dropped/skipped/extra title
    # inside a frozen file must make the lane non-green even when the file set matches.
    titles_ok, titles_detail = _diff_frontend_titles(frozen_inventory, observed_titles)
    vitest_ok = _frontend_vitest_ok(parsed_ok, counts)
    detail["vitest"] = {
        "exit_code": vproc.returncode,
        "counts": counts,
        "parsed_ok": parsed_ok,
        "json": str(vitest_json),
        "file_set_ok": file_set_ok,
        "missing_files": missing_files,
        "extra_files": extra_files,
        "titles_ok": titles_ok,
        "title_mismatches": titles_detail["title_mismatches"],
    }

    tproc = _run(["npm", "run", "typecheck:build"], cwd=frontend_dir)
    detail["typecheck"] = {"exit_code": tproc.returncode}

    bproc = _run(["npx", "vite", "build"], cwd=frontend_dir)
    detail["build"] = {"exit_code": bproc.returncode}

    # BROWSER e2e GATE (G13(b) / G19 / plan §9.1-§9.3). The Firefox lane runs SEPARATELY
    # in _run_browser_e2e (called by main BEFORE this lane) and writes its structured JSON
    # report to evidence_dir/frontend-e2e.json. Here we only PARSE + GATE on that report:
    #   * absent report  -> status "not_executed_offline", browser_ok False (the frozen
    #     G13(b) call hits exactly this: a fresh empty evidence_dir has no report);
    #   * present report -> green browser only if it EXECUTED and expected>0, unexpected==0,
    #     flaky==0, skipped==0 AND the executed spec set equals the frozen e2e inventory
    #     (all frontend/e2e/export-track1-closeout/*.spec.ts).
    # browser_ok is folded into lane.green, so an un-run / failed / partial browser proof
    # can never be a green, gating input to passed:true.
    e2e_dir = repo / manifest_mod.FRONTEND_E2E_DIR
    expected_specs = {p.name for p in e2e_dir.glob("*.spec.ts")} if e2e_dir.exists() else set()
    browser_ok, browser_detail = _parse_playwright_e2e(
        evidence_dir / "frontend-e2e.json", expected_specs
    )
    detail["playwright_e2e"] = browser_detail

    lane.green = _frontend_lane_green(
        vitest_ok=vitest_ok,
        file_set_ok=file_set_ok,
        titles_ok=titles_ok,
        typecheck_exit=tproc.returncode,
        build_exit=bproc.returncode,
        browser_ok=browser_ok,
    )
    return lane


# ---- browser e2e (Firefox) lane (plan §9.1-§9.3) ------------------------------


def _playwright_spec_basenames(data: dict[str, object]) -> set[str]:
    """Every ``*.spec.ts`` file basename that appears in a Playwright JSON report's suite
    tree — the set of spec files that actually EXECUTED. Walks nested suites/specs so a
    dropped or extra spec file surfaces as a spec-set mismatch."""
    names: set[str] = set()

    def _walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        file_val = node.get("file")
        if isinstance(file_val, str) and file_val.endswith(".spec.ts"):
            names.add(Path(file_val).name)
        for key in ("suites", "specs"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    _walk(child)

    top = data.get("suites")
    if isinstance(top, list):
        for suite in top:
            _walk(suite)
    return names


def _parse_playwright_e2e(path: Path, expected_specs: set[str]) -> tuple[bool, dict[str, object]]:
    """Parse the structured Firefox report at ``path`` and decide ``browser_ok``. Pure +
    importable so the committed mutation tests drive it WITHOUT running a browser.

    Returns ``(browser_ok, detail)``. ``browser_ok`` is True ONLY when the run executed and
    Playwright's ``stats`` show ``expected > 0`` with ``unexpected == flaky == skipped == 0``
    AND the executed spec-file basenames equal ``expected_specs`` (the frozen e2e
    inventory). An ABSENT report is recorded as ``not_executed_offline`` (the frozen G13(b)
    state); an unparseable report or one _run_browser_e2e marked ``browser_run_failed`` /
    ``not_executed_offline`` is likewise not green. Fail-closed: missing counts default to a
    failing value."""
    if not path.is_file():
        return False, {
            "status": "not_executed_offline",
            "browser_ok": False,
            "reason": (
                "no frontend-e2e.json in the evidence dir — the Firefox e2e lane did not "
                "execute (the frozen candidate/needs_review Firefox proofs need a "
                "fixture-mode server + a browser); an un-run browser proof cannot be green"
            ),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, {"status": "unparseable", "browser_ok": False}
    if not isinstance(data, dict):
        return False, {"status": "unparseable", "browser_ok": False}
    marked = data.get("status")
    if marked in {"browser_run_failed", "not_executed_offline"}:
        return False, {
            "status": str(marked),
            "browser_ok": False,
            "exit_code": data.get("exit_code"),
        }
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}

    def _stat(key: str, absent_default: int) -> int:
        raw = stats.get(key, absent_default) if isinstance(stats, dict) else absent_default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return absent_default

    expected = _stat("expected", 0)
    unexpected = _stat("unexpected", 1)
    flaky = _stat("flaky", 1)
    skipped = _stat("skipped", 1)
    executed_specs = _playwright_spec_basenames(data)
    spec_set_ok = executed_specs == expected_specs and bool(expected_specs)
    browser_ok = bool(
        expected > 0 and unexpected == 0 and flaky == 0 and skipped == 0 and spec_set_ok
    )
    return browser_ok, {
        "status": "executed",
        "browser_ok": browser_ok,
        "counts": {
            "expected": expected,
            "unexpected": unexpected,
            "flaky": flaky,
            "skipped": skipped,
        },
        "executed_specs": sorted(executed_specs),
        "expected_specs": sorted(expected_specs),
        "spec_set_ok": spec_set_ok,
    }


def _run_browser_e2e(repo: Path, evidence_dir: Path) -> dict[str, object]:
    """RUN the frozen Firefox e2e proofs (``npx playwright test <FRONTEND_E2E_DIR>
    --reporter=json --project=firefox``, fixture mode) and write the structured report to
    ``evidence_dir/frontend-e2e.json`` (plan §9.1/§9.3). Kept SEPARATE from
    _run_frontend_lane (which only parses+gates) so the frozen G13(b) test — which calls
    _run_frontend_lane alone against a fresh empty evidence_dir — records
    ``not_executed_offline``. Called by main() BEFORE the frontend lane.

    On a host without ``npx`` no report is written (the frontend lane then records
    not_executed_offline). When Playwright emits no parseable JSON (e.g. the fixture server
    failed to boot), a truthful ``browser_run_failed`` record is written — NEVER a
    success-shaped report — so the frontend lane's browser gate is non-green."""
    frontend_dir = repo / "frontend"
    out_path = evidence_dir / "frontend-e2e.json"
    summary: dict[str, object] = {"command": manifest_mod.COMMAND_INVENTORY["browser_e2e"]}
    if shutil.which("npx") is None:
        summary["status"] = "not_executed_offline"
        summary["reason"] = "npx unavailable on this host; browser e2e lane cannot run"
        return summary
    e2e_rel = manifest_mod.FRONTEND_E2E_DIR.split("/", 1)[1]
    proc = _run(
        ["npx", "playwright", "test", e2e_rel, "--reporter=json", "--project=firefox"],
        cwd=frontend_dir,
    )
    summary["exit_code"] = proc.returncode
    report: object = None
    stdout = (proc.stdout or "").strip()
    if stdout:
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError:
            report = None
    if isinstance(report, dict):
        out_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        summary["status"] = "executed"
    else:
        # Truthful failure record: Playwright produced no parseable JSON report (missing
        # browser, fixture-server boot failure, crash). NEVER a success-shaped artifact.
        failure = {
            "status": "browser_run_failed",
            "exit_code": proc.returncode,
            "note": (
                "playwright did not emit a parseable JSON report on stdout; the Firefox "
                "e2e lane did not complete — recorded as failed, not faked"
            ),
        }
        out_path.write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        summary["status"] = "browser_run_failed"
    return summary
