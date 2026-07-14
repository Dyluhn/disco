"""HARN-1a tests: ProviderLedgerOracle (MiniMax-only / no-OpenRouter /
zero-calls-after-terminal) + the relay-log parser."""

from __future__ import annotations

import json

from _eventlog import clean_smoke_log

from harness.build_soak.classify import classify
from harness.build_soak.oracles.provider_ledger import ProviderLedgerOracle
from harness.build_soak.provider_ledger import (
    parse_relay_log,
    parse_relay_log_lines,
    record_applies_to_conversation,
)
from harness.product_build.minimax_relay import relay_log_record as _rel5b_relay_record

_PROV = {
    "id": "minimax_soak",
    "assertions": {
        "provider": {"require_host_substr": "minimax", "model": "MiniMax-M3"},
    },
}


def _run(ledger, scenario=_PROV):
    return ProviderLedgerOracle().check([], scenario=scenario, provider_ledger=ledger)


def _run_for(ledger, conversation_id: str, scenario=_PROV):
    return ProviderLedgerOracle().check(
        [], scenario=scenario, provider_ledger=ledger, conversation_id=conversation_id
    )


# --- opt-in / skip ------------------------------------------------------------
def test_skips_when_no_provider_assertion():
    r = ProviderLedgerOracle().check([], scenario={"id": "x", "assertions": {}}, provider_ledger=[])
    assert r[0].skipped


def test_absent_ledger_fails_closed_by_default():
    # provider asserted + ledger absent (None) + require_ledger default True → fail-closed
    r = _run(None)
    assert r[0].failed
    assert r[0].code == "MISSING_REQUIRED_EVIDENCE"
    assert r[0].facts["captured"] is False


def test_absent_ledger_skips_when_require_ledger_false():
    scenario = {
        "id": "x",
        "assertions": {"provider": {"model": "MiniMax-M3", "require_ledger": False}},
    }
    r = _run(None, scenario=scenario)
    assert r[0].skipped


def test_empty_ledger_fails_closed_distinct_from_absent():
    r = _run([])
    assert r[0].failed
    assert r[0].code == "MISSING_REQUIRED_EVIDENCE"
    assert r[0].facts["captured"] is True and r[0].facts["records"] == 0


def test_malformed_record_fails_closed():
    # a hostless / non-dict record is corrupt evidence → fail-closed, never under-report
    r = _run([{"host": "api.minimaxi.com", "model": "MiniMax-M3"}, {"__malformed__": "junk line"}])
    assert r[0].failed
    assert r[0].code == "MISSING_REQUIRED_EVIDENCE"
    assert r[0].facts["bad_index"] == 1


# --- pass ---------------------------------------------------------------------
def test_passes_all_minimax_calls():
    ledger = [
        {"host": "api.minimaxi.com", "model": "MiniMax-M3", "after_terminal": False},
        {"host": "api.minimaxi.com", "model": "MiniMax-M3", "after_terminal": False},
    ]
    r = _run(ledger)
    assert r[0].passed, r[0].to_dict()
    assert r[0].facts["calls"] == 2


# --- the HARD constraint: no OpenRouter --------------------------------------
def test_openrouter_call_fails():
    ledger = [
        {"host": "api.minimaxi.com", "model": "MiniMax-M3"},
        {"host": "openrouter.ai", "model": "MiniMax-M3"},
    ]
    r = _run(ledger)
    assert r[0].code == "PROVIDER_FORBIDDEN"
    assert "openrouter" in r[0].facts["host"]


def test_non_required_host_fails():
    ledger = [{"host": "api.openai.com", "model": "MiniMax-M3"}]
    r = _run(ledger)
    assert r[0].code == "PROVIDER_FORBIDDEN"


def test_wrong_model_fails():
    ledger = [{"host": "api.minimaxi.com", "model": "gpt-4o"}]
    r = _run(ledger)
    assert r[0].code == "PROVIDER_WRONG_MODEL"
    assert r[0].facts["expected"] == "MiniMax-M3"


def test_call_after_terminal_fails():
    ledger = [
        {"host": "api.minimaxi.com", "model": "MiniMax-M3", "after_terminal": False},
        {"host": "api.minimaxi.com", "model": "MiniMax-M3", "after_terminal": True},
    ]
    r = _run(ledger)
    assert r[0].code == "PROVIDER_CALL_AFTER_TERMINAL"


def test_conversation_scoped_after_terminal_ignores_other_conversation():
    ledger = [
        {
            "host": "api.minimaxi.com",
            "model": "MiniMax-M3",
            "after_terminal": False,
            "conversation_id": "conv_terminal",
        },
        {
            "host": "api.minimaxi.com",
            "model": "MiniMax-M3",
            "after_terminal": True,
            "conversation_id": "conv_overlap",
        },
    ]
    r = _run_for(ledger, "conv_terminal")
    assert r[0].passed, r[0].to_dict()
    assert r[0].facts["calls"] == 1


def test_conversation_scoped_after_terminal_same_conversation_still_fails():
    ledger = [
        {
            "host": "api.minimaxi.com",
            "model": "MiniMax-M3",
            "after_terminal": False,
            "conversation_id": "conv_terminal",
        },
        {
            "host": "api.minimaxi.com",
            "model": "MiniMax-M3",
            "after_terminal": True,
            "conversation_id": "conv_terminal",
        },
    ]
    r = _run_for(ledger, "conv_terminal")
    assert r[0].code == "PROVIDER_CALL_AFTER_TERMINAL"


def test_unscoped_after_terminal_legacy_record_still_fails_closed():
    ledger = [
        {
            "host": "api.minimaxi.com",
            "model": "MiniMax-M3",
            "after_terminal": True,
            "conversation_id": None,
        }
    ]
    r = _run_for(ledger, "conv_terminal")
    assert r[0].code == "PROVIDER_CALL_AFTER_TERMINAL"


def test_default_forbid_is_openrouter_even_without_require_host():
    scenario = {"id": "x", "assertions": {"provider": {"model": "MiniMax-M3"}}}
    r = _run([{"host": "openrouter.ai", "model": "MiniMax-M3"}], scenario=scenario)
    assert r[0].code == "PROVIDER_FORBIDDEN"


# --- relay-log parser ---------------------------------------------------------
def test_parser_jsonl_records():
    text = "\n".join(
        [
            (
                '{"ts": "t1", "host": "api.minimaxi.com", '
                '"model": "MiniMax-M3", "conversation_id": "conv_parse"}'
            ),
            '{"url": "https://openrouter.ai/api/v1/chat", "model": "x"}',
        ]
    )
    recs = parse_relay_log(text)
    assert len(recs) == 2
    assert recs[0]["host"] == "api.minimaxi.com"
    assert recs[0]["conversation_id"] == "conv_parse"
    assert recs[1]["host"] == "openrouter.ai"  # host derived from url


def test_parser_loose_lines():
    lines = [
        'POST https://api.minimaxi.com/v1/text/chatcompletion model="MiniMax-M3" 200',
        "noise line with no url",
        "GET https://openrouter.ai/api model=foo",
    ]
    recs = parse_relay_log_lines(lines)
    assert [r["host"] for r in recs] == ["api.minimaxi.com", "openrouter.ai"]
    assert recs[0]["model"] == "MiniMax-M3"


def test_parser_skips_hostless_lines():
    assert parse_relay_log_lines(["", "just text", "  "]) == []


# --- end-to-end through classify() --------------------------------------------
def test_classify_fails_on_openrouter_in_ledger():
    scenario = {
        "id": "s",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "provider": {"require_host_substr": "minimax", "model": "MiniMax-M3"},
        },
    }
    ledger = [{"host": "openrouter.ai", "model": "MiniMax-M3"}]
    c = classify(clean_smoke_log(), scenario=scenario, provider_ledger=ledger)
    assert c["status"] == "FAIL"
    assert c["code"] == "PROVIDER_FORBIDDEN"
    assert c["severity"] == "P0"


def test_classify_passes_with_minimax_ledger():
    scenario = {
        "id": "s",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "provider": {"require_host_substr": "minimax", "model": "MiniMax-M3"},
        },
    }
    ledger = [{"host": "api.minimaxi.com", "model": "MiniMax-M3"}]
    c = classify(clean_smoke_log(), scenario=scenario, provider_ledger=ledger)
    assert c["status"] == "PASS", c


def test_classify_required_ledger_absent_is_invalid_run():
    # provider asserted, require_ledger default True, no ledger → fail-closed INVALID_RUN
    scenario = {
        "id": "s",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "provider": {"require_host_substr": "minimax"},
        },
    }
    c = classify(clean_smoke_log(), scenario=scenario, provider_ledger=None)
    assert c["status"] == "INVALID_RUN", c
    assert c["code"] == "MISSING_REQUIRED_EVIDENCE"


def test_classify_provider_opt_out_passes_without_ledger():
    scenario = {
        "id": "s",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "provider": {"require_host_substr": "minimax", "require_ledger": False},
        },
    }
    c = classify(clean_smoke_log(), scenario=scenario, provider_ledger=None)
    assert c["status"] == "PASS", c  # opted out → SKIP → run passes on the rest


# --- classify_run_folder ledger ingestion (tolerant, no crash, no silent drop) ----
def test_run_folder_reads_ledger_and_enforces(tmp_path):
    from harness.build_soak.classify import classify_run_folder

    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    (tmp_path / "provider-call-ledger.jsonl").write_text(
        '{"host": "openrouter.ai", "model": "MiniMax-M3"}\n', encoding="utf-8"
    )
    scenario = {
        "id": "s",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "provider": {"require_host_substr": "minimax", "model": "MiniMax-M3"},
        },
    }
    c = classify_run_folder(tmp_path, scenario=scenario)
    assert c["status"] == "FAIL"
    assert c["code"] == "PROVIDER_FORBIDDEN"


def test_run_folder_malformed_ledger_line_is_evidence_gap_not_crash(tmp_path):
    from harness.build_soak.classify import classify_run_folder

    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in clean_smoke_log()), encoding="utf-8"
    )
    # a valid minimax call followed by a corrupt (non-JSON) line — must NOT crash and
    # must NOT silently drop the bad line (which could hide a forbidden call)
    (tmp_path / "provider-call-ledger.jsonl").write_text(
        '{"host": "api.minimaxi.com", "model": "MiniMax-M3"}\n{ this is not json\n',
        encoding="utf-8",
    )
    scenario = {
        "id": "s",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "provider": {"require_host_substr": "minimax", "model": "MiniMax-M3"},
        },
    }
    c = classify_run_folder(tmp_path, scenario=scenario)
    assert c["status"] == "INVALID_RUN"  # corrupt evidence → fail-closed, no crash
    assert c["code"] == "MISSING_REQUIRED_EVIDENCE"


# --- parser edge cases --------------------------------------------------------
def test_parser_empty_and_blank_input():
    assert parse_relay_log("") == []
    assert parse_relay_log("\n\n  \n") == []


def test_parser_invalid_json_with_url_falls_back_to_regex():
    # a non-JSON line that still carries a URL must yield a record (not be dropped)
    recs = parse_relay_log_lines(["{bad json but https://openrouter.ai/x model=foo"])
    assert len(recs) == 1 and recs[0]["host"] == "openrouter.ai"


def test_parser_after_terminal_defaults_false():
    recs = parse_relay_log('{"host": "api.minimaxi.com"}')
    assert recs[0]["after_terminal"] is False
    assert recs[0]["model"] == ""


# --- [REL-5b] exclude benign post-terminal auto-title from the runaway oracle ---


def test_rel5b_relay_record_marks_has_tools() -> None:
    b = _rel5b_relay_record("https://api.minimaxi.chat/v1/chat/completions", "m", has_tools=True)
    s = _rel5b_relay_record("https://api.minimaxi.chat/v1/chat/completions", "m", has_tools=False)
    assert b["has_tools"] is True and s["has_tools"] is False


def test_rel5b_parse_preserves_has_tools_default_true_failclosed() -> None:
    import json as _json

    lines = [
        _json.dumps({"host": "api.minimaxi.chat", "model": "m", "ts": 1.0, "has_tools": True}),
        _json.dumps({"host": "api.minimaxi.chat", "model": "m", "ts": 2.0, "has_tools": False}),
        _json.dumps(
            {"host": "api.minimaxi.chat", "model": "m", "ts": 3.0}
        ),  # unmarked → fail-closed True
    ]
    recs = parse_relay_log("\n".join(lines))
    assert [r["has_tools"] for r in recs] == [True, False, True]


def test_rel5b_after_terminal_counts_only_build_driver_calls() -> None:
    term = 100.0
    recs = [
        {"ts": 101.0, "has_tools": False},  # SUMMARIZER auto-title after terminal — EXCLUDED
        {"ts": 102.0, "has_tools": True},  # a real DRIVER runaway after terminal — FLAGGED
        {"ts": 99.0, "has_tools": True},  # before terminal — not counted
    ]
    ts_recs = [(float(r["ts"]), bool(r.get("has_tools", True))) for r in recs]
    assert sum(1 for t, ht in ts_recs if t > term and ht) == 1


def test_provider_ledger_conversation_filter_mixed_records() -> None:
    assert record_applies_to_conversation({"conversation_id": "conv_a"}, "conv_a")
    assert not record_applies_to_conversation({"conversation_id": "conv_b"}, "conv_a")
    assert record_applies_to_conversation({"conversation_id": None}, "conv_a")
    assert record_applies_to_conversation({}, "conv_a")
