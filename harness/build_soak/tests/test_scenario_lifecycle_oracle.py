from __future__ import annotations

from harness.build_soak.oracles.scenario_lifecycle import (
    ContextPressureOracle,
    ScenarioLifecycleOracle,
)


def _status(seq: int, value: str) -> dict:
    return {"seq": seq, "kind": "status", "source": "system", "status": value}


def test_import_pause_restart_and_rollback_require_causal_evidence() -> None:
    scenario = {
        "id": "lifecycle",
        "import_fixture": {"filename": "seed.zip", "files": {"README.md": "seed"}},
        "lifecycle": {
            "pause_resume_at": "after_first_file_write",
            "restart_after_terminal": True,
            "restore_version": "oldest",
        },
    }
    events = [
        _status(1, "RUNNING"),
        _status(2, "PAUSED"),
        _status(3, "RUNNING"),
        {"seq": 4, "kind": "workspace_restored", "source": "system"},
        _status(5, "FINISHED"),
    ]
    evidence = {
        "import": {"accepted": True, "file_count": 1, "bytes": 4},
        "pause_resume": {"ok": True},
        "restart": {
            "ok": True,
            "before_status": "FINISHED",
            "after_status": "FINISHED",
            "before_digest": "a" * 64,
            "after_digest": "a" * 64,
        },
        "rollback": {"ok": True, "restored": 1, "new_version": 3},
    }

    result = ScenarioLifecycleOracle().check(
        events, scenario=scenario, product_evidence=evidence
    )[0]
    assert result.passed, result.to_dict()


def test_restart_digest_mismatch_fails_lifecycle() -> None:
    result = ScenarioLifecycleOracle().check(
        [_status(1, "FINISHED")],
        scenario={"id": "restart", "lifecycle": {"restart_after_terminal": True}},
        product_evidence={
            "restart": {
                "ok": False,
                "before_status": "FINISHED",
                "after_status": "FINISHED",
                "before_digest": "a" * 64,
                "after_digest": "b" * 64,
            }
        },
    )[0]
    assert result.code == "LIFECYCLE_SEQUENCE_INVALID"
    assert result.facts["failed_actions"] == ["restart"]


def test_pause_evidence_without_durable_paused_transition_fails() -> None:
    result = ScenarioLifecycleOracle().check(
        [_status(1, "RUNNING"), _status(2, "FINISHED")],
        scenario={
            "id": "pause",
            "lifecycle": {"pause_resume_at": "after_first_file_write"},
        },
        product_evidence={"pause_resume": {"ok": True}},
    )[0]
    assert result.code == "LIFECYCLE_SEQUENCE_INVALID"


def _context_events(*, compacted: bool = True) -> list[dict]:
    events = [
        {
            "seq": 1,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_read",
                "call_id": "r1",
                "arguments": {"path": "catalog.txt"},
            },
        },
        {
            "seq": 2,
            "kind": "observation",
            "source": "environment",
            "tool_result": {
                "tool_name": "file_read",
                "call_id": "r1",
                "success": True,
                "content": "[lines 1-200 of 10000]; read more with offset=201",
            },
        },
        {
            "seq": 3,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_read",
                "call_id": "r2",
                "arguments": {"path": "catalog.txt", "offset": 201},
            },
        },
    ]
    if compacted:
        events.append({"seq": 4, "kind": "condensation", "source": "system"})
    return events


def test_context_pressure_requires_distinct_ranges_hint_and_compaction() -> None:
    scenario = {
        "assertions": {
            "context_pressure": {
                "path": "catalog.txt",
                "min_distinct_offsets": 2,
                "max_reads_per_offset": 2,
                "require_compaction": True,
            }
        }
    }
    result = ContextPressureOracle().check(_context_events(), scenario=scenario)[0]
    assert result.passed, result.to_dict()
    assert result.facts["distinct_offsets"] == ["0", "201"]


def test_context_pressure_missing_compaction_fails() -> None:
    scenario = {
        "assertions": {
            "context_pressure": {
                "path": "catalog.txt",
                "min_distinct_offsets": 2,
                "require_compaction": True,
            }
        }
    }
    result = ContextPressureOracle().check(
        _context_events(compacted=False), scenario=scenario
    )[0]
    assert result.code == "CONTEXT_PRESSURE_NOT_OBSERVED"


def test_context_pressure_rejects_silent_reread_churn() -> None:
    events = _context_events()
    events.insert(2, {**events[0], "seq": 21})
    events.insert(3, {**events[1], "seq": 22})

    result = ContextPressureOracle().check(
        events,
        scenario={
            "assertions": {
                "context_pressure": {
                    "path": "catalog.txt",
                    "min_distinct_offsets": 2,
                    "max_reads_per_offset": 1,
                    "require_compaction": True,
                }
            }
        },
    )[0]

    assert result.code == "CONTEXT_PRESSURE_NOT_OBSERVED"
    assert result.facts["offset_counts"]["0"] == 2
