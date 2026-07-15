"""Fail-closed adjudicator pins (codex P0 false-negative regressions).

Three P0 false-negatives where the adjudicator would have classified a REAL failure
as PASS. Each is pinned here so it can never silently regress:

  P0 #1 — a real SQLite row {seq,kind,source,payload(JSON)} had its payload DROPPED
          by the normalizer, hiding detail/tool_call/revision from every predicate,
          so WRITE_TOOL_ATTEMPTED_IN_PLANNING / APPROVE_PLAN_NO_EXECUTION /
          NO_REPLAN_AFTER_REVISION all classified PASS in row shape.
  P0 #2 — a plan that reached FINISHED with NO plan_approved status PASSED (the
          approval chain was only checked when an approval already existed). A
          follow-up re-review tightened this to the FULL ordered chain:
          PlanEvent < AWAITING_PLAN_APPROVAL < RUNNING/plan_approved < action — an
          approval with no preceding AWAITING gate (interactive) is a forged chain.
  P0 #3 — a scenario REQUIRING tool-scope evidence with that evidence missing was
          SKIPped into PASS instead of INVALID_RUN.
"""

from __future__ import annotations

from _eventlog import action, awaiting, clean_smoke_log, msg, observation, plan, status, to_db_rows

from harness.build_soak.classify import classify

_PLAN_SCN = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}


def _both_shapes(events):
    """(full-dict classification, db-row classification) for the same events."""
    return classify(events, scenario=_PLAN_SCN), classify(to_db_rows(events), scenario=_PLAN_SCN)


def _strip_volatile(c):
    return {k: v for k, v in c.items() if k not in ("run_id", "conversation_id")}


# ---- P0 #1 — row-shape parity, and the 3 named failures CODED in row shape ----


def _wrong_tool_in_planning_log():
    return [
        msg(1, "user", "build"),
        action(2, "file_write", args={"path": "index.html", "content": "x"}, action_id="a2"),
        observation(3, "a2", tool="file_write"),
        plan(4, revision=1),
        status(5, "AWAITING_PLAN_APPROVAL", "evt_4"),
    ]


def _approve_no_execution_log():
    return [
        msg(1, "user", "build"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        status(5, "FINISHED"),
    ]


def _no_replan_log():
    return [
        msg(1, "user", "build"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="a5"),
        observation(6, "a5"),
        status(7, "FINISHED"),
        msg(8, "user", "also add a contact page"),
        action(9, "file_write", args={"path": "c.html", "content": "x"}, action_id="a9"),
        observation(10, "a9", tool="file_write"),
        status(11, "FINISHED"),
    ]


def test_row_shape_classifies_identically_clean():
    from _eventlog import clean_smoke_log

    full, row = _both_shapes(clean_smoke_log())
    assert full["status"] == "PASS"
    assert _strip_volatile(full) == _strip_volatile(row)


def test_write_in_planning_coded_in_both_shapes():
    full, row = _both_shapes(_wrong_tool_in_planning_log())
    assert full["code"] == "WRITE_TOOL_ATTEMPTED_IN_PLANNING"
    assert row["code"] == "WRITE_TOOL_ATTEMPTED_IN_PLANNING"  # NOT PASS
    assert row["status"] == "FAIL"
    assert _strip_volatile(full) == _strip_volatile(row)


def test_approve_no_execution_coded_in_both_shapes():
    full, row = _both_shapes(_approve_no_execution_log())
    assert full["code"] == "APPROVE_PLAN_NO_EXECUTION"
    assert row["code"] == "APPROVE_PLAN_NO_EXECUTION"  # NOT PASS
    assert row["status"] == "FAIL"
    assert _strip_volatile(full) == _strip_volatile(row)


def test_no_replan_coded_in_both_shapes():
    full, row = _both_shapes(_no_replan_log())
    assert full["code"] == "NO_REPLAN_AFTER_REVISION"
    assert row["code"] == "NO_REPLAN_AFTER_REVISION"  # NOT PASS
    assert row["status"] == "FAIL"
    assert _strip_volatile(full) == _strip_volatile(row)


def test_db_row_payload_is_merged_not_dropped():
    # Direct normalizer proof: a row's JSON payload content survives normalization.
    from harness.build_soak.events import normalize_event

    full = plan(3, revision=2)
    [row] = to_db_rows([full])
    norm = normalize_event(row)
    assert norm["revision"] == 2
    assert norm["kind"] == "plan"
    assert norm["seq"] == 3


# ---- P0 #2 — a plan that FINISHED without ever being approved must FAIL ----


def test_plan_finished_without_approval_fails():
    events = [
        msg(1, "user", "build"),
        plan(2, revision=1),
        status(3, "FINISHED"),  # no AWAITING / plan_approved ever
    ]
    c = classify(events, scenario=_PLAN_SCN)
    assert c["status"] == "FAIL"
    assert c["code"] == "PLAN_APPROVED_STATUS_MISSING"
    # the FIRST missing link in the ordered chain is the human-approval gate
    assert c["first_broken_link"] == "plan_event -> awaiting_plan_approval"


def test_plan_awaiting_approval_is_not_a_false_finish():
    # A run legitimately parked at AWAITING_PLAN_APPROVAL (non-terminal) is NOT a
    # finish — it must not be flagged as a missing-approval failure.
    events = [
        msg(1, "user", "build"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
    ]
    c = classify(events, scenario=_PLAN_SCN)
    assert c["status"] == "PASS"


# ---- P0 #2b — approval WITHOUT a preceding AWAITING_PLAN_APPROVAL (forged gate) ----


def _approval_without_awaiting_log():
    return [
        msg(1, "user", "build"),
        plan(2, revision=1),
        status(3, "RUNNING", "plan_approved"),  # NO awaiting gate before it
        action(4, "shell", action_id="a4"),
        observation(5, "a4"),
        status(6, "FINISHED"),
    ]


def test_approval_without_preceding_awaiting_fails_both_shapes():
    full = classify(_approval_without_awaiting_log(), scenario=_PLAN_SCN)
    row = classify(to_db_rows(_approval_without_awaiting_log()), scenario=_PLAN_SCN)
    assert full["status"] == "FAIL"
    assert full["code"] == "PLAN_APPROVED_STATUS_MISSING"
    assert full["first_broken_link"] == "plan_event -> awaiting_plan_approval"
    assert row["code"] == "PLAN_APPROVED_STATUS_MISSING"  # NOT PASS in row shape


def test_legit_full_chain_still_passes():
    # user -> Plan -> AWAITING -> RUNNING/plan_approved -> action -> obs -> FINISHED
    events = [
        msg(1, "user", "build"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="a5"),
        observation(6, "a5"),
        status(7, "FINISHED"),
    ]
    c = classify(events, scenario=_PLAN_SCN)
    assert c["status"] == "PASS"


def test_autonomous_inline_approval_without_awaiting_passes():
    # Autonomous build: plan_approved emitted inline with NO awaiting -> legitimate.
    c = classify(
        _approval_without_awaiting_log(),
        scenario={**_PLAN_SCN, "autonomous": True},
    )
    assert c["status"] == "PASS"


def test_autonomous_override_via_manifest_flag():
    # The classify-level autonomous override (manifest flag) relaxes the awaiting link.
    c = classify(_approval_without_awaiting_log(), scenario=_PLAN_SCN, autonomous=True)
    assert c["status"] == "PASS"


# ---- P0 #2c — REVISED approval must follow the revised plan (ordered revised chain) ----


def _initial_build_with_awaiting():
    return [
        msg(1, "user", "build"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="a5"),
        observation(6, "a5"),
        status(7, "FINISHED"),
    ]


def _revised_approval_before_revised_plan_log():
    return _initial_build_with_awaiting() + [
        msg(8, "user", "revise it"),
        status(9, "RUNNING", "plan_approved"),  # approval BEFORE the revised plan
        plan(10, revision=2),
        action(11, "file_write", args={"path": "x"}, action_id="a11"),
        observation(12, "a11"),
        status(13, "FINISHED"),
    ]


def test_revised_approval_before_revised_plan_fails_both_shapes():
    full = classify(_revised_approval_before_revised_plan_log())
    row = classify(to_db_rows(_revised_approval_before_revised_plan_log()))
    assert full["status"] == "FAIL"
    assert full["code"] == "STALE_PLAN_USED_AFTER_FOLLOWUP"
    assert row["status"] == "FAIL"  # NOT PASS in row shape
    assert row["code"] == "STALE_PLAN_USED_AFTER_FOLLOWUP"


def test_legit_revised_chain_passes():
    events = _initial_build_with_awaiting() + [
        msg(8, "user", "revise the heading"),
        status(9, "RUNNING", "planning"),
        plan(10, revision=2),
        awaiting(11, 10),
        status(12, "RUNNING", "plan_approved"),
        action(13, "file_write", args={"path": "index.html", "content": "x"}, action_id="a13"),
        observation(14, "a13"),
        status(15, "FINISHED"),
    ]
    c = classify(events)
    assert c["status"] == "PASS", c


def test_autonomous_revised_chain_passes():
    events = _initial_build_with_awaiting() + [
        msg(8, "user", "revise"),
        plan(9, revision=2),
        status(10, "RUNNING", "plan_approved"),  # inline approve, no awaiting
        action(11, "file_write", args={"path": "x"}, action_id="a11"),
        observation(12, "a11"),
        status(13, "FINISHED"),
    ]
    c = classify(events, autonomous=True)
    assert c["status"] == "PASS", c


# ---- P0 #3 — required-but-missing tool-scope evidence -> INVALID_RUN ----


def test_scenario_requires_tool_scope_missing_is_invalid_run():
    scenario = {
        "id": "must_plan_before_tool",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write", "shell", "serve"]},
        },
    }
    from _eventlog import clean_smoke_log

    # No tool_scope evidence supplied -> cannot adjudicate the §11.7 ALLOWED check.
    c = classify(clean_smoke_log(), scenario=scenario)
    assert c["status"] == "INVALID_RUN"
    assert c["required_evidence_present"] is False
    assert c["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"


def test_scenario_requires_tool_scope_present_can_pass():
    scenario = {
        "id": "must_plan_before_tool",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write", "shell", "serve"]},
        },
    }
    from _eventlog import clean_smoke_log

    # Captured planning tool scope with no leaked mutating tool -> adjudicable.
    tool_scope = [
        {
            "mode": "planning",
            "attempt": 1,
            "complete": True,
            "offered_tools": ["submit_plan", "file_read"],
            "allowed_tools": ["submit_plan", "file_read"],
            "offered_count": 2,
            "allowed_count": 2,
        }
    ]
    c = classify(clean_smoke_log(), scenario=scenario, tool_scope=tool_scope)
    assert c["status"] == "PASS"
    assert any(
        result.get("facts", {}).get("checked") == "allowed_in_planning"
        and result.get("facts", {}).get("planning_turn_count") == 1
        for result in c["oracle_results"]
    )


def test_scenario_requires_tool_scope_present_and_leaked_fails():
    scenario = {
        "id": "must_plan_before_tool",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    from _eventlog import clean_smoke_log

    # A mutating tool remained CALLABLE in planning scope -> §11.7 violation.
    tool_scope = [
        {
            "mode": "planning",
            "attempt": 1,
            "complete": True,
            "offered_tools": ["submit_plan"],
            "allowed_tools": ["submit_plan", "file_write"],
            "offered_count": 1,
            "allowed_count": 2,
        }
    ]
    c = classify(clean_smoke_log(), scenario=scenario, tool_scope=tool_scope)
    assert c["status"] == "FAIL"
    assert c["code"] == "WRITE_TOOL_ALLOWED_IN_PLANNING"


def _scope_trace(*scopes: dict) -> dict:
    events = [
        {"seq": index, "kind": "tool_scope", **scope} for index, scope in enumerate(scopes, start=1)
    ]
    return {
        "conversation_id": "conv_scope",
        "event_count": len(events),
        "dropped_event_count": 0,
        "routing_decisions": [],
        "spans": [],
        "tool_scopes": list(scopes),
        "events": events,
    }


def _valid_planning_scope(**updates) -> dict:
    scope = {
        "mode": "planning",
        "attempt": 1,
        "complete": True,
        "offered_tools": ["file_read", "submit_plan"],
        "allowed_tools": ["file_read", "submit_plan"],
        "offered_count": 2,
        "allowed_count": 2,
    }
    scope.update(updates)
    return scope


def test_required_tool_scope_is_derived_from_inspect_trace():
    scenario = {
        "id": "scope_from_trace",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    c = classify(
        clean_smoke_log(),
        scenario=scenario,
        inspect_trace=_scope_trace(_valid_planning_scope()),
    )
    assert c["status"] == "PASS", c
    assert not any(
        result.get("skipped", False)
        for result in c["oracle_results"]
        if result["oracle"] == "ToolScopeOracle"
    )


def test_required_tool_scope_tampered_projection_is_invalid():
    scenario = {
        "id": "scope_tampered",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    trace = _scope_trace(_valid_planning_scope())
    trace["tool_scopes"][0]["allowed_tools"] = ["submit_plan"]
    c = classify(clean_smoke_log(), scenario=scenario, inspect_trace=trace)
    assert c["status"] == "INVALID_RUN", c
    assert c["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"


def test_required_tool_scope_missing_projection_is_invalid():
    scenario = {
        "id": "scope_missing_projection",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    trace = _scope_trace(_valid_planning_scope())
    del trace["tool_scopes"]
    c = classify(clean_smoke_log(), scenario=scenario, inspect_trace=trace)
    assert c["status"] == "INVALID_RUN", c
    assert c["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"


def test_required_inspect_tool_scope_leaked_callable_fails():
    scenario = {
        "id": "scope_leak_from_trace",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    leaked = _valid_planning_scope(
        allowed_tools=["file_read", "file_write", "submit_plan"],
        allowed_count=3,
    )
    c = classify(clean_smoke_log(), scenario=scenario, inspect_trace=_scope_trace(leaked))
    assert c["status"] == "FAIL", c
    assert c["code"] == "WRITE_TOOL_ALLOWED_IN_PLANNING"


def test_required_tool_scope_overflow_is_invalid_not_truncated_pass():
    scenario = {
        "id": "scope_overflow",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    overflow = _valid_planning_scope(
        complete=False,
        offered_tools=[],
        allowed_tools=[],
        offered_count=513,
        allowed_count=513,
    )
    c = classify(clean_smoke_log(), scenario=scenario, inspect_trace=_scope_trace(overflow))
    assert c["status"] == "INVALID_RUN", c
    assert c["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"


def test_required_tool_scope_evicted_from_trace_ring_is_invalid():
    scenario = {
        "id": "scope_trace_truncated",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    trace = _scope_trace(_valid_planning_scope())
    trace["dropped_event_count"] = 1
    c = classify(clean_smoke_log(), scenario=scenario, inspect_trace=trace)
    assert c["status"] == "INVALID_RUN", c
    assert c["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"


def test_required_tool_scope_without_planning_turn_is_invalid():
    scenario = {
        "id": "scope_no_planning",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    scope = _valid_planning_scope(mode="long_horizon")
    c = classify(clean_smoke_log(), scenario=scenario, inspect_trace=_scope_trace(scope))
    assert c["status"] == "INVALID_RUN", c
    assert c["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"


def test_required_tool_scope_malformed_turn_is_invalid():
    scenario = {
        "id": "scope_malformed",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "tool_scope": {"planning_disallows": ["file_write"]},
        },
    }
    scope = _valid_planning_scope(allowed_tools="file_read")
    c = classify(clean_smoke_log(), scenario=scenario, inspect_trace=_scope_trace(scope))
    assert c["status"] == "INVALID_RUN", c
    assert c["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"
