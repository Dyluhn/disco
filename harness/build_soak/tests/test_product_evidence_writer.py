"""HARN-1b (evidence side) tests: the validated product-evidence writer closes the
loop with the HARN-2 oracles (write → classify_run_folder → verdict)."""

from __future__ import annotations

import json

import pytest
from _eventlog import clean_smoke_log

from harness.build_soak.classify import classify_run_folder
from harness.build_soak.product_evidence import (
    validate_product_evidence,
    write_product_evidence,
    write_provider_ledger,
)


def _green():
    return {
        "browser_ws": {"connections": 1},
        "lifecycle": {"terminal": "FINISHED"},
        "sidecar": {"stopped_at_terminal": True, "provider_calls_after_terminal": 0},
        "preview": {"owner": "platform", "manual_port": False},
        "shown": {"artifact_shown": True, "preview_shown": True},
        "verification": {"ready_for_verification_called": True, "passed": True},
        "export": {"requested": False},
        "cleanup": {"orphans": 0, "workspace_released": True},
    }


def _scn():
    return {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}


# --- validation ---------------------------------------------------------------
def test_green_evidence_validates_clean():
    assert validate_product_evidence(_green()) == []


def test_bool_where_int_is_a_problem():
    ev = _green(); ev["browser_ws"]["connections"] = True
    probs = validate_product_evidence(ev)
    assert any("browser_ws.connections" in p and "bool" in p for p in probs)


def test_wrong_type_is_a_problem():
    ev = _green(); ev["sidecar"]["provider_calls_after_terminal"] = "n/a"
    probs = validate_product_evidence(ev)
    assert any("sidecar.provider_calls_after_terminal" in p for p in probs)


def test_non_dict_slice_is_a_problem():
    ev = _green(); ev["preview"] = ["not", "a", "dict"]
    assert any("preview: not a dict" == p for p in validate_product_evidence(ev))


def test_unknown_top_level_key_allowed():
    ev = _green(); ev["future_slice"] = {"anything": 1}
    assert validate_product_evidence(ev) == []


# --- strict write refuses malformed evidence ----------------------------------
def test_strict_write_raises_on_malformed(tmp_path):
    ev = _green(); ev["browser_ws"]["connections"] = "lots"
    with pytest.raises(ValueError):
        write_product_evidence(tmp_path, ev)
    assert not (tmp_path / "product-evidence.json").exists()  # nothing written


def test_non_strict_write_persists_anyway(tmp_path):
    ev = _green(); ev["browser_ws"]["connections"] = "lots"
    p = write_product_evidence(tmp_path, ev, strict=False)
    assert p.exists()


# --- the loop: writer → classify_run_folder → oracle verdict ------------------
def test_written_green_evidence_classifies_pass(tmp_path):
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    write_product_evidence(tmp_path, _green())
    assert classify_run_folder(tmp_path, scenario=_scn())["status"] == "PASS"


def test_written_violation_classifies_fail(tmp_path):
    ev = _green(); ev["preview"]["owner"] = "model"
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    write_product_evidence(tmp_path, ev)
    c = classify_run_folder(tmp_path, scenario=_scn())
    assert c["status"] == "FAIL" and c["code"] == "PREVIEW_OWNERSHIP_VIOLATION"


# --- provider ledger writer round-trips through the ProviderLedgerOracle -------
def test_provider_ledger_writer_round_trip(tmp_path):
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    write_provider_ledger(tmp_path, [{"host": "openrouter.ai", "model": "MiniMax-M3"}])
    scenario = {
        "id": "s",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "provider": {"require_host_substr": "minimax", "model": "MiniMax-M3"},
        },
    }
    c = classify_run_folder(tmp_path, scenario=scenario)
    assert c["status"] == "FAIL" and c["code"] == "PROVIDER_FORBIDDEN"


# --- optional hardening (Codex): non-dict root + remaining int fields ---------
def test_non_dict_root_is_a_problem():
    assert validate_product_evidence(["not", "a", "dict"]) == ["product_evidence is not a dict"]  # type: ignore[arg-type]


def test_all_int_fields_reject_bad_types():
    for slice_name, field in (
        ("browser_ws", "connections"),
        ("sidecar", "provider_calls_after_terminal"),
        ("cleanup", "orphans"),
    ):
        ev = _green()
        ev[slice_name][field] = "x"
        assert any(f"{slice_name}.{field}" in p for p in validate_product_evidence(ev))
        ev2 = _green()
        ev2[slice_name][field] = True  # bool where int
        assert any(f"{slice_name}.{field}" in p and "bool" in p for p in validate_product_evidence(ev2))
