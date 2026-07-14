#!/usr/bin/env python3
"""closeout_pytest_report — a purpose-built pytest reporting plugin for the Export
Track-1 Closeout verifier's governed pytest lanes (plan §9.3 / R6b harness correction).

Loaded into a governed lane via ``-p closeout_pytest_report`` (the verifier prepends
``scripts/`` to the subprocess ``PYTHONPATH`` so the bare module name resolves). It writes
a structured JSON report to the path named by ``--closeout-report-json`` (or, as a
fallback, the ``CLOSEOUT_REPORT_JSON`` env var):

    {
      "schema": "closeout-pytest-report/v1",
      "exit_status": <int>,
      "selected":  [nodeid, ...],          # session.items — the governed-marker selection
      "categories": {nodeid: category},    # every RUNTIME/COLLECTION outcome, honestly typed
      "collection_skipped": [collector-nodeid, ...],
      "collection_errors":  [collector-nodeid, ...],
      "deselected_count": <int>,           # marker-deselected count (transparency, NOT gated)
      "counts": {category: n, ...}
    }

``category`` is one of ``passed`` / ``failed`` / ``error`` / ``skipped`` / ``xfailed`` /
``xpassed`` (a seventh, ``deselected``, is derived VERIFIER-side when a governed-SELECTED
node produces no result — a within-selection disappearance).

Why a dedicated plugin and not JUnit XML alone: JUnit collapses an xfail into
``<skipped>``, records a NON-strict xpass as a plain pass, and simply OMITS a deselected or
vanished test — so a lane that must be green ONLY when every governed-selected test truly
PASSED cannot be judged from JUnit. This plugin categorizes each outcome from pytest's own
report objects (never console text) and records the marker-governed selection, so the
verifier can require ``selected == represented-as-passed`` exactly.

Design notes:
* The intentional ``-m "not integration"`` deselection removes integration tests from the
  session; they are simply ABSENT from ``selected`` (and never appear in ``categories``),
  so the verifier's gate never treats that governed selection as a violation.
* A module-level ``pytest.skip`` with ``allow_module_level=True`` (e.g. a dormant revival
  harness) produces a skipped COLLECTION report whose test items are never collected — it
  is recorded under ``collection_skipped`` and in ``categories`` keyed by the collector
  nodeid, so it cannot slip past a selection-only comparison.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

# Outcome precedence: when a single node emits several phase reports (setup/call/teardown),
# the WORST wins so a teardown error is never masked by a passed call.
_PRECEDENCE: dict[str, int] = {
    "passed": 0,
    "xpassed": 1,
    "xfailed": 2,
    "skipped": 3,
    "failed": 4,
    "error": 5,
}


def _runtime_category(report: pytest.TestReport) -> str | None:
    """The honest category of ONE runtest phase report, or None when the phase carries no
    gating signal (a passed setup/teardown). Mirrors pytest's own outcome-letter logic:
    ``wasxfail`` present ⇒ xfailed/xpassed; a call phase ⇒ passed/failed/skipped; a
    setup/teardown failure ⇒ error; a setup skip ⇒ skipped."""
    if hasattr(report, "wasxfail"):
        if report.skipped:
            return "xfailed"
        if report.passed:
            return "xpassed"
        return "failed"
    when = report.when
    if when == "call":
        if report.passed:
            return "passed"
        if report.failed:
            return "failed"
        if report.skipped:
            return "skipped"
        return None
    # setup / teardown
    if report.failed:
        return "error"
    if report.skipped and when == "setup":
        return "skipped"
    return None


class _CloseoutReporter:
    """Accumulates the marker-governed selection and every runtime/collection outcome, then
    writes the structured JSON report at session finish."""

    def __init__(self, out_path: Path) -> None:
        self._out_path = out_path
        self._selected: list[str] = []
        self._categories: dict[str, str] = {}
        self._collection_skipped: list[str] = []
        self._collection_errors: list[str] = []
        self._deselected_count = 0
        self._exit_status = -1

    def _record(self, nodeid: str, category: str) -> None:
        prev = self._categories.get(nodeid)
        if prev is None or _PRECEDENCE[category] > _PRECEDENCE[prev]:
            self._categories[nodeid] = category

    # ---- collection ----------------------------------------------------------
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        # session.items is the FINAL selection — after the ``-m`` marker filter deselected
        # everything outside the governed marker. This is the authoritative selected set.
        self._selected = [item.nodeid for item in session.items]

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        # Marker-deselected (e.g. integration) tests land here. Recorded only as a COUNT for
        # transparency; they are intentionally outside the governed selection, never gated.
        self._deselected_count += len(items)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # A module/class skipped or errored at COLLECTION never yields test items, so it can
        # only be seen here (a module-level pytest.skip, a collection-time import error).
        if report.skipped:
            self._collection_skipped.append(report.nodeid)
            self._record(report.nodeid, "skipped")
        elif report.failed:
            self._collection_errors.append(report.nodeid)
            self._record(report.nodeid, "error")

    # ---- execution -----------------------------------------------------------
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        category = _runtime_category(report)
        if category is not None:
            self._record(report.nodeid, category)

    # ---- output --------------------------------------------------------------
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self._exit_status = int(exitstatus)
        counts: dict[str, int] = {}
        for category in self._categories.values():
            counts[category] = counts.get(category, 0) + 1
        payload = {
            "schema": "closeout-pytest-report/v1",
            "exit_status": self._exit_status,
            "selected": sorted(self._selected),
            "categories": dict(sorted(self._categories.items())),
            "collection_skipped": sorted(self._collection_skipped),
            "collection_errors": sorted(self._collection_errors),
            "deselected_count": self._deselected_count,
            "counts": dict(sorted(counts.items())),
        }
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        self._out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("closeout-report")
    group.addoption(
        "--closeout-report-json",
        action="store",
        default=None,
        dest="closeout_report_json",
        help="write the structured closeout pytest report (nodeid -> category) to this path",
    )


def pytest_configure(config: pytest.Config) -> None:
    out = config.getoption("closeout_report_json", None) or os.environ.get("CLOSEOUT_REPORT_JSON")
    if not out:
        return
    config.pluginmanager.register(_CloseoutReporter(Path(out)), "closeout-reporter-instance")
