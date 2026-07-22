"""P1B-LIVE-2: the capture→dossier→classify pipeline proven with SYNTHETIC captured fixtures
(no live model). A green dossier classifies PASS; a broken slice / forbidden provider / missing
required slice / malformed capture all fail correctly — the live run feeds the same writer."""

from __future__ import annotations

from dataclasses import replace

import pytest
from _eventlog import action, clean_smoke_log, msg, observation, plan, status

from harness.build_soak import failure_codes as fc
from harness.product_build import (
    EXPORT_SMOKE,
    STATIC_SITE_SMOKE,
    STATIC_SMOKE_STRICT,
    classify_dossier,
    write_dossier,
)


def _autonomous_log() -> list[dict]:
    """An AUTONOMOUS drive: the agent auto-approves its plan inline, so there is NO
    AWAITING_PLAN_APPROVAL status (the disco-kernel default; matches the live MiniMax run)."""
    # Mirrors the real live MiniMax shape: the plan is auto-approved inline (a RUNNING/
    # plan_approved status, NO preceding AWAITING_PLAN_APPROVAL), then execution + finish.
    return [
        msg(1, "user", "Build a one-page coffee shop site."),
        status(2, "RUNNING"),
        plan(3, steps=2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "file_write", args={"path": "index.html"}, action_id="a5"),
        observation(6, "a5", tool="file_write", success=True),
        status(7, "FINISHED"),
    ]


# --- TEST-ONLY green fixtures (never a production passing default) -------------
_MINIMAX_LEDGER = [{"host": "api.minimaxi.com", "model": "MiniMax-M3", "tokens": 128}]


def _green_pe() -> dict:
    return {
        "browser_ws": {"connections": 1},
        "lifecycle": {"terminal": "FINISHED", "statuses": ["RUNNING", "FINISHED"]},
        "sidecar": {"stopped_at_terminal": True, "provider_calls_after_terminal": 0},
        "preview": {"owner": "platform", "manual_port": False},
        "shown": {"artifact_shown": True, "preview_shown": True},
        "verification": {"ready_for_verification_called": True, "passed": True},
        "export": {"requested": False},
        "cleanup": {"orphans": 0, "workspace_released": True},
    }


def _write(tmp_path, *, pe=None, ledger=None, artifacts=None):
    return write_dossier(
        tmp_path,
        product_evidence=pe if pe is not None else _green_pe(),
        provider_records=_MINIMAX_LEDGER if ledger is None else ledger,
        events=clean_smoke_log(),
        artifacts=artifacts,
        run_id="r1",
        scenario_id="static_site_smoke",
    )


# --- green path ---------------------------------------------------------------
def test_green_dossier_classifies_pass(tmp_path) -> None:
    _write(tmp_path)
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "PASS", c


def test_dossier_has_locked_evidence_files(tmp_path) -> None:
    _write(tmp_path, artifacts={"timeline.md": "# run\nopened build\n"})
    for f in (
        "events.jsonl",
        "product-evidence.json",
        "provider-call-ledger.jsonl",
        "manifest.json",
    ):
        assert (tmp_path / f).is_file(), f
    assert (
        tmp_path / "artifacts" / "timeline.md"
    ).is_file()  # artifacts namespaced under artifacts/


def test_artifact_cannot_clobber_a_core_dossier_file(tmp_path) -> None:
    # an artifact named like a core file must NOT overwrite the strict-validated one
    _write(tmp_path, artifacts={"product-evidence.json": "EVIL", "events.jsonl": "EVIL"})
    import json as _json

    core = _json.loads((tmp_path / "product-evidence.json").read_text())
    assert core["browser_ws"]["connections"] == 1  # the real evidence, not "EVIL"
    assert (tmp_path / "artifacts" / "product-evidence.json").read_text() == "EVIL"
    # the run still classifies normally (hash lock intact)
    assert classify_dossier(tmp_path, STATIC_SITE_SMOKE)["status"] == "PASS"


# --- autonomous drive (the disco-kernel default; the live MiniMax run) ---------
def test_autonomous_dossier_classifies_pass(tmp_path) -> None:
    # An autonomous build emits NO AWAITING_PLAN_APPROVAL; recording autonomous=True on the
    # manifest relaxes the approval-gate link so an otherwise-clean run PASSes. This is the
    # exact gap the live MiniMax-M3 product-harness run hit (PLAN_APPROVED_STATUS_MISSING).
    write_dossier(
        tmp_path,
        product_evidence=_green_pe(),
        provider_records=_MINIMAX_LEDGER,
        events=_autonomous_log(),
        run_id="auto1",
        scenario_id="static_site_smoke",
        autonomous=True,
    )
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "PASS", c


def test_autonomous_log_without_flag_fails_approval_chain(tmp_path) -> None:
    # The SAME autonomous-shaped log WITHOUT the autonomous manifest flag fails the approval
    # ordering — proving the flag (not a fixture accident) is what relaxes the gate.
    write_dossier(
        tmp_path,
        product_evidence=_green_pe(),
        provider_records=_MINIMAX_LEDGER,
        events=_autonomous_log(),
        run_id="auto2",
        scenario_id="static_site_smoke",
        autonomous=False,
    )
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "FAIL" and c["code"] == "PLAN_APPROVED_STATUS_MISSING", c


# --- P1B-LIVE-STABILITY: the STRICT scenario ENFORCES the sidecar slice --------
def test_strict_scenario_green_passes(tmp_path) -> None:
    _write(tmp_path)  # _green_pe carries a clean sidecar slice
    assert classify_dossier(tmp_path, STATIC_SMOKE_STRICT)["status"] == "PASS"


def test_strict_scenario_missing_sidecar_is_invalid(tmp_path) -> None:
    # under STATIC_SITE_SMOKE the sidecar oracle SKIPs (sidecar not required); under the STRICT
    # scenario an absent sidecar slice is INVALID_RUN (it is required evidence).
    pe = _green_pe()
    del pe["sidecar"]
    _write(tmp_path, pe=pe)
    c = classify_dossier(tmp_path, STATIC_SMOKE_STRICT)
    assert c["status"] == "INVALID_RUN" and c["code"] == fc.MISSING_REQUIRED_EVIDENCE, c
    assert "sidecar" in c["facts"]["missing_slices"], c
    # the SAME dossier still PASSes the non-strict scenario (sidecar optional there) — proving the
    # strictness is scenario-scoped, not a global change.
    assert classify_dossier(tmp_path, STATIC_SITE_SMOKE)["status"] == "PASS"


# --- P10: the EXPORT-requiring scenario (export/handoff) ----------------------
def _green_export_pe() -> dict:
    pe = _green_pe()
    pe["export"] = {
        "requested": True,
        "download_present": True,
        "download_bytes": 2048,
        "workspace_match": True,
    }
    return pe


def _write_export(tmp_path, *, pe=None):
    return write_dossier(
        tmp_path,
        product_evidence=pe if pe is not None else _green_export_pe(),
        provider_records=_MINIMAX_LEDGER,
        events=clean_smoke_log(),  # the full awaiting+approval chain (non-autonomous)
        run_id="ex1",
        scenario_id="export_smoke",
    )


def test_export_scenario_green_classifies_pass(tmp_path) -> None:
    _write_export(tmp_path)
    c = classify_dossier(tmp_path, EXPORT_SMOKE)
    assert c["status"] == "PASS", c


def test_export_scenario_no_export_requested_is_invalid(tmp_path) -> None:
    # the export slice is present but requested=False — requires_export ⇒ INVALID_RUN (an export
    # scenario that delivered no export is incomplete, NOT a pass via the oracle's skip).
    pe = _green_export_pe()
    pe["export"] = {"requested": False}
    _write_export(tmp_path, pe=pe)
    c = classify_dossier(tmp_path, EXPORT_SMOKE)
    assert c["status"] == "INVALID_RUN" and c["code"] == fc.MISSING_REQUIRED_EVIDENCE, c


def test_export_scenario_empty_download_fails(tmp_path) -> None:
    # requested but the download is absent / zero bytes — the ExportDownloadOracle FAILS.
    pe = _green_export_pe()
    pe["export"] = {"requested": True, "download_present": False, "download_bytes": 0}
    _write_export(tmp_path, pe=pe)
    c = classify_dossier(tmp_path, EXPORT_SMOKE)
    assert c["status"] == "FAIL" and c["code"] == "EXPORT_DOWNLOAD_MISSING", c


def test_static_scenario_unaffected_by_export_requirement(tmp_path) -> None:
    # the no-export static scenario still PASSes with export.requested=False (no cross-contam).
    _write(tmp_path)  # _green_pe has export={"requested": False}
    assert classify_dossier(tmp_path, STATIC_SITE_SMOKE)["status"] == "PASS"


# --- broken slices / provider -------------------------------------------------
def test_broken_browser_ws_fails(tmp_path) -> None:
    pe = _green_pe()
    pe["browser_ws"]["connections"] = 0
    _write(tmp_path, pe=pe)
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "FAIL" and c["code"] == "BROWSER_WS_NOT_CONNECTED", c


def test_openrouter_provider_record_fails(tmp_path) -> None:
    _write(tmp_path, ledger=[{"host": "openrouter.ai/api", "model": "x"}])
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "FAIL", c  # forbidden provider host


def test_missing_provider_ledger_is_invalid_run(tmp_path) -> None:
    _write(tmp_path, ledger=[])  # require_ledger=True → no provider evidence
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "INVALID_RUN", c


# --- product-harness completeness (no PASS via SKIP) --------------------------
def test_missing_required_slice_is_invalid_not_pass(tmp_path) -> None:
    pe = _green_pe()
    del pe["cleanup"]  # a required slice — its oracle would SKIP, but the run is incomplete
    _write(tmp_path, pe=pe)
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "INVALID_RUN" and c["code"] == fc.MISSING_REQUIRED_EVIDENCE
    assert "cleanup" in c["facts"]["missing_slices"]


def test_no_product_evidence_is_invalid_run(tmp_path) -> None:
    # an empty folder (no dossier) must not classify PASS for a product scenario
    c = classify_dossier(tmp_path, STATIC_SITE_SMOKE)
    assert c["status"] == "INVALID_RUN" and c["code"] == fc.MISSING_REQUIRED_EVIDENCE


# --- write_dossier guards -----------------------------------------------------
def test_malformed_capture_raises_before_writing(tmp_path) -> None:
    pe = _green_pe()
    pe["export"] = {
        "requested": True,
        "download_present": True,
        "download_bytes": True,
    }  # bool, not int
    with pytest.raises(ValueError):
        _write(tmp_path, pe=pe)
    assert not (tmp_path / "product-evidence.json").exists()  # nothing written on preflight failure


def test_unsafe_artifact_path_rejected(tmp_path) -> None:
    for bad in ("../escape.txt", "/etc/passwd"):
        with pytest.raises(ValueError):
            _write(tmp_path / bad.replace("/", "_"), artifacts={bad: "x"})


def test_requires_export_scenario_rejects_unrequested(tmp_path) -> None:
    export_scn = replace(STATIC_SITE_SMOKE, id="export_smoke", requires_export=True)
    _write(tmp_path)  # green pe has export.requested=False
    c = classify_dossier(tmp_path, export_scn)
    assert c["status"] == "INVALID_RUN" and c["code"] == fc.MISSING_REQUIRED_EVIDENCE
