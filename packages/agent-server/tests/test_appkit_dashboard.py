"""Unit tests for the AppKit verified-app regression dashboard (EPIC M — M3).

These build fake disco-verify dossiers on disk (the exact shape ``run_scenario``
writes) and assert the generator produces a stable ``summary.json`` + ``index.html``
that rolls up the AppKit golden-path results — no live server, no browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server.verify.appkit_dashboard import (
    _first_failing_check,
    build_summary,
    collect_appkit_rows,
    generate_dashboard,
    main,
    render_html,
)

# ---------------------------------------------------------------------------
# Dossier fixtures
# ---------------------------------------------------------------------------


def _verify_observation(*, passed: bool, failing: list[str] | None = None) -> dict[str, Any]:
    failing = failing or []
    seven = [
        "design_lint_clean", "schema_sql_valid", "drizzle_schema_valid",
        "worker_contract", "lead_form_posts", "local_api_roundtrip",
        "route_coverage", "section_coverage",
    ]
    checks = [{"name": n, "passed": n not in failing, "evidence": ""} for n in seven]
    return {
        "kind": "observation",
        "tool_result": {
            "success": True,
            "tool_name": "verify_appkit_app",
            "structured": {
                "passed": passed,
                "verdict": "pass" if passed else "fail",
                "failure_fingerprint": "" if passed else "wc:guard-first",
                "checks": checks,
            },
        },
    }


def _app_create_observation(name: str = "Ledgerly") -> dict[str, Any]:
    return {
        "kind": "observation",
        "tool_result": {
            "success": True,
            "tool_name": "app_create",
            "structured": {"app_name": name, "app_kind": "landing_page"},
        },
    }


def _write_dossier(
    base: Path,
    run_id: str,
    *,
    scenario: dict[str, Any],
    result: dict[str, Any],
    cid: str = "conv_x",
    events: list[dict[str, Any]] | None = None,
) -> Path:
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "conversation_id": cid,
                "scenario": scenario,
                "result": result,
            }
        ),
        encoding="utf-8",
    )
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for evt in events or []:
            fh.write(json.dumps(evt) + "\n")
    return run_dir


def _appkit_scenario(sid: str = "appkit_live_golden") -> dict[str, Any]:
    return {"id": sid, "appkit_mode": True, "expect_appkit_verify": True}


# ---------------------------------------------------------------------------
# _first_failing_check
# ---------------------------------------------------------------------------


def test_first_failing_check_respects_canonical_order() -> None:
    verdict = {
        "checks": [
            {"name": "route_coverage", "passed": False},
            {"name": "worker_contract", "passed": False},
            {"name": "drizzle_schema_valid", "passed": False},
        ]
    }
    # drizzle_schema_valid follows schema_sql_valid and precedes worker_contract.
    assert _first_failing_check(verdict) == "drizzle_schema_valid"


def test_first_failing_check_none_when_all_pass() -> None:
    verdict = {"checks": [{"name": "worker_contract", "passed": True}]}
    assert _first_failing_check(verdict) is None


# ---------------------------------------------------------------------------
# collect_appkit_rows
# ---------------------------------------------------------------------------


def test_collect_only_appkit_dossiers(tmp_path: Path) -> None:
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_appkit",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED", "passed": True,
                "validator_problems": []},
        events=[_app_create_observation(), _verify_observation(passed=True)],
    )
    # A non-AppKit dossier — must be excluded.
    _write_dossier(
        verify_root, "run_slides",
        scenario={"id": "slides_from_research_report"},
        result={"scenario_id": "slides_from_research_report", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
    )
    rows = collect_appkit_rows(verify_root, out_dir=out_dir)
    assert len(rows) == 1
    assert rows[0].scenario_id == "appkit_live_golden"
    assert rows[0].appkit_verify_passed is True
    assert rows[0].app_name == "Ledgerly"
    assert rows[0].first_failing_check is None


def test_collect_surfaces_first_failing_check(tmp_path: Path) -> None:
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_fail",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED", "passed": False,
                "validator_problems": ["expect_appkit_verify: did NOT pass"]},
        events=[
            _app_create_observation(),
            _verify_observation(passed=False, failing=["worker_contract"]),
        ],
    )
    rows = collect_appkit_rows(verify_root, out_dir=out_dir)
    assert len(rows) == 1
    assert rows[0].passed is False
    assert rows[0].appkit_verify_passed is False
    assert rows[0].first_failing_check == "worker_contract"
    assert rows[0].failure_fingerprint == "wc:guard-first"


def test_collect_handles_missing_verdict(tmp_path: Path) -> None:
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_noverify",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "ERROR", "passed": False,
                "validator_problems": ["no verify"]},
        events=[_app_create_observation()],  # verifier never ran
    )
    rows = collect_appkit_rows(verify_root, out_dir=out_dir)
    assert rows[0].appkit_verify_passed is None


def test_collect_rows_sorted_by_run_id(tmp_path: Path) -> None:
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    for rid in ("run_c", "run_a", "run_b"):
        _write_dossier(
            verify_root, rid,
            scenario=_appkit_scenario(),
            result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                    "passed": True, "validator_problems": []},
            events=[_verify_observation(passed=True)],
        )
    rows = collect_appkit_rows(verify_root, out_dir=out_dir)
    assert [r.run_id for r in rows] == ["run_a", "run_b", "run_c"]


def test_collect_links_ui_screenshots_by_cid(tmp_path: Path) -> None:
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    e2e_root = tmp_path / "e2e"
    _write_dossier(
        verify_root, "run_ui",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
        cid="conv_ui_42",
        events=[_verify_observation(passed=True)],
    )
    # A UI evidence run that references this conversation id.
    ui_run = e2e_root / "ui_run_1"
    (ui_run / "screenshots").mkdir(parents=True)
    (ui_run / "manifest.json").write_text(json.dumps({"conversation_id": "conv_ui_42"}))
    (ui_run / "screenshots" / "final.png").write_bytes(b"\x89PNG\r\n")
    rows = collect_appkit_rows(verify_root, out_dir=out_dir, e2e_root=e2e_root)
    assert any("final.png" in s for s in rows[0].ui_screenshots)


# ---------------------------------------------------------------------------
# generate_dashboard + render
# ---------------------------------------------------------------------------


def test_generate_dashboard_writes_summary_and_html(tmp_path: Path) -> None:
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_pass",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
        events=[_app_create_observation(), _verify_observation(passed=True)],
    )
    _write_dossier(
        verify_root, "run_fail",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": False, "validator_problems": ["worker_contract failed"]},
        events=[
            _app_create_observation(),
            _verify_observation(passed=False, failing=["worker_contract"]),
        ],
    )
    summary = generate_dashboard(verify_root, out_dir)

    assert (out_dir / "summary.json").exists()
    assert (out_dir / "index.html").exists()
    assert summary.total == 2
    assert summary.passed == 1
    assert summary.failed == 1
    assert summary.failing_checks == {"worker_contract": 1}

    data = json.loads((out_dir / "summary.json").read_text())
    assert data["total"] == 2
    assert len(data["rows"]) == 2

    html_text = (out_dir / "index.html").read_text()
    assert "AppKit Verified-App Regression Dashboard" in html_text
    assert "worker_contract" in html_text
    assert "1 passed" in html_text
    assert "1 failed" in html_text


def test_generate_dashboard_empty_when_no_dossiers(tmp_path: Path) -> None:
    summary = generate_dashboard(tmp_path / "missing", tmp_path / "dash")
    assert summary.total == 0
    html_text = (tmp_path / "dash" / "index.html").read_text()
    assert "No AppKit dossiers found" in html_text


# ---------------------------------------------------------------------------
# P1: no silent false-green — pass/fail + exit code factor appkit_verify_passed
# ---------------------------------------------------------------------------


def test_false_green_when_verify_missing(tmp_path: Path) -> None:
    """P1 (adversarial false-green): result.passed=True but verify_appkit_app never ran →
    the dashboard must mark the row FAILED (not green) and summary.failed must count it."""
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_falsegreen_missing",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
        events=[_app_create_observation()],  # verifier NEVER ran
    )
    summary = generate_dashboard(verify_root, out_dir)
    assert summary.total == 1
    assert summary.passed == 0
    assert summary.failed == 1
    row = summary.rows[0]
    assert row.passed is True  # the underlying result claimed pass …
    assert row.appkit_verify_passed is None  # … but verify never ran …
    assert row.green is False  # … so the overall verdict is NOT green
    html_text = (out_dir / "index.html").read_text()
    assert "false-green" in html_text  # surfaced in the row


def test_false_green_when_verify_failed(tmp_path: Path) -> None:
    """P1 (adversarial false-green): result.passed=True but verify_appkit_app FAILED →
    not green, counted as failed."""
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_falsegreen_failed",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
        events=[_app_create_observation(), _verify_observation(passed=False,
                                                               failing=["worker_contract"])],
    )
    summary = generate_dashboard(verify_root, out_dir)
    assert summary.passed == 0
    assert summary.failed == 1
    assert summary.rows[0].green is False


def test_green_only_when_both_pass(tmp_path: Path) -> None:
    """P1 control: a fully-passing dossier (result.passed AND verify passed) is green."""
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_green",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
        events=[_app_create_observation(), _verify_observation(passed=True)],
    )
    summary = generate_dashboard(verify_root, out_dir)
    assert summary.passed == 1
    assert summary.failed == 0
    assert summary.rows[0].green is True


def _run_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verify_root: Path) -> int:
    out_dir = tmp_path / "dash"
    monkeypatch.setattr(
        "sys.argv",
        ["appkit_dashboard", "--verify-root", str(verify_root), "--out", str(out_dir)],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


def test_main_exits_nonzero_on_false_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: a passed-but-verify-missing dossier must make the gate (main) exit non-zero —
    a silent false-green would otherwise exit 0 and pass CI."""
    verify_root = tmp_path / "disco-verify"
    _write_dossier(
        verify_root, "run_falsegreen",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
        events=[_app_create_observation()],  # verify missing
    )
    assert _run_main(tmp_path, monkeypatch, verify_root) == 1


def test_main_exits_zero_when_all_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 control: a fully-passing dossier exits 0."""
    verify_root = tmp_path / "disco-verify"
    _write_dossier(
        verify_root, "run_green",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": True, "validator_problems": []},
        events=[_app_create_observation(), _verify_observation(passed=True)],
    )
    assert _run_main(tmp_path, monkeypatch, verify_root) == 0


def test_render_html_escapes_problem_text(tmp_path: Path) -> None:
    verify_root = tmp_path / "disco-verify"
    out_dir = tmp_path / "dash"
    _write_dossier(
        verify_root, "run_xss",
        scenario=_appkit_scenario(),
        result={"scenario_id": "appkit_live_golden", "terminal_status": "FINISHED",
                "passed": False, "validator_problems": ["<script>alert(1)</script>"]},
        events=[_verify_observation(passed=False, failing=["worker_contract"])],
    )
    rows = collect_appkit_rows(verify_root, out_dir=out_dir)
    html_text = render_html(build_summary(rows))
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;" in html_text
