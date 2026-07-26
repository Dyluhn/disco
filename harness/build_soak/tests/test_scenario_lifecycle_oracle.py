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

    result = ScenarioLifecycleOracle().check(events, scenario=scenario, product_evidence=evidence)[
        0
    ]
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
    result = ContextPressureOracle().check(_context_events(compacted=False), scenario=scenario)[0]
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


def _prefixed_context_events(path: str) -> list[dict]:
    """The seed-460000 shape: the agent spells the path with a /workspace/ prefix."""
    return [
        {
            "seq": 1,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_read",
                "call_id": "r1",
                "arguments": {"path": path, "offset": 1, "limit": 40},
            },
        },
        {
            "seq": 2,
            "kind": "observation",
            "source": "system",
            "tool_result": {
                "tool_name": "file_read",
                "call_id": "r1",
                "success": True,
                "content": "[lines 1-40 of 30000]; read more with offset=9996",
            },
        },
        {
            "seq": 3,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_read",
                "call_id": "r2",
                "arguments": {"path": path, "offset": 9996, "limit": 10},
            },
        },
        {
            "seq": 4,
            "kind": "action",
            "source": "agent",
            "tool_call": {
                "tool_name": "file_read",
                "call_id": "r3",
                "arguments": {"path": path, "offset": 19996, "limit": 10},
            },
        },
        {"seq": 5, "kind": "condensation", "source": "system"},
    ]


_CTX_SCN = {
    "assertions": {
        "context_pressure": {
            "path": "catalog.txt",
            "min_distinct_offsets": 3,
            "max_reads_per_offset": 2,
            "require_compaction": True,
        }
    }
}


def test_workspace_prefixed_reads_are_the_same_file() -> None:
    """POSITIVE CONTROL: the exact failure of context seed 460000.

    The agent paged catalog.txt at offsets 1, 9996 and 19996 — three distinct
    offsets, precisely what the assertion asks for — but spelled the path
    `/workspace/catalog.txt`, which the product's own
    `strip_redundant_workspace_prefix` defines as the same file. A raw string
    comparison discarded all three reads and reported `read_count: 0`.
    """
    result = ContextPressureOracle().check(
        _prefixed_context_events("/workspace/catalog.txt"), scenario=_CTX_SCN
    )[0]

    assert result.passed, result.to_dict()
    assert result.facts["read_count"] == 3, result.facts
    assert result.facts["distinct_offsets"] == ["1", "19996", "9996"], result.facts


def test_bare_relative_reads_still_count() -> None:
    """NEGATIVE CONTROL against regression: the original spelling must still work."""
    result = ContextPressureOracle().check(
        _prefixed_context_events("catalog.txt"), scenario=_CTX_SCN
    )[0]

    assert result.passed, result.to_dict()
    assert result.facts["read_count"] == 3, result.facts


def test_a_different_file_is_still_not_the_asserted_one() -> None:
    """NEGATIVE CONTROL: normalising a prefix must not make every path match.

    Paging some other file is not paging catalog.txt, and the oracle must still
    say so. Narrowing what counts as a spelling difference must not widen what
    counts as the file.
    """
    result = ContextPressureOracle().check(
        _prefixed_context_events("/workspace/other.txt"), scenario=_CTX_SCN
    )[0]

    assert result.code == "CONTEXT_PRESSURE_NOT_OBSERVED", result.facts
    assert result.facts["read_count"] == 0, result.facts
