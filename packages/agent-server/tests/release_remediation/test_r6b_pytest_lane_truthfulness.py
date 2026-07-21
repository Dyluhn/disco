"""R6b (REOPENED R6) — mutation/regression tests for the closeout verifier's per-lane
pytest-report truthfulness gate (plan §9.3).

The owner caught a fail-open: the non-live lane set green from ``exit_code == 0`` ALONE,
but pytest exits 0 with SKIPPED / xfailed tests (and a non-strict xpass records as a plain
pass, while a vanished/deselected test is simply absent) — so a lane with real skips was
wrongly GREEN. The correction runs every governed pytest lane under a purpose-built
reporting plugin (``scripts/closeout_pytest_report.py``) that writes ``{nodeid: category}``
honestly typed from pytest's own report objects, and the verifier requires
``selected == represented-as-passed`` exactly.

These tests live OUTSIDE the frozen dirs (unmarked → they run in the normal unit suite) and
import the REAL verifier + plugin modules exactly as ``test_r6_verifier_truthfulness.py``
does — a call to ``importlib.import_module``, no ``unittest.mock`` — driving the real gating
functions with crafted structured-report inputs AND a genuine tiny pytest run under the
plugin. Nothing here edits a frozen file.

Teeth proven (each drives the REAL gate to NON-green with a distinct, specific reason):
(a) a failure, (b) a skip, (c) an xfail, (d) a NON-strict xpass (invisible to JUnit),
(e) an error, (f) a within-selection disappearance/deselection, plus a module-level
collection skip named outside the selection. The intentional ``-m "not integration"``
deselection is proven NOT a violation. A missing/malformed/zero-selected report fails
CLOSED. The plugin's own categorization is proven by a real subprocess pytest run.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

verify = importlib.import_module("verify_export_track1_closeout")
plugin = importlib.import_module("closeout_pytest_report")
manifest_mod = importlib.import_module("gen_closeout_acceptance_manifest")

LaneResult = verify.LaneResult
_LANE = "python-nonlive"


def _report(selected: list[str], categories: dict[str, str], **extra: object) -> dict[str, object]:
    """A minimal structured closeout report (the shape the plugin writes)."""
    base: dict[str, object] = {
        "schema": "closeout-pytest-report/v1",
        "exit_status": 0,
        "selected": list(selected),
        "categories": dict(categories),
        "collection_skipped": [],
        "collection_errors": [],
        "deselected_count": 0,
        "counts": {},
    }
    base.update(extra)
    return base


# ---- the pure gate: every non-passing category is NON-green + specifically named --------


def test_all_selected_passed_is_green() -> None:
    rep = _report(["a.py::t1", "a.py::t2"], {"a.py::t1": "passed", "a.py::t2": "passed"})
    ok, reasons, summary = verify._evaluate_structured_pytest_report(rep, lane=_LANE)
    assert ok is True and reasons == []
    assert summary["selected_count"] == 2 and summary["passed_count"] == 2


@pytest.mark.parametrize(
    ("category", "needle"),
    [
        ("failed", "FAILED"),
        ("skipped", "SKIPPED"),
        ("xfailed", "XFAILED"),
        ("xpassed", "XPASSED"),
        ("error", "ERRORED"),
    ],
)
def test_each_nonpassed_selected_category_is_non_green_and_named(
    category: str, needle: str
) -> None:
    """(a)-(e): a governed-SELECTED test in each non-passing category makes the lane
    non-green with a reason that names BOTH the nodeid and the specific category."""
    rep = _report(["a.py::ok", "a.py::bad"], {"a.py::ok": "passed", "a.py::bad": category})
    ok, reasons, _ = verify._evaluate_structured_pytest_report(rep, lane=_LANE)
    assert ok is False
    assert any("a.py::bad" in r and needle in r for r in reasons), reasons


def test_within_selection_disappearance_is_non_green_and_named() -> None:
    """(f): a governed-SELECTED test that produced NO result (deselected / vanished within
    the governed selection) is a distinct violation."""
    rep = _report(["a.py::t1", "a.py::gone"], {"a.py::t1": "passed"})
    ok, reasons, _ = verify._evaluate_structured_pytest_report(rep, lane=_LANE)
    assert ok is False
    assert any("a.py::gone" in r and "NO result" in r for r in reasons), reasons


def test_collection_level_skip_outside_selection_is_named() -> None:
    """A module-level collection skip (its items never collected, so it is not in the
    selection) is still named as a collection-level violation — this is the ``test_router_
    overflow`` allow_module_level skip class."""
    rep = _report(
        ["a.py::t1"],
        {"a.py::t1": "passed", "packages/core/tests/test_router_overflow.py": "skipped"},
    )
    ok, reasons, _ = verify._evaluate_structured_pytest_report(rep, lane=_LANE)
    assert ok is False
    assert any("test_router_overflow.py" in r and "collection-level" in r for r in reasons)


def test_extra_passed_result_not_in_selection_is_non_green() -> None:
    rep = _report(["a.py::t1"], {"a.py::t1": "passed", "a.py::surprise": "passed"})
    ok, reasons, _ = verify._evaluate_structured_pytest_report(rep, lane=_LANE)
    assert ok is False
    assert any("a.py::surprise" in r and "not in the governed selection" in r for r in reasons)


def test_missing_or_malformed_or_empty_report_fails_closed() -> None:
    ok_none, reasons_none, _ = verify._evaluate_structured_pytest_report(None, lane=_LANE)
    assert ok_none is False and any("missing" in r for r in reasons_none)

    ok_bad, reasons_bad, _ = verify._evaluate_structured_pytest_report(
        {"selected": "not-a-list"}, lane=_LANE
    )
    assert ok_bad is False and any("malformed" in r for r in reasons_bad)

    ok_empty, reasons_empty, _ = verify._evaluate_structured_pytest_report(
        _report([], {}), lane=_LANE
    )
    assert ok_empty is False and any("zero tests" in r for r in reasons_empty)


def test_intentional_marker_deselection_is_not_a_violation() -> None:
    """Constraint 4: ``-m "not integration"`` deselects integration tests — they are absent
    from ``selected`` (and never in ``categories``), so a run where every governed-selected
    test passed is GREEN even though many tests were marker-deselected."""
    rep = _report(
        ["a.py::unit1", "a.py::unit2"],
        {"a.py::unit1": "passed", "a.py::unit2": "passed"},
        deselected_count=4242,
    )
    ok, reasons, _ = verify._evaluate_structured_pytest_report(rep, lane=_LANE)
    assert ok is True and reasons == []


# ---- lane finalization: the structured gate folds into lane.green + rejection_reasons ----


def _lane(exit_code: int, **junit: int) -> LaneResult:
    base = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    base.update(junit)
    return LaneResult(name=_LANE, status="ran", exit_code=exit_code, junit=base)


def test_finalize_lane_folds_structured_and_junit_reasons() -> None:
    lane = _lane(0, tests=10, skipped=1)
    rep = _report(["a.py::t1", "a.py::sk"], {"a.py::t1": "passed", "a.py::sk": "skipped"})
    verify._finalize_pytest_lane(lane, report=rep)
    assert lane.green is False and lane.rejected is True
    assert any("1 skipped" in r for r in lane.rejection_reasons)  # junit backstop
    assert any("a.py::sk" in r and "SKIPPED" in r for r in lane.rejection_reasons)  # structured


def test_finalize_lane_green_only_when_junit_and_structured_both_clean() -> None:
    lane = _lane(0, tests=2)
    rep = _report(["a.py::t1", "a.py::t2"], {"a.py::t1": "passed", "a.py::t2": "passed"})
    verify._finalize_pytest_lane(lane, report=rep)
    assert lane.green is True and lane.rejection_reasons == []


def test_finalize_lane_missing_report_is_non_green_even_when_junit_clean() -> None:
    """Fail-closed: a JUnit that looks clean (exit 0, no failures/errors/skips) cannot green
    the lane if the structured report is absent."""
    lane = _lane(0, tests=5)
    verify._finalize_pytest_lane(lane, report=None)
    assert lane.green is False
    assert any("missing or unparseable" in r for r in lane.rejection_reasons)


def test_finalize_lane_extra_reasons_flow_through() -> None:
    """The closeout lane's frozen node-ID inventory mismatch rides in as ``extra_reasons``."""
    lane = _lane(0, tests=5)
    rep = _report(["a.py::t1"], {"a.py::t1": "passed"})
    verify._finalize_pytest_lane(
        lane, report=rep, extra_reasons=["frozen node-ID inventory mismatch: missing=['x']"]
    )
    assert lane.green is False
    assert any("inventory mismatch" in r for r in lane.rejection_reasons)


def test_not_passed_reasons_surfaces_a_governed_lane_named_skip() -> None:
    """The final verdict narrative names the specific skip, not just ``lane not green``."""
    named = f"{_LANE}: test a.py::sk was SKIPPED (must PASS)"
    lane = LaneResult(
        name=_LANE, status="ran", green=False, rejected=True, rejection_reasons=[named]
    )
    reasons = verify._not_passed_reasons(
        author=False, clean=True, frozen_ok=True, lanes=[lane], hygiene_ok=True
    )
    assert named in reasons


# ---- the plugin itself: a genuine pytest run categorizes skip / xfail / xpass honestly ----


def test_plugin_categorizes_a_real_run_including_a_hidden_xpass(tmp_path: Path) -> None:
    """The teeth of the plugin: a REAL pytest run (subprocess, under ``-p
    closeout_pytest_report``) categorizes a genuine skip / xfail / NON-strict xpass /
    module-level collection skip correctly — and the run EXITS 0 (proving the fail-open),
    yet the real gate rejects it and names the XPASS that JUnit would have hidden."""
    (tmp_path / "test_outcomes.py").write_text(
        textwrap.dedent(
            """
            import pytest

            def test_pass():
                assert True

            @pytest.mark.skip(reason="deliberate")
            def test_skip():
                assert False

            @pytest.mark.xfail(reason="deliberate")
            def test_xfail():
                assert False

            @pytest.mark.xfail(reason="deliberate")
            def test_xpass():
                assert True
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "test_modskip.py").write_text(
        textwrap.dedent(
            """
            import pytest

            pytest.skip("dormant module", allow_module_level=True)

            def test_never():
                assert True
            """
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            ".",
            "-o",
            "addopts=",
            "-o",
            "xfail_strict=false",
            "-p",
            plugin.__name__,
            "--closeout-report-json",
            str(report_path),
            "-q",
        ],
        cwd=tmp_path,
        env=verify._pytest_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    # The fail-open in the flesh: a run with a skip + xfail + non-strict xpass EXITS 0.
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert report_path.is_file(), f"plugin wrote no report\nstdout={proc.stdout}"
    data = json.loads(report_path.read_text(encoding="utf-8"))
    cats: dict[str, str] = data["categories"]

    def _cat(suffix: str) -> str:
        return next(v for k, v in cats.items() if k.endswith(suffix))

    assert _cat("test_outcomes.py::test_pass") == "passed"
    assert _cat("test_outcomes.py::test_skip") == "skipped"
    assert _cat("test_outcomes.py::test_xfail") == "xfailed"
    assert _cat("test_outcomes.py::test_xpass") == "xpassed"
    # Module-level skip → a collection-level skip keyed by the module nodeid, never collected
    # as an item (so it is NOT in ``selected``) yet still recorded.
    assert any(k.endswith("test_modskip.py") and v == "skipped" for k, v in cats.items())
    assert any("test_modskip.py" in nid for nid in data["collection_skipped"])
    assert "test_outcomes.py::test_never" not in " ".join(data["selected"])

    # The real gate rejects this exit-0 run and names the XPASS JUnit would have hidden.
    ok, reasons, _ = verify._evaluate_structured_pytest_report(data, lane=_LANE)
    assert ok is False
    assert any("test_xpass" in r and "XPASSED" in r for r in reasons), reasons
    assert any("test_skip" in r and "SKIPPED" in r for r in reasons), reasons
    assert any("test_xfail" in r and "XFAILED" in r for r in reasons), reasons


# ---- STALE-EVIDENCE-DIR + EXIT-0 CRASH: the launcher unlinks before every lane run --------


def test_stale_report_is_unlinked_so_an_exit0_crash_fails_closed(tmp_path: Path) -> None:
    """A REUSED ``--evidence-dir`` may hold a prior GREEN structured report at the lane's
    FIXED path. If a governed test hard-crashes the interpreter with exit code 0
    (``os._exit(0)``) so ``pytest_sessionfinish`` never overwrites it, the launcher must have
    UNLINKED the stale report FIRST — leaving it ABSENT so the fail-closed path fires
    (non-green) — instead of the stale green report being read and the lane falsely greened.
    Drives the REAL ``_run_pytest_lane``."""
    repo = tmp_path / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (tests / "test_crash.py").write_text(
        textwrap.dedent(
            """
            import os

            def test_hard_exit_zero():
                os._exit(0)
            """
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    report_name = "closeout-report-nonlive.json"
    junit_name = "pytest-nonlive.xml"
    node = "tests/test_crash.py::test_hard_exit_zero"
    # A prior GREEN report + junit sitting at the fixed lane paths (a reused evidence dir).
    stale = _report([node], {node: "passed"})
    (evidence / report_name).write_text(json.dumps(stale), encoding="utf-8")
    (evidence / junit_name).write_text(
        '<testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    # Sanity: that stale report WOULD read GREEN if it survived (the falsely-green risk).
    ok_stale, _, _ = verify._evaluate_structured_pytest_report(stale, lane=_LANE)
    assert ok_stale is True

    lane = verify._run_pytest_lane(
        repo,
        evidence,
        name=_LANE,
        paths=("tests",),
        marker="",
        junit_filename=junit_name,
        report_filename=report_name,
    )
    # The exit-0 crash: the subprocess returns 0 and pytest_sessionfinish never fired.
    assert lane.exit_code == 0, "os._exit(0) must make the lane subprocess exit 0"
    # Because the launcher unlinked the stale report first, nothing overwrote it -> ABSENT.
    assert not Path(lane.report_path or "").is_file(), "stale report must have been unlinked"
    loaded = verify._load_structured_report(Path(lane.report_path or ""))
    assert loaded is None
    verify._finalize_pytest_lane(lane, report=loaded)
    assert lane.green is False
    assert any("missing or unparseable" in r for r in lane.rejection_reasons)


# ---- wiring guards: every governed pytest lane loads the plugin; it is a frozen file ------


def test_governed_lanes_wire_the_plugin_and_report_flag() -> None:
    """Source guard (cf. ``test_scanner_lane_git_diff_invocation_forces_ab_prefixes``): both
    lane runners (_run_pytest_lane for nonlive+closeout, _run_live_lane) load the plugin and
    pass the structured-report flag, and extend PYTHONPATH so the bare name resolves."""
    src = (_SCRIPTS_DIR / "verify_export_track1_closeout.py").read_text(encoding="utf-8")
    assert src.count("--closeout-report-json") >= 2
    assert "_CLOSEOUT_REPORT_PLUGIN" in src and "os.pathsep" in src
    assert plugin.__name__ == "closeout_pytest_report"


def test_plugin_is_frozen() -> None:
    assert "scripts/closeout_pytest_report.py" in manifest_mod.FROZEN_FILES


# ---- C9-01 reconciliation: the frozen §3.2 baseline allowlist is EXACT ---------


_ROUTER = "packages/core/tests/test_router_overflow.py"
_APPKIT_XFAIL = (
    "packages/core/tests/test_appkit_directory.py"
    "::test_unknown_app_kind_lowers_as_lead_gen_byte_identical"
)


def test_nonlive_baseline_pairs_are_accepted_exactly() -> None:
    """The two §3.2-permitted pre-existing outcomes (dormant router collection skip +
    stale-upstream AppKit xfail) are accepted for the non-live lane when they appear
    with EXACTLY their frozen categories — and are recorded in the summary."""
    report = _report(
        ["a::t1", _APPKIT_XFAIL],
        {"a::t1": "passed", _APPKIT_XFAIL: "xfailed", _ROUTER: "skipped"},
    )
    ok, reasons, summary = verify._evaluate_structured_pytest_report(
        report, lane=_LANE, baseline_allowlist=verify._NONLIVE_BASELINE_ALLOWLIST
    )
    assert ok, reasons
    assert {e["nodeid"] for e in summary["baseline_allowlisted"]} == {_ROUTER, _APPKIT_XFAIL}


def test_nonlive_extra_skip_stays_fatal_despite_the_allowlist() -> None:
    """ANY additional skip — even alongside the permitted pair — remains fatal."""
    report = _report(
        ["a::t1", "a::t2", _APPKIT_XFAIL],
        {
            "a::t1": "passed",
            "a::t2": "skipped",
            _APPKIT_XFAIL: "xfailed",
            _ROUTER: "skipped",
        },
    )
    ok, reasons, _ = verify._evaluate_structured_pytest_report(
        report, lane=_LANE, baseline_allowlist=verify._NONLIVE_BASELINE_ALLOWLIST
    )
    assert not ok
    assert any("a::t2" in r and "SKIPPED" in r for r in reasons)


def test_nonlive_allowlisted_node_in_a_different_category_stays_fatal() -> None:
    """A CHANGED baseline outcome (the xfail node XPASSING) is not the frozen pair
    and must remain fatal — the allowlist matches (nodeid, category) exactly."""
    report = _report(
        ["a::t1", _APPKIT_XFAIL],
        {"a::t1": "passed", _APPKIT_XFAIL: "xpassed", _ROUTER: "skipped"},
    )
    ok, reasons, _ = verify._evaluate_structured_pytest_report(
        report, lane=_LANE, baseline_allowlist=verify._NONLIVE_BASELINE_ALLOWLIST
    )
    assert not ok
    assert any(_APPKIT_XFAIL in r and "XPASSED" in r for r in reasons)


def test_focused_and_live_lanes_take_no_allowlist() -> None:
    """The baseline applies to python-nonlive ONLY: the same pair presented to a lane
    evaluated WITHOUT an allowlist (the default — focused closeout / live) is fatal."""
    report = _report(
        ["a::t1", _APPKIT_XFAIL],
        {"a::t1": "passed", _APPKIT_XFAIL: "xfailed", _ROUTER: "skipped"},
    )
    ok, reasons, _ = verify._evaluate_structured_pytest_report(report, lane="python-closeout")
    assert not ok
    assert any(_APPKIT_XFAIL in r for r in reasons)
    assert any(_ROUTER in r for r in reasons)


def test_junit_skip_count_must_equal_the_baseline_exactly() -> None:
    """The coarse JUnit backstop is exact: fewer skips than the frozen baseline (a
    silently 'fixed' dormant module) is as fatal as more — divergence either way is a
    governance event, not a silent pass."""
    ok_junit = {"tests": 100, "failures": 0, "errors": 0, "skipped": 2}
    assert verify._junit_reasons(ok_junit, 0, _LANE, allowed_skipped=2) == []
    high = {"tests": 100, "failures": 0, "errors": 0, "skipped": 3}
    assert any("exactly 2" in r for r in verify._junit_reasons(high, 0, _LANE, allowed_skipped=2))
    low = {"tests": 100, "failures": 0, "errors": 0, "skipped": 1}
    assert any("exactly 2" in r for r in verify._junit_reasons(low, 0, _LANE, allowed_skipped=2))
    zero_default = {"tests": 100, "failures": 0, "errors": 0, "skipped": 1}
    assert any("exactly 0" in r for r in verify._junit_reasons(zero_default, 0, "python-closeout"))
