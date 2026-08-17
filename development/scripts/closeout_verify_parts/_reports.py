"""Pytest lane execution + structured-report evaluation — extracted from
:mod:`verify_export_track1_closeout`.

A governed pytest lane is green ONLY when EVERY governed-selected test truly
PASSED. JUnit cannot prove that (an xfail collapses to <skipped>, a NON-strict
xpass records as a plain pass, a deselected/vanished test is simply absent), so
each governed pytest lane is run under the purpose-built reporting plugin
(``development/scripts/closeout_pytest_report.py``), which writes ``{nodeid: category}``
(category honestly typed from pytest's own report objects, never console text)
plus the marker-governed selection. The verifier then requires
``selected == represented-as-passed`` exactly.

Note: ``_run_pytest_lane`` itself (the function that actually dispatches a
governed lane) stays physically defined in the parent module, not here — see
``verify_export_track1_closeout.py`` module docstring for why. This module
carries the constants + evaluation/finalization helpers it (and the parent's
``_run_live_lane``) build on.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# ---- structured per-test report plugin (R6b harness correction, plan §9.3) --------
# ``development/scripts/`` is prepended to the lane subprocess PYTHONPATH (by the parent's
# ``_pytest_env``) so ``-p closeout_pytest_report`` resolves by bare module name.
_CLOSEOUT_REPORT_PLUGIN = "closeout_pytest_report"
_NONLIVE_REPORT_FILE = "closeout-report-nonlive.json"
_CLOSEOUT_REPORT_FILE = "closeout-report-closeout.json"
_LIVE_REPORT_FILE = "closeout-report-live.json"
_CAPTURE_REPORT_FILE = "closeout-report-capture.json"
# The one green category; every other category (failed/error/skipped/xfailed/xpassed) — and
# a governed-selected node that produced NO result (a within-selection deselection) — is a
# violation that forces the lane NON-green with a distinct, specific reason.
_PASS_CATEGORY = "passed"


@dataclass
class LaneResult:
    name: str
    status: str  # "ran"
    green: bool | None = None
    exit_code: int | None = None
    junit: dict[str, int] | None = None
    junit_path: str | None = None
    rejected: bool | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    report_path: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


def _load_structured_report(path: Path) -> dict[str, object] | None:
    """Read a structured closeout pytest report. Returns None (fail-closed) when the file is
    absent or unparseable — the governed lane then rejects for a missing report."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_junit(path: Path) -> dict[str, int]:
    """Aggregate counts from a JUnit XML, summed across testsuites."""
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if not path.is_file():
        return counts
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, "0") or "0")
    return counts


def _junit_reasons(
    junit: dict[str, int], exit_code: int | None, lane: str, *, allowed_skipped: int = 0
) -> list[str]:
    """The JUnit-aggregate rejection reasons for a governed pytest lane (plan §1.2/§3.2):
    a nonzero exit, any failure/error, a skip count that is not EXACTLY the lane's
    permitted baseline (0 for every lane except python-nonlive's frozen §3.2 pairs —
    JUnit counts both a collection skip and an xfail under ``skipped``), or a zero-test
    collection. Kept as the coarse backstop; the fine-grained per-test truth comes from
    the structured report."""
    reasons: list[str] = []
    if exit_code != 0:
        reasons.append(f"{lane} pytest exit code {exit_code} (expected 0)")
    for bad in ("failures", "errors"):
        if junit.get(bad, 0) > 0:
            reasons.append(f"{lane} lane has {junit[bad]} {bad} (must be 0)")
    skipped = junit.get("skipped", 0)
    if skipped != allowed_skipped:
        reasons.append(
            f"{lane} lane has {skipped} skipped (must be exactly {allowed_skipped}: "
            "the frozen §3.2 baseline, nothing more, nothing less)"
        )
    if junit.get("tests", 0) == 0:
        reasons.append(f"{lane} lane collected zero tests")
    return reasons


# The work-order §3.2-permitted PRE-EXISTING non-pass baseline, applied to the FULL
# non-integration lane (python-nonlive) ONLY. Exact (nodeid -> category) pairs, frozen:
# any OTHER non-pass outcome, an allowlisted node in a DIFFERENT category, or a change
# in the baseline's junit-level count stays fatal. The focused closeout and live lanes
# take no allowlist and remain strictly zero-skip (C9-01 reconciliation, 2026-07-17).
_NONLIVE_BASELINE_ALLOWLIST: dict[str, str] = {
    # Dormant v1.2 router revival harness — documented module-level collection skip.
    "current/packages/core/tests/test_router_overflow.py": "skipped",
    # Historical stale-upstream xfail, documented in-marker at nightly HEAD b6cc4a1a.
    "current/packages/core/tests/test_appkit_directory.py"
    "::test_unknown_app_kind_lowers_as_lead_gen_byte_identical": "xfailed",
}


# Human-readable clause per non-passing category, for the not_passed_reasons narrative.
_CATEGORY_REASON: dict[str, str] = {
    "skipped": "was SKIPPED",
    "xfailed": "XFAILED (expected-fail under an xfail marker)",
    "xpassed": "XPASSED (unexpected pass under an xfail marker)",
    "failed": "FAILED",
    "error": "ERRORED",
}


def _evaluate_selected_nodes(
    lane: str,
    selected_set: set[str],
    cats: dict[str, str],
    baseline_allowlist: dict[str, str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Every governed-selected node must be represented as passed (or be an EXACT frozen
    §3.2 baseline pair — python-nonlive only; see ``_NONLIVE_BASELINE_ALLOWLIST``).
    Returns ``(reasons, allowlisted)`` for the selected-node half of the gate."""
    reasons: list[str] = []
    allowlisted: list[dict[str, str]] = []
    for nid in sorted(selected_set):
        cat = cats.get(nid)
        if cat is None:
            reasons.append(
                f"{lane}: selected test {nid} produced NO result "
                "(deselected / vanished within the governed selection)"
            )
        elif cat != _PASS_CATEGORY:
            if baseline_allowlist.get(nid) == cat:
                allowlisted.append({"nodeid": nid, "category": cat})
                continue
            clause = _CATEGORY_REASON.get(cat, f"reported category {cat!r}")
            reasons.append(f"{lane}: test {nid} {clause} (must PASS)")
    return reasons, allowlisted


def _evaluate_unselected_nodes(
    lane: str,
    selected_set: set[str],
    cats: dict[str, str],
    baseline_allowlist: dict[str, str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Any outcome OUTSIDE the selection: a collection-level skip/error, or a stray pass.
    Returns ``(reasons, allowlisted)`` for the unselected-node half of the gate."""
    reasons: list[str] = []
    allowlisted: list[dict[str, str]] = []
    for nid in sorted(cats):
        if nid in selected_set:
            continue
        cat = cats[nid]
        if cat == _PASS_CATEGORY:
            reasons.append(f"{lane}: unexpected passed result {nid} not in the governed selection")
        elif baseline_allowlist.get(nid) == cat:
            allowlisted.append({"nodeid": nid, "category": cat})
        else:
            clause = _CATEGORY_REASON.get(cat, f"reported category {cat!r}")
            reasons.append(
                f"{lane}: collection-level {clause} at {nid} "
                "(a governed collector was skipped/errored)"
            )
    return reasons, allowlisted


def _evaluate_structured_pytest_report(
    report: dict[str, object] | None,
    *,
    lane: str,
    baseline_allowlist: dict[str, str] | None = None,
) -> tuple[bool, list[str], dict[str, object]]:
    """Decide, from the structured ``{nodeid: category}`` report the closeout plugin wrote,
    whether EVERY governed-selected test is represented as ``passed`` — the fine-grained
    truth JUnit cannot give (plan §9.3). Returns ``(ok, reasons, summary)``. Fail-CLOSED: a
    missing/malformed report, or a report that selected zero tests, is NOT ok.

    The gate is exact set-equality ``selected == represented-as-passed``, evaluated by
    ``_evaluate_selected_nodes`` (governed-selected nodes) and ``_evaluate_unselected_nodes``
    (everything else the report represented), in that order:

    * a governed-selected node whose category is failed/error/skipped/xfailed/xpassed, or
      that produced NO result at all (a within-selection deselection / disappearance), is a
      distinct, specifically-named violation;
    * a non-passed outcome OUTSIDE the selection (a module-level collection skip/error keyed
      by its collector nodeid) is likewise named;
    * a passed result that is not in the governed selection is an unexpected extra.

    The intentional ``-m "not integration"`` deselection is honored: those tests are absent
    from ``selected`` (and never appear in ``categories``), so they are never gated. Pure +
    importable so the committed mutation tests drive it with crafted reports."""
    if report is None:
        return (
            False,
            [f"{lane}: structured pytest report missing or unparseable (fail-closed)"],
            {},
        )
    selected = report.get("selected")
    categories = report.get("categories")
    if not isinstance(selected, list) or not isinstance(categories, dict):
        return (
            False,
            [f"{lane}: structured pytest report malformed (missing selected/categories)"],
            {},
        )
    selected_set = {str(x) for x in selected}
    cats: dict[str, str] = {str(k): str(v) for k, v in categories.items()}
    if not selected_set:
        return False, [f"{lane}: structured report selected zero tests"], {}

    baseline_allowlist = baseline_allowlist or {}
    selected_reasons, selected_allowlisted = _evaluate_selected_nodes(
        lane, selected_set, cats, baseline_allowlist
    )
    unselected_reasons, unselected_allowlisted = _evaluate_unselected_nodes(
        lane, selected_set, cats, baseline_allowlist
    )
    reasons = selected_reasons + unselected_reasons
    allowlisted = selected_allowlisted + unselected_allowlisted

    non_passed = sorted(nid for nid, cat in cats.items() if cat != _PASS_CATEGORY)
    summary: dict[str, object] = {
        "selected_count": len(selected_set),
        "passed_count": sum(1 for cat in cats.values() if cat == _PASS_CATEGORY),
        "represented_count": len(cats),
        "non_passed": non_passed,
        "collection_skipped": report.get("collection_skipped", []),
        "collection_errors": report.get("collection_errors", []),
        "deselected_count": report.get("deselected_count"),
        "counts": report.get("counts", {}),
        "baseline_allowlisted": allowlisted,
    }
    return (not reasons), reasons, summary


def _finalize_pytest_lane(
    lane: LaneResult,
    *,
    report: dict[str, object] | None,
    extra_reasons: list[str] | None = None,
    baseline_allowlist: dict[str, str] | None = None,
) -> dict[str, object]:
    """Set a governed pytest lane's green from BOTH the JUnit aggregate AND the structured
    per-test report (and any lane-specific ``extra_reasons``, e.g. the closeout frozen
    node-ID inventory mismatch), in place. Green ONLY when there are zero reasons across all
    sources. Returns the structured summary for the lane detail."""
    baseline = baseline_allowlist or {}
    reasons = _junit_reasons(
        lane.junit or {}, lane.exit_code, lane.name, allowed_skipped=len(baseline)
    )
    _, structured_reasons, summary = _evaluate_structured_pytest_report(
        report, lane=lane.name, baseline_allowlist=baseline
    )
    reasons.extend(structured_reasons)
    if extra_reasons:
        reasons.extend(extra_reasons)
    lane.rejected = bool(reasons)
    lane.rejection_reasons = reasons
    lane.green = not lane.rejected
    return summary
