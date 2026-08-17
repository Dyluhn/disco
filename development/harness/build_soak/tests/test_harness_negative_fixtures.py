"""P1B-LIVE-STABILITY (STAB-1): deterministic NEGATIVE fixtures proving the browser product-
harness classification FAILS CLOSED on every required negative — no SKIP-as-PASS, no oracle
weakening. Each fixture flips exactly ONE field on a known-green dossier and asserts the precise
classification outcome, so a future oracle regression that lets a defect through turns red here.

Coverage (the stability negative matrix):
  INVALID_RUN — missing required slice (browser_ws/lifecycle/preview/shown/verification/cleanup),
                missing provider ledger, missing/unknown scenario_id.
  FAIL        — OpenRouter host in the ledger, provider call after terminal (sidecar), an empty
                PreviewPane (nothing shown), no cleanup (orphan / workspace not released),
                FINISHED without verification truth.
"""

from __future__ import annotations

import json

import pytest
from _eventlog import clean_smoke_log

from harness.build_soak import failure_codes as fc
from harness.product_build import STATIC_SITE_SMOKE, classify_dossier, write_dossier
from harness.product_build.classify_captured import classify_capture

_MINIMAX_LEDGER = [{"host": "api.minimaxi.chat", "model": "MiniMax-M3"}]


def _green_pe() -> dict:
    """A fully-green product-evidence dossier (incl. a clean sidecar slice) — the baseline every
    negative below flips exactly one field of."""
    return {
        "browser_ws": {"connections": 1},
        "lifecycle": {"terminal": "FINISHED", "statuses": ["RUNNING", "FINISHED"]},
        "sidecar": {"stopped_at_terminal": True, "provider_calls_after_terminal": 0},
        "preview": {"owner": "platform", "manual_port": False},
        "shown": {"artifact_shown": True, "preview_shown": True},
        "verification": {"ready_for_verification_called": True, "passed": True},
        "cleanup": {"orphans": 0, "workspace_released": True},
    }


def _classify(tmp_path, *, pe=None, ledger=None) -> dict:
    write_dossier(
        tmp_path,
        product_evidence=pe if pe is not None else _green_pe(),
        provider_records=_MINIMAX_LEDGER if ledger is None else ledger,
        events=clean_smoke_log(),  # full awaiting+approval chain (non-autonomous fixture)
        run_id="neg",
        scenario_id="static_site_smoke",
    )
    return classify_dossier(tmp_path, STATIC_SITE_SMOKE)


def test_baseline_green_passes(tmp_path) -> None:
    # the control: the unflipped dossier PASSes, so each negative below is the ONLY change.
    assert _classify(tmp_path)["status"] == "PASS"


# --- INVALID_RUN: missing required evidence -----------------------------------
@pytest.mark.parametrize(
    "slice_name", ["browser_ws", "lifecycle", "preview", "shown", "verification", "cleanup"]
)
def test_missing_required_slice_is_invalid_run(tmp_path, slice_name) -> None:
    pe = _green_pe()
    del pe[slice_name]
    c = _classify(tmp_path, pe=pe)
    assert c["status"] == "INVALID_RUN" and c["code"] == fc.MISSING_REQUIRED_EVIDENCE, (
        slice_name,
        c,
    )
    assert slice_name in c["facts"].get("missing_slices", []), c


def test_missing_provider_ledger_is_invalid_run(tmp_path) -> None:
    c = _classify(tmp_path, ledger=[])  # require_ledger=True → no provider evidence
    assert c["status"] == "INVALID_RUN" and c["code"] == fc.MISSING_REQUIRED_EVIDENCE, c


def test_missing_scenario_id_is_rejected(tmp_path) -> None:
    cap = tmp_path / "cap.json"
    cap.write_text(
        json.dumps(
            {
                "product_evidence": _green_pe(),
                "provider_records": _MINIMAX_LEDGER,
                "events": clean_smoke_log(),
                "autonomous": False,
            }  # NO scenario_id
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="known scenario_id"):
        classify_capture(str(cap), tmp_path / "d")


def test_unknown_scenario_id_is_rejected(tmp_path) -> None:
    cap = tmp_path / "cap.json"
    cap.write_text(
        json.dumps(
            {
                "product_evidence": _green_pe(),
                "provider_records": _MINIMAX_LEDGER,
                "events": clean_smoke_log(),
                "autonomous": False,
                "scenario_id": "bogus_scenario",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="known scenario_id"):
        classify_capture(str(cap), tmp_path / "d")


# --- FAIL: fail-closed oracles. Each flips the MINIMAL field(s) to trip ONE gate and asserts
#     that gate's exact code (so a fixture can't pass via a different gate). The ledger keeps a
#     VALID model so the OpenRouter case isolates the forbidden-HOST check, not model-mismatch.
def test_openrouter_host_in_ledger_fails(tmp_path) -> None:
    # Isolate the FORBIDDEN-openrouter gate specifically: the host contains BOTH "minimax" (so the
    # require-host check passes) AND "openrouter" (so ONLY the forbid-substr check fires) — a plain
    # "openrouter.ai" host would also trip the missing-"minimax" branch under the same code. Valid
    # model too, so it's neither PROVIDER_WRONG_MODEL nor the required-host miss.
    c = _classify(tmp_path, ledger=[{"host": "api.minimax.openrouter.ai", "model": "MiniMax-M3"}])
    assert c["status"] == "FAIL" and c["code"] == fc.PROVIDER_FORBIDDEN, c
    assert c["facts"].get("forbidden_substr") == "openrouter", c


def test_provider_call_after_terminal_fails(tmp_path) -> None:
    # sole change: a non-zero post-terminal provider-call count (stopped flag left True).
    pe = _green_pe()
    pe["sidecar"] = {"stopped_at_terminal": True, "provider_calls_after_terminal": 2}
    c = _classify(tmp_path, pe=pe)
    assert c["status"] == "FAIL" and c["code"] == fc.SIDECAR_NOT_STOPPED, c


def test_sidecar_not_stopped_fails(tmp_path) -> None:
    # sole change: the sidecar did not stop at terminal (call count left 0).
    pe = _green_pe()
    pe["sidecar"] = {"stopped_at_terminal": False, "provider_calls_after_terminal": 0}
    c = _classify(tmp_path, pe=pe)
    assert c["status"] == "FAIL" and c["code"] == fc.SIDECAR_NOT_STOPPED, c


def test_empty_previewpane_and_nothing_shown_fails(tmp_path) -> None:
    # the ShowToUser gate fails only when NEITHER artifact nor preview was shown (the OR oracle),
    # so both must flip — but the asserted code proves it's specifically the shown gate.
    pe = _green_pe()
    pe["shown"] = {"artifact_shown": False, "preview_shown": False}
    c = _classify(tmp_path, pe=pe)
    assert c["status"] == "FAIL" and c["code"] == fc.ARTIFACT_NOT_SHOWN_TO_USER, c


def test_cleanup_orphan_fails(tmp_path) -> None:
    # sole change: an orphan survived (workspace_released left True).
    pe = _green_pe()
    pe["cleanup"] = {"orphans": 1, "workspace_released": True}
    c = _classify(tmp_path, pe=pe)
    assert c["status"] == "FAIL" and c["code"] == fc.WORKSPACE_NOT_CLEANED, c


def test_cleanup_workspace_not_released_fails(tmp_path) -> None:
    # sole change: the workspace was not released (orphans left 0).
    pe = _green_pe()
    pe["cleanup"] = {"orphans": 0, "workspace_released": False}
    c = _classify(tmp_path, pe=pe)
    assert c["status"] == "FAIL" and c["code"] == fc.WORKSPACE_NOT_CLEANED, c


def test_finished_without_verification_truth_fails(tmp_path) -> None:
    # sole change: the finish-gate verification did not pass.
    pe = _green_pe()
    pe["verification"] = {"ready_for_verification_called": True, "passed": False}
    c = _classify(tmp_path, pe=pe)
    assert c["status"] == "FAIL" and c["code"] == fc.VERIFICATION_GATE_BYPASSED, c
