"""P1B-LIVE-3b durable automation: the capture-JSON → dossier → classify bridge that the live
Playwright product-harness spec drives. A green capture classifies PASS (exit 0); a broken
slice / missing autonomous flag returns non-PASS (non-zero) so the spec fails loudly."""

from __future__ import annotations

import json

import pytest
from _eventlog import action, msg, observation, plan, status
from harness.product_build.classify_captured import classify_capture, main

_LEDGER = [{"host": "api.minimaxi.chat", "model": "MiniMax-M3"}]


def _autonomous_log() -> list[dict]:
    return [
        msg(1, "user", "Build a one-page coffee shop site."),
        status(2, "RUNNING"),
        plan(3, steps=2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "file_write", args={"path": "index.html"}, action_id="a5"),
        observation(6, "a5", tool="file_write", success=True),
        status(7, "FINISHED"),
    ]


def _green_capture() -> dict:
    return {
        "product_evidence": {
            "browser_ws": {"connections": 1},
            "lifecycle": {"terminal": "FINISHED", "statuses": ["RUNNING", "FINISHED"]},
            "preview": {"owner": "platform", "manual_port": False},
            "shown": {"artifact_shown": True, "preview_shown": True},
            "verification": {"ready_for_verification_called": True, "passed": True},
            "cleanup": {"orphans": 0, "workspace_released": True},
        },
        "provider_records": _LEDGER,
        "events": _autonomous_log(),
        "autonomous": True,
        "run_id": "cap1",
        "scenario_id": "static_site_smoke",
    }


def _write_capture(tmp_path, cap) -> str:
    p = tmp_path / "captured.json"
    p.write_text(json.dumps(cap), encoding="utf-8")
    return str(p)


def test_green_capture_classifies_pass(tmp_path) -> None:
    cap = _write_capture(tmp_path, _green_capture())
    result = classify_capture(cap, tmp_path / "dossier")
    assert result["status"] == "PASS", result


def test_main_returns_zero_on_pass(tmp_path) -> None:
    cap = _write_capture(tmp_path, _green_capture())
    assert main(["classify_captured", cap, str(tmp_path / "dossier")]) == 0


def test_deepseek_direct_capture_is_exactly_provider_bound(tmp_path) -> None:
    cap = _green_capture()
    cap["scenario_id"] = "static_site_smoke_deepseek_direct"
    cap["provider_records"] = [
        {"host": "api.deepseek.com", "model": "deepseek-v4-flash"}
    ]
    assert classify_capture(_write_capture(tmp_path, cap), tmp_path / "dossier")["status"] == "PASS"

    cap["provider_records"] = [{"host": "api.deepseek.com", "model": "MiniMax-M3"}]
    result = classify_capture(_write_capture(tmp_path, cap), tmp_path / "wrong-model")
    assert result["status"] == "FAIL" and result["code"] == "PROVIDER_WRONG_MODEL", result


def test_main_nonzero_when_autonomous_flag_dropped(tmp_path) -> None:
    # the SAME autonomous-shaped run, but the capture forgot to record autonomous=True →
    # the approval-gate ordering fails → non-PASS → the spec must see a non-zero exit.
    bad = _green_capture()
    bad["autonomous"] = False
    cap = _write_capture(tmp_path, bad)
    assert main(["classify_captured", cap, str(tmp_path / "dossier")]) == 1


def test_main_nonzero_on_broken_slice(tmp_path) -> None:
    bad = _green_capture()
    bad["product_evidence"]["browser_ws"]["connections"] = 0  # WS never connected
    cap = _write_capture(tmp_path, bad)
    assert main(["classify_captured", cap, str(tmp_path / "dossier")]) == 1


# --- P10: scenario-aware routing (export scenario) ----------------------------
def _green_export_capture() -> dict:
    cap = _green_capture()
    cap["product_evidence"]["export"] = {
        "requested": True,
        "download_present": True,
        "download_bytes": 2048,
        "workspace_match": True,
    }
    cap["scenario_id"] = "export_smoke"
    return cap


def test_export_capture_classifies_pass(tmp_path) -> None:
    cap = _write_capture(tmp_path, _green_export_capture())
    assert classify_capture(cap, tmp_path / "dossier")["status"] == "PASS"


def test_export_capture_without_download_is_nonzero(tmp_path) -> None:
    bad = _green_export_capture()
    bad["product_evidence"]["export"] = {
        "requested": True,
        "download_present": False,
        "download_bytes": 0,
    }
    cap = _write_capture(tmp_path, bad)
    assert main(["classify_captured", cap, str(tmp_path / "dossier")]) == 1


def test_unknown_scenario_id_raises(tmp_path) -> None:
    # a typo'd / unknown scenario must NOT silently fall back to a laxer scenario.
    bad = _green_capture()
    bad["scenario_id"] = "bogus_scenario"
    cap = _write_capture(tmp_path, bad)
    with pytest.raises(ValueError, match="known scenario_id"):
        classify_capture(cap, tmp_path / "dossier")


def test_missing_scenario_id_raises(tmp_path) -> None:
    # a capture with NO scenario_id must raise — never default to the laxer static scenario
    # (else an export capture that forgot the field would fail-open to a no-export PASS).
    bad = _green_export_capture()
    del bad["scenario_id"]
    cap = _write_capture(tmp_path, bad)
    with pytest.raises(ValueError, match="known scenario_id"):
        classify_capture(cap, tmp_path / "dossier")
