"""Model-free suite result and artifact adjudication."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from harness.reliability.state import FAIL, INFRA, INVALID, PASS


def _pytest_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "pytest did not produce its required JUnit evidence"
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = sum(
        1 for case in cases if case.find("failure") is not None or case.find("error") is not None
    )
    skipped = sum(1 for case in cases if case.find("skipped") is not None)
    passed = len(cases) - failed - skipped
    if failed:
        return FAIL, 0, f"{failed} pytest case(s) failed"
    if exit_code != 0:
        return INFRA, 0, f"pytest exited {exit_code} without a classified test failure"
    if skipped:
        return INVALID, 0, f"{skipped} pytest case(s) skipped; skipped evidence never counts"
    if not cases:
        return INVALID, 0, "pytest collected no cases"
    return PASS, units, f"{passed} pytest case(s) passed"


def _playwright_tests(report: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        tests = node.get("tests")
        if isinstance(tests, list):
            found.extend(test for test in tests if isinstance(test, dict))
        for key in ("suites", "specs"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    visit(child)

    visit(report)
    return found


def _playwright_test_outcome(test: dict[str, Any]) -> str:
    results = test.get("results") or []
    statuses = [result.get("status") for result in results if isinstance(result, dict)]
    expected = test.get("expectedStatus", "passed")
    if expected == "skipped" or not statuses or statuses[-1] == "skipped":
        return "skipped"
    if expected != "passed" or any(status != "passed" for status in statuses):
        return "failed"
    return "passed"


def _playwright_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "Playwright did not produce its required JSON evidence"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"Playwright JSON evidence is unreadable: {exc}"
    tests = _playwright_tests(report)
    outcomes = Counter(_playwright_test_outcome(test) for test in tests)
    passed, skipped, failed = (
        outcomes["passed"],
        outcomes["skipped"],
        outcomes["failed"],
    )
    top_errors = report.get("errors") if isinstance(report, dict) else None
    if failed or top_errors:
        return FAIL, 0, f"{failed} failed test(s), {len(top_errors or [])} top-level error(s)"
    if exit_code != 0:
        return INFRA, 0, f"Playwright exited {exit_code} without classified test failures"
    if skipped:
        return INVALID, 0, f"{skipped} Playwright test(s) skipped; skipped evidence never counts"
    if passed < units:
        return INVALID, 0, f"only {passed}/{units} required Playwright trials passed"
    return PASS, units, f"{passed} Playwright trial(s) passed"


def _vitest_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "Vitest did not produce its required JSON evidence"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"Vitest JSON evidence is unreadable: {exc}"
    failed = int(report.get("numFailedTests") or 0)
    pending = int(report.get("numPendingTests") or 0)
    passed = int(report.get("numPassedTests") or 0)
    total = int(report.get("numTotalTests") or (failed + pending + passed))
    if failed:
        return FAIL, 0, f"{failed} Vitest test(s) failed"
    if exit_code != 0:
        return INFRA, 0, f"Vitest exited {exit_code} without classified test failures"
    if pending:
        return INVALID, 0, f"{pending} Vitest test(s) skipped; skipped evidence never counts"
    if total <= 0 or passed <= 0:
        return INVALID, 0, "Vitest produced no passing test evidence"
    return PASS, units, f"{passed} Vitest test(s) passed"


_PROMOTION_SUMMARY_NAME = "batch-summary.json"


def _build_soak_summary_candidates(out: Path, root: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(out.rglob(_PROMOTION_SUMMARY_NAME)):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if root == resolved or root in resolved.parents:
            candidates.append(resolved)
    return candidates


def _matching_batch_summaries(candidates: list[Path], batch_id: str) -> list[Path]:
    matched: list[Path] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, dict) and payload.get("batch_id") == batch_id:
            matched.append(path)
    return matched


def _select_expected_batch_summary(
    candidates: list[Path],
    expected_batch_id: str,
) -> tuple[Path | None, str]:
    matched = _matching_batch_summaries(candidates, expected_batch_id)
    if not matched:
        return None, (
            f"no {_PROMOTION_SUMMARY_NAME} under the output root declares batch_id "
            f"{expected_batch_id!r} — refusing to adjudicate evidence that is not "
            "provably this invocation's"
        )
    if len(matched) > 1:
        return None, (
            f"{len(matched)} summaries declare batch_id {expected_batch_id!r}: "
            + ", ".join(str(path) for path in matched[:4])
        )
    return matched[0], ""


def _select_build_soak_summary(
    out: Path, *, expected_batch_id: str | None = None
) -> tuple[Path | None, str]:
    """The ONE promotion summary bound to this invocation, or a refusal reason.

    Selection is by IDENTITY, never by recency. The previous rule took the
    newest `batch-summary.json` under ``out`` by mtime, which infers intent from
    a filename plus a clock. Diagnostic and promotion campaigns write the same
    filename, so a root containing both silently resolved to whichever ran last.

    Observed 2026-07-27: 26 promotion-named summaries sit under the Epic-4
    DIAGNOSTIC tree (correctly preserved — a diagnostic attempt is history and is
    never renamed or deleted to tidy an audit). A reader rooted at their shared
    ancestor resolved to one of THOSE while the counted lanes had not yet written
    theirs. It would have flipped to the counted lane later purely by ordering.
    A certification must not rest on which file was touched last.

    So: ambiguity is REFUSED rather than guessed. Bind ``expected_batch_id`` to
    disambiguate deliberately; otherwise exactly one candidate must exist.
    """
    try:
        root = out.resolve()
    except OSError as exc:
        return None, f"build-soak output root is unreadable: {exc}"

    candidates = _build_soak_summary_candidates(out, root)

    if not candidates:
        return None, f"build soak produced no {_PROMOTION_SUMMARY_NAME} under {out}"

    if expected_batch_id is not None:
        selected, reason = _select_expected_batch_summary(candidates, expected_batch_id)
        if selected is None and reason.startswith("no "):
            reason = reason.replace("under the output root", f"under {out}", 1)
        return selected, reason

    if len(candidates) > 1:
        return None, (
            f"{len(candidates)} {_PROMOTION_SUMMARY_NAME} files under {out} — ambiguous, "
            "and recency is not intent. Bind the invocation's batch_id, or point the "
            "reader at the single campaign root it should adjudicate. Found: "
            + ", ".join(str(p) for p in candidates[:4])
        )
    return candidates[0], ""


def _build_soak_result(
    out: Path, *, exit_code: int, units: int, expected_batch_id: str | None = None
) -> tuple[str, int, str]:
    summary_path, refusal = _select_build_soak_summary(out, expected_batch_id=expected_batch_id)
    if summary_path is None:
        return INFRA, 0, refusal
    try:
        report = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"build-soak summary is unreadable: {exc}"
    if not isinstance(report, dict):
        return INFRA, 0, "build-soak summary is not an object"
    # Missing identity is refused, not defaulted: a summary that cannot say which
    # invocation produced it cannot be proven to be this one's.
    if not str(report.get("batch_id") or "").strip():
        return INVALID, 0, f"build-soak summary {summary_path} declares no batch_id"
    runs = report.get("runs") or []
    statuses = Counter(str(run.get("status")) for run in runs if isinstance(run, dict))
    if statuses["FAIL"]:
        return FAIL, 0, f"{statuses['FAIL']} build-soak trial(s) failed"
    if statuses["INVALID_RUN"]:
        return INVALID, 0, f"{statuses['INVALID_RUN']} build-soak trial(s) invalid"
    if statuses["INFRA_FAILURE"]:
        return INFRA, 0, f"{statuses['INFRA_FAILURE']} build-soak infrastructure failure(s)"
    if exit_code != 0:
        return INFRA, 0, f"build soak exited {exit_code} without a classified failure"
    if statuses["PASS"] < units or len(runs) < units:
        return INVALID, 0, f"only {statuses['PASS']}/{units} required build trials passed"
    return PASS, units, f"{statuses['PASS']} build-soak trial(s) passed"


def _fresh_device_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "fresh-device harness produced no result evidence"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"fresh-device result is unreadable: {exc}"
    status = report.get("status")
    if status == FAIL:
        return FAIL, 0, str(report.get("reason") or "fresh-device product check failed")
    if status != PASS or exit_code != 0:
        return INFRA, 0, str(report.get("reason") or f"fresh-device exit {exit_code}")
    fingerprint = report.get("device_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{24}", fingerprint) is None:
        return INVALID, 0, "fresh-device evidence has no valid machine fingerprint"
    checks = report.get("checks") or []
    if not checks or any(check.get("status") != PASS for check in checks):
        return INVALID, 0, "fresh-device evidence has missing or non-passing checks"
    return PASS, units, f"{len(checks)} fresh-device checks passed"


def _fresh_device_fingerprint(path: Path) -> str | None:
    """Return only a harness-produced hardware identity, never an operator label."""

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    fingerprint = report.get("device_fingerprint")
    if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{24}", fingerprint):
        return fingerprint
    return None
