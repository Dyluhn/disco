"""Model-free suite result and artifact adjudication."""

from __future__ import annotations

import json
import math
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
    passed = statuses["PASS"]
    if statuses["FAIL"]:
        return FAIL, 0, f"{statuses['FAIL']} build-soak trial(s) failed"
    if statuses["INVALID_RUN"]:
        return INVALID, passed, f"{statuses['INVALID_RUN']} build-soak trial(s) invalid"
    if statuses["INFRA_FAILURE"]:
        return INFRA, passed, f"{statuses['INFRA_FAILURE']} build-soak infrastructure failure(s)"
    if exit_code != 0:
        return INFRA, passed, f"build soak exited {exit_code} without a classified failure"
    if passed < units or len(runs) < units:
        return INVALID, passed, f"only {passed}/{units} required build trials passed"
    return PASS, units, f"{passed} build-soak trial(s) passed"


def _build_soak_pass_identity(run: dict[str, Any], index: int) -> tuple[str, str, str]:
    """Parse one PASS owner without mixing row validation into cohort selection."""

    seed = run.get("seed")
    conversation_id = run.get("conversation_id")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        return "", "", f"PASS run {index} has no exact nonnegative seed"
    if (
        not isinstance(conversation_id, str)
        or not conversation_id
        or conversation_id != conversation_id.strip()
    ):
        return "", "", f"PASS run {index} has no exact conversation_id"
    return f"build-soak-seed:{seed}", conversation_id, ""


def _build_soak_pass_identities(
    out: Path,
    *,
    expected_batch_id: str | None = None,
) -> tuple[list[str], frozenset[str], str]:
    """Return exact seed and conversation owners for every completed PASS cell."""

    summary_path, refusal = _select_build_soak_summary(
        out, expected_batch_id=expected_batch_id
    )
    if summary_path is None:
        return [], frozenset(), refusal
    try:
        report = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], frozenset(), f"build-soak summary is unreadable: {exc}"
    runs = report.get("runs") if isinstance(report, dict) else None
    if not isinstance(runs, list):
        return [], frozenset(), "build-soak summary runs must be a list"
    trial_ids: list[str] = []
    conversation_ids: list[str] = []
    for index, run in enumerate(runs):
        if not isinstance(run, dict) or run.get("status") != "PASS":
            continue
        trial_id, conversation_id, identity_error = _build_soak_pass_identity(run, index)
        if identity_error:
            return [], frozenset(), identity_error
        trial_ids.append(trial_id)
        conversation_ids.append(conversation_id)
    if len(trial_ids) != len(set(trial_ids)):
        return [], frozenset(), "PASS runs repeat a seed identity"
    if len(conversation_ids) != len(set(conversation_ids)):
        return [], frozenset(), "PASS runs repeat a conversation identity"
    return trial_ids, frozenset(conversation_ids), ""


_FRESH_DEVICE_PHASES = (
    "pristine-device",
    "clean-clone",
    "install-and-boot",
    "first-pair-and-config",
    "model-and-internet",
    "build-search-export",
    "cold-restart",
    "upgrade-in-place",
    "backup-restore",
    "uninstall",
)

_FRESH_DEVICE_EVIDENCE_KEYS = {
    "pristine-device": {
        "device_label",
        "fingerprint",
        "engine",
        "containers",
        "images",
        "volumes",
        "checked_ports",
        "cpu_count",
        "memory_available_bytes",
        "disk_free_bytes",
    },
    "clean-clone": {"base_commit", "upgrade_commit"},
    "install-and-boot": {"ui", "image_ids"},
    "first-pair-and-config": set(),
    "model-and-internet": set(),
    "build-search-export": {"project_count", "project_digests"},
    "cold-restart": {"readiness_seconds", "data_digest", "project_digests"},
    "upgrade-in-place": {
        "readiness_seconds",
        "stable_data_digest",
        "project_digests",
        "base_commit",
        "upgrade_commit",
        "image_ids",
    },
    "backup-restore": {
        "readiness_seconds",
        "backup_digest",
        "project_digests",
        "project_count",
    },
    "uninstall": set(),
}


def _fresh_nonnegative_int(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _fresh_duration(value: Any, *, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= maximum
    )


def _fresh_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _fresh_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, str) and bool(item) and item == item.strip() for item in value
        )
        and value == sorted(set(value))
    )


def _fresh_project_digests(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(project_id, str)
            and bool(project_id)
            and project_id == project_id.strip()
            and _fresh_sha256(digest)
            for project_id, digest in value.items()
        )
        and list(value) == sorted(value)
    )


def _fresh_check_map(checks: Any) -> tuple[dict[str, dict[str, Any]] | None, str]:
    if not isinstance(checks, list) or len(checks) != len(_FRESH_DEVICE_PHASES):
        return None, "fresh-device evidence does not contain the exact ten checks"
    if any(not isinstance(check, dict) for check in checks):
        return None, "fresh-device evidence contains a malformed check"
    if tuple(check.get("id") for check in checks) != _FRESH_DEVICE_PHASES:
        return None, "fresh-device checks are missing, duplicated, or out of order"

    evidence_by_phase: dict[str, dict[str, Any]] = {}
    for phase, check in zip(_FRESH_DEVICE_PHASES, checks, strict=True):
        if set(check) != {"id", "status", "detail", "evidence"}:
            return None, f"fresh-device {phase} check has an invalid shape"
        detail = check.get("detail")
        evidence = check.get("evidence")
        if check.get("status") != PASS or not isinstance(detail, str) or not detail.strip():
            return None, f"fresh-device {phase} check has invalid evidence"
        if not isinstance(evidence, dict) or set(evidence) != _FRESH_DEVICE_EVIDENCE_KEYS[phase]:
            return None, f"fresh-device {phase} check has invalid evidence"
        evidence_by_phase[phase] = evidence
    return evidence_by_phase, ""


def _fresh_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_pristine(evidence: dict[str, Any], fingerprint: str) -> bool:
    ports = evidence["checked_ports"]
    if not isinstance(ports, list) or len(ports) != 3:
        return False
    host_counts = all(
        _fresh_nonnegative_int(evidence[field]) for field in ("containers", "images", "volumes")
    )
    resources = all(
        _fresh_nonnegative_int(evidence[field], minimum=1)
        for field in ("cpu_count", "memory_available_bytes", "disk_free_bytes")
    )
    valid_ports = all(_fresh_nonnegative_int(port, minimum=1) and port <= 65_535 for port in ports)
    if not valid_ports:
        return False
    return all(
        (
            evidence["fingerprint"] == fingerprint,
            _fresh_nonempty_string(evidence["device_label"]),
            _fresh_nonempty_string(evidence["engine"]),
            host_counts,
            resources,
            valid_ports,
            len(set(ports)) == 3,
        )
    )


def _valid_clone(evidence: dict[str, Any]) -> bool:
    base_commit = evidence["base_commit"]
    upgrade_commit = evidence["upgrade_commit"]
    return all(
        (
            _fresh_nonempty_string(base_commit),
            _fresh_nonempty_string(upgrade_commit),
            base_commit != upgrade_commit,
        )
    )


def _valid_install(evidence: dict[str, Any]) -> bool:
    ui = evidence["ui"]
    return (
        isinstance(ui, str)
        and ui.startswith(("http://", "https://"))
        and _fresh_string_list(evidence["image_ids"])
    )


def _valid_build(evidence: dict[str, Any]) -> bool:
    projects = evidence["project_digests"]
    if not _fresh_project_digests(projects):
        return False
    count = evidence["project_count"]
    return _fresh_nonnegative_int(count, minimum=1) and count == len(projects)


def _valid_restart(evidence: dict[str, Any], projects: Any) -> bool:
    return all(
        (
            _fresh_duration(evidence["readiness_seconds"], maximum=900),
            _fresh_sha256(evidence["data_digest"]),
            evidence["project_digests"] == projects,
        )
    )


def _valid_upgrade(
    evidence: dict[str, Any],
    *,
    projects: Any,
    base_commit: Any,
    upgrade_commit: Any,
    install_images: Any,
) -> bool:
    image_ids = evidence["image_ids"]
    if not _fresh_string_list(image_ids) or not isinstance(install_images, list):
        return False
    return all(
        (
            _fresh_duration(evidence["readiness_seconds"], maximum=900),
            _fresh_sha256(evidence["stable_data_digest"]),
            evidence["project_digests"] == projects,
            evidence["base_commit"] == base_commit,
            evidence["upgrade_commit"] == upgrade_commit,
            set(install_images).issubset(image_ids),
        )
    )


def _valid_restore(evidence: dict[str, Any], projects: Any) -> bool:
    if not isinstance(projects, dict):
        return False
    count = evidence["project_count"]
    return all(
        (
            _fresh_duration(evidence["readiness_seconds"], maximum=3_600),
            _fresh_sha256(evidence["backup_digest"]),
            evidence["project_digests"] == projects,
            _fresh_nonnegative_int(count, minimum=1),
            count == len(projects),
        )
    )


def _fresh_device_checks_reason(checks: Any, fingerprint: str) -> str:
    evidence_by_phase, reason = _fresh_check_map(checks)
    if evidence_by_phase is None:
        return reason

    pristine = evidence_by_phase["pristine-device"]
    if not _valid_pristine(pristine, fingerprint):
        return "fresh-device pristine host evidence is invalid"

    clone = evidence_by_phase["clean-clone"]
    if not _valid_clone(clone):
        return "fresh-device clone identities are invalid"

    install = evidence_by_phase["install-and-boot"]
    if not _valid_install(install):
        return "fresh-device install evidence is invalid"

    build = evidence_by_phase["build-search-export"]
    baseline_projects = build["project_digests"]
    if not _valid_build(build):
        return "fresh-device build/export evidence is invalid"

    restart = evidence_by_phase["cold-restart"]
    if not _valid_restart(restart, baseline_projects):
        return "fresh-device restart evidence is invalid"

    upgrade = evidence_by_phase["upgrade-in-place"]
    if not _valid_upgrade(
        upgrade,
        projects=baseline_projects,
        base_commit=clone["base_commit"],
        upgrade_commit=clone["upgrade_commit"],
        install_images=install["image_ids"],
    ):
        return "fresh-device upgrade evidence is invalid"

    restore = evidence_by_phase["backup-restore"]
    if not _valid_restore(restore, baseline_projects):
        return "fresh-device restore evidence is invalid"
    return ""


def _fresh_device_result(path: Path, *, exit_code: int, units: int) -> tuple[str, int, str]:
    if not path.is_file():
        return INFRA, 0, "fresh-device harness produced no result evidence"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return INFRA, 0, f"fresh-device result is unreadable: {exc}"
    if not isinstance(report, dict):
        return INFRA, 0, "fresh-device result is not an object"
    status = report.get("status")
    if status == FAIL:
        return FAIL, 0, str(report.get("reason") or "fresh-device product check failed")
    if status != PASS or exit_code != 0:
        return INFRA, 0, str(report.get("reason") or f"fresh-device exit {exit_code}")
    fingerprint = report.get("device_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{24}", fingerprint) is None:
        return INVALID, 0, "fresh-device evidence has no valid machine fingerprint"
    checks = report.get("checks")
    invalid_reason = _fresh_device_checks_reason(checks, fingerprint)
    if invalid_reason:
        return INVALID, 0, invalid_reason
    return PASS, units, f"{len(checks)} fresh-device checks passed"


def _fresh_device_fingerprint(path: Path) -> str | None:
    """Return only a harness-produced hardware identity, never an operator label."""

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(report, dict):
        return None
    fingerprint = report.get("device_fingerprint")
    if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{24}", fingerprint):
        return fingerprint
    return None
