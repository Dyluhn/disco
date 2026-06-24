"""Fail-closed adjudicator pins (codex P0 false-negative regressions).

Three P0 false-negatives where the adjudicator would have classified a REAL failure
as PASS. Each is pinned here so it can never silently regress:

  P0 #1 — a real SQLite row {seq,kind,source,payload(JSON)} had its payload DROPPED
          by the normalizer, hiding detail/tool_call/revision from every predicate,
          so WRITE_TOOL_ATTEMPTED_IN_PLANNING / APPROVE_PLAN_NO_EXECUTION /
          NO_REPLAN_AFTER_REVISION all classified PASS in row shape.
  P0 #2 — a plan that reached FINISHED with NO plan_approved status PASSED (the
          approval chain was only checked when an approval already existed).
  P0 #3 — a scenario REQUIRING tool-scope evidence with that evidence missing was
          SKIPped into PASS instead of INVALID_RUN.
"""

from __future__ import annotations

from _eventlog import action, msg, observation, plan, status, to_db_rows

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
        status(3, "RUNNING", "plan_approved"),
        status(4, "FINISHED"),
    ]


def _no_replan_log():
    return [
        msg(1, "user", "build"),
        plan(2, revision=1),
        status(3, "RUNNING", "plan_approved"),
        action(4, "shell", action_id="a4"),
        observation(5, "a4"),
        status(6, "FINISHED"),
        msg(7, "user", "also add a contact page"),
        action(8, "file_write", args={"path": "c.html", "content": "x"}, action_id="a8"),
        observation(9, "a8", tool="file_write"),
        status(10, "FINISHED"),
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
    assert c["first_broken_link"] == "plan_event -> approval_status"


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
    tool_scope = [{"mode": "planning", "allowed_tools": ["submit_plan", "file_read"]}]
    c = classify(clean_smoke_log(), scenario=scenario, tool_scope=tool_scope)
    assert c["status"] == "PASS"


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
    tool_scope = [{"mode": "planning", "allowed_tools": ["submit_plan", "file_write"]}]
    c = classify(clean_smoke_log(), scenario=scenario, tool_scope=tool_scope)
    assert c["status"] == "FAIL"
    assert c["code"] == "WRITE_TOOL_ALLOWED_IN_PLANNING"
