"""HARN-2 tests: the 8 browser product-harness oracles + classify integration."""

from __future__ import annotations

from _eventlog import clean_smoke_log

from harness.build_soak.classify import classify
from harness.build_soak.oracles.browser_evidence import (
    BrowserWSOracle,
    CleanupOracle,
    ExportDownloadOracle,
    LifecycleOracle,
    PreviewOwnershipOracle,
    ShowToUserOracle,
    SidecarStopOracle,
    VerificationGateOracle,
)


def _green_evidence() -> dict:
    return {
        "browser_ws": {"connections": 1, "closes": [{"code": 1000, "reason": "done"}]},
        "lifecycle": {"statuses": ["RUNNING", "FINISHED"], "terminal": "FINISHED"},
        "sidecar": {"stopped_at_terminal": True, "provider_calls_after_terminal": 0},
        "preview": {
            "owner": "platform",
            "manual_port": False,
            "shown_to_user": True,
            "url": "http://x",
        },
        "shown": {"artifact_shown": True, "preview_shown": True},
        "verification": {"ready_for_verification_called": True, "passed": True},
        "export": {"requested": True, "download_present": True, "download_bytes": 4096},
        "cleanup": {"orphans": 0, "workspace_released": True},
    }


# --- absent evidence → every oracle SKIPs (headless run unaffected) ------------
def test_all_skip_without_evidence():
    for cls in (
        BrowserWSOracle,
        LifecycleOracle,
        SidecarStopOracle,
        PreviewOwnershipOracle,
        ShowToUserOracle,
        VerificationGateOracle,
        ExportDownloadOracle,
        CleanupOracle,
    ):
        r = cls().check(product_evidence=None)
        assert r[0].skipped, cls.__name__


# --- green evidence → every oracle PASSes -------------------------------------
def test_all_pass_on_green_evidence():
    ev = _green_evidence()
    for cls in (
        BrowserWSOracle,
        LifecycleOracle,
        SidecarStopOracle,
        PreviewOwnershipOracle,
        ShowToUserOracle,
        VerificationGateOracle,
        ExportDownloadOracle,
        CleanupOracle,
    ):
        r = cls().check(product_evidence=ev)
        assert r[0].passed, (cls.__name__, r[0].to_dict())


# --- each oracle's concrete violation -----------------------------------------
def test_browser_ws_not_connected_fails():
    ev = _green_evidence()
    ev["browser_ws"]["connections"] = 0
    assert BrowserWSOracle().check(product_evidence=ev)[0].code == "BROWSER_WS_NOT_CONNECTED"


def test_lifecycle_no_clean_terminal_fails():
    ev = _green_evidence()
    ev["lifecycle"]["terminal"] = "PAUSED"
    assert LifecycleOracle().check(product_evidence=ev)[0].code == "LIFECYCLE_SEQUENCE_INVALID"


def test_sidecar_calls_after_terminal_fails():
    ev = _green_evidence()
    ev["sidecar"]["provider_calls_after_terminal"] = 3
    assert SidecarStopOracle().check(product_evidence=ev)[0].code == "SIDECAR_NOT_STOPPED"


def test_preview_model_owned_fails():
    ev = _green_evidence()
    ev["preview"]["owner"] = "model"
    assert (
        PreviewOwnershipOracle().check(product_evidence=ev)[0].code == "PREVIEW_OWNERSHIP_VIOLATION"
    )


def test_preview_manual_port_fails():
    ev = _green_evidence()
    ev["preview"]["manual_port"] = True
    assert (
        PreviewOwnershipOracle().check(product_evidence=ev)[0].code == "PREVIEW_OWNERSHIP_VIOLATION"
    )


def test_not_shown_to_user_fails():
    ev = _green_evidence()
    ev["shown"] = {"artifact_shown": False, "preview_shown": False}
    assert ShowToUserOracle().check(product_evidence=ev)[0].code == "ARTIFACT_NOT_SHOWN_TO_USER"


def test_verification_bypassed_fails():
    ev = _green_evidence()
    ev["verification"]["ready_for_verification_called"] = False
    assert (
        VerificationGateOracle().check(product_evidence=ev)[0].code == "VERIFICATION_GATE_BYPASSED"
    )


def test_export_requested_but_no_download_fails():
    ev = _green_evidence()
    ev["export"] = {"requested": True, "download_present": False, "download_bytes": 0}
    assert ExportDownloadOracle().check(product_evidence=ev)[0].code == "EXPORT_DOWNLOAD_MISSING"


def test_export_not_requested_skips():
    ev = _green_evidence()
    ev["export"] = {"requested": False}
    assert ExportDownloadOracle().check(product_evidence=ev)[0].skipped


def test_cleanup_orphans_fail():
    ev = _green_evidence()
    ev["cleanup"]["orphans"] = 2
    assert CleanupOracle().check(product_evidence=ev)[0].code == "WORKSPACE_NOT_CLEANED"


def test_cleanup_volume_orphans_fail():
    ev = _green_evidence()
    ev["cleanup"] = {
        "orphans": 1,
        "workspace_released": False,
        "scope": "conversation",
        "container_orphans": 0,
        "volume_orphans": 1,
        "volume_scope": "conversation",
    }
    r = CleanupOracle().check(product_evidence=ev)[0]
    assert r.code == "WORKSPACE_NOT_CLEANED"
    assert r.facts["volume_orphans"] == 1


def test_cleanup_back_compat_without_volume_fields_still_passes():
    ev = _green_evidence()
    ev["cleanup"] = {"orphans": 0, "workspace_released": True}
    assert CleanupOracle().check(product_evidence=ev)[0].passed


# --- classify integration -----------------------------------------------------
def _scn():
    return {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}


def test_classify_unaffected_without_product_evidence():
    # headless run: product_evidence None → all browser oracles SKIP → still PASS
    assert classify(clean_smoke_log(), scenario=_scn())["status"] == "PASS"


def test_classify_passes_with_green_product_evidence():
    c = classify(clean_smoke_log(), scenario=_scn(), product_evidence=_green_evidence())
    assert c["status"] == "PASS", c


def test_classify_fails_on_browser_ws_not_connected():
    ev = _green_evidence()
    ev["browser_ws"]["connections"] = 0
    c = classify(clean_smoke_log(), scenario=_scn(), product_evidence=ev)
    assert c["status"] == "FAIL"
    assert c["code"] == "BROWSER_WS_NOT_CONNECTED"
    assert c["severity"] == "P0"


# --- fail-closed on malformed / partial evidence (no crash, no false-PASS) -----
def test_malformed_numeric_is_fail_closed_not_crash():
    ev = _green_evidence()
    ev["browser_ws"]["connections"] = "n/a"
    r = BrowserWSOracle().check(product_evidence=ev)  # must not raise
    assert r[0].code == "BROWSER_WS_NOT_CONNECTED"


def test_malformed_sidecar_count_fail_closed():
    ev = _green_evidence()
    ev["sidecar"]["provider_calls_after_terminal"] = "lots"
    assert SidecarStopOracle().check(product_evidence=ev)[0].code == "SIDECAR_NOT_STOPPED"


def test_sidecar_missing_call_count_is_invalid_run():
    ev = _green_evidence()
    del ev["sidecar"]["provider_calls_after_terminal"]

    r = SidecarStopOracle().check(product_evidence=ev)[0]
    assert r.code == "MISSING_REQUIRED_EVIDENCE"
    c = classify(clean_smoke_log(), scenario=_scn(), product_evidence=ev)
    assert c["status"] == "INVALID_RUN"
    assert c["code"] == "MISSING_REQUIRED_EVIDENCE"


def test_preview_missing_owner_is_fail_closed():
    ev = _green_evidence()
    ev["preview"] = {"manual_port": False}  # no owner field
    assert (
        PreviewOwnershipOracle().check(product_evidence=ev)[0].code == "PREVIEW_OWNERSHIP_VIOLATION"
    )


def test_cleanup_missing_release_field_is_fail_closed():
    ev = _green_evidence()
    ev["cleanup"] = {"orphans": 0}  # no workspace_released
    assert CleanupOracle().check(product_evidence=ev)[0].code == "WORKSPACE_NOT_CLEANED"


def test_cleanup_missing_orphans_is_invalid_run():
    ev = _green_evidence()
    ev["cleanup"] = {"workspace_released": True}

    r = CleanupOracle().check(product_evidence=ev)[0]
    assert r.code == "MISSING_REQUIRED_EVIDENCE"
    c = classify(clean_smoke_log(), scenario=_scn(), product_evidence=ev)
    assert c["status"] == "INVALID_RUN"
    assert c["code"] == "MISSING_REQUIRED_EVIDENCE"


def test_verification_missing_passed_is_fail_closed():
    ev = _green_evidence()
    ev["verification"] = {"ready_for_verification_called": True}
    assert (
        VerificationGateOracle().check(product_evidence=ev)[0].code == "VERIFICATION_GATE_BYPASSED"
    )


def test_export_malformed_bytes_fail_closed():
    ev = _green_evidence()
    ev["export"] = {"requested": True, "download_present": True, "download_bytes": "big"}
    assert ExportDownloadOracle().check(product_evidence=ev)[0].code == "EXPORT_DOWNLOAD_MISSING"


# --- classify_run_folder reads product-evidence.json --------------------------
def test_run_folder_reads_product_evidence_green(tmp_path):
    import json as _json

    from harness.build_soak.classify import classify_run_folder

    (tmp_path / "events.jsonl").write_text(
        "\n".join(_json.dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    (tmp_path / "product-evidence.json").write_text(
        _json.dumps(_green_evidence()), encoding="utf-8"
    )
    c = classify_run_folder(tmp_path, scenario=_scn())
    assert c["status"] == "PASS", c


def test_run_folder_product_evidence_violation_fails(tmp_path):
    import json as _json

    from harness.build_soak.classify import classify_run_folder

    ev = _green_evidence()
    ev["sidecar"]["provider_calls_after_terminal"] = 5
    (tmp_path / "events.jsonl").write_text(
        "\n".join(_json.dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    (tmp_path / "product-evidence.json").write_text(_json.dumps(ev), encoding="utf-8")
    c = classify_run_folder(tmp_path, scenario=_scn())
    assert c["status"] == "FAIL" and c["code"] == "SIDECAR_NOT_STOPPED"


def test_run_folder_corrupt_product_evidence_does_not_crash(tmp_path):
    from harness.build_soak.classify import classify_run_folder

    (tmp_path / "events.jsonl").write_text(
        "\n".join(__import__("json").dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    (tmp_path / "product-evidence.json").write_text("{ not valid json", encoding="utf-8")
    c = classify_run_folder(tmp_path, scenario=_scn())  # corrupt → ignored (None) → oracles SKIP
    assert c["status"] == "PASS", c
