"""Deterministic classifier: fixture event logs -> exact codes + first broken link,
row-shape normalization, evidence-hash-mismatch -> INVALID_RUN (guidelines §13, §16, PR S2)."""

from __future__ import annotations

import json

from _eventlog import action, agent_error, awaiting, clean_smoke_log, msg, observation, plan, status

from harness.build_soak.classify import classify, classify_run_folder
from harness.build_soak.evidence import (
    EvidenceManifest,
    compute_evidence_hashes,
    write_manifest,
)

_SCN = {
    "id": "static_html_minimal",
    "assertions": {
        "event_chain": {"require_plan_before_execution": True},
        "workspace": {"files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}]},
        "terminal_status_in": ["FINISHED"],
    },
}


def test_clean_run_passes():
    scenario = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}
    c = classify(clean_smoke_log(), scenario=scenario)
    assert c["status"] == "PASS"
    assert c["severity"] == "NONE"
    assert c["code"] is None
    assert c["accepted_by"] == "oracle"
    assert c["agent_comments_ignored_for_adjudication"] is True


def test_no_replan_classifies_p0_with_broken_link():
    events = [
        msg(1, "user", "build"),
        plan(2, revision=1),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="a5"),
        observation(6, "a5"),
        status(7, "FINISHED"),
        msg(8, "user", "also add a contact page"),
        action(9, "file_write", args={"path": "c.html", "content": "x"}, action_id="a9"),
        observation(10, "a9"),
        status(11, "FINISHED"),
    ]
    c = classify(events)
    assert c["status"] == "FAIL"
    assert c["code"] == "NO_REPLAN_AFTER_REVISION"
    assert c["severity"] == "P0"
    assert c["first_broken_link"] == "followup_user_event -> revised_plan_event"
    # advisory likely-files surfaced for the repair loop
    assert any("plans.py" in f for f in c.get("likely_files", []))


def test_write_in_planning_attempted_classifies():
    # A mutating action before any plan approval, in a plan-gated run.
    events = [
        msg(1, "user", "build"),
        action(2, "file_write", args={"path": "index.html", "content": "bad"}, action_id="a2"),
        observation(3, "a2"),
        plan(4, revision=1),
        status(5, "AWAITING_PLAN_APPROVAL", "evt_4"),
    ]
    c = classify(events)
    assert c["status"] == "FAIL"
    assert c["code"] == "WRITE_TOOL_ATTEMPTED_IN_PLANNING"
    assert c["severity"] == "P0"


# The product's planning-gate refusal text (disco.core.loop.engine `_gate_planning_mode`,
# only the tool name interpolated). Fixtures must carry the REAL marker the oracle keys
# on — a generic "rejected" string is NOT a recognized gate rejection.
_PLANNING_GATE_REFUSAL = (
    "<system-reminder>\n"
    "REFUSED: `file_write` is not available in PLANNING mode. No workspace mutation "
    "or execution is allowed before plan approval. Call `submit_plan`.\n"
    "</system-reminder>"
)


def test_rejected_write_in_planning_is_not_a_violation():
    # Bug 13 (tool_scope mirror): a mutating action ATTEMPTED in planning that the
    # planning gate REJECTS BEFORE execution (the REAL _gate_planning_mode marker, no
    # observation) did NOT mutate state — the §11.3 tool-rejection contract working. It
    # must NOT raise WRITE_TOOL_ATTEMPTED_IN_PLANNING. The recovery (file_read) + a real
    # plan follow, so the run is clean.
    events = [
        msg(1, "user", "build"),
        action(2, "file_write", args={"path": "index.html", "content": "bad"}, action_id="a2"),
        agent_error(3, "a2", error=_PLANNING_GATE_REFUSAL),
        action(4, "file_read", args={"path": "index.html"}, action_id="a4"),
        observation(5, "a4", tool="file_read"),
        plan(6, revision=1),
        awaiting(7, 6),
        status(8, "RUNNING", "plan_approved"),
        action(9, "file_write", args={"path": "index.html", "content": "ok"}, action_id="a9"),
        observation(10, "a9", tool="file_write"),
        status(11, "FINISHED"),
    ]
    scenario = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}
    c = classify(events, scenario=scenario)
    assert c["code"] != "WRITE_TOOL_ATTEMPTED_IN_PLANNING", c
    assert c["status"] == "PASS", c


def test_nongate_agent_error_write_in_planning_still_a_violation():
    # codex anti-false-PASS #2 (tool_scope mirror): a planning write whose tool RAISED
    # (a bare AgentErrorEvent, no gate marker, no observation — it may have mutated then
    # thrown) is NOT a recognized gate rejection → STILL WRITE_TOOL_ATTEMPTED_IN_PLANNING.
    events = [
        msg(1, "user", "build"),
        action(2, "file_write", args={"path": "index.html", "content": "bad"}, action_id="a2"),
        agent_error(3, "a2", error="ERROR: file_write raised OSError: disk full"),
        plan(4, revision=1),
        status(5, "AWAITING_PLAN_APPROVAL", "evt_4"),
    ]
    c = classify(events)
    assert c["status"] == "FAIL"
    assert c["code"] == "WRITE_TOOL_ATTEMPTED_IN_PLANNING"


def test_failed_write_in_planning_still_a_violation():
    # codex anti-false-PASS mirror: a planning write that REACHED the executor and
    # returned a tool_result observation with success=False (ran, may have mutated
    # then failed) is STILL WRITE_TOOL_ATTEMPTED_IN_PLANNING. Only a gate-rejected
    # write (agent_error + NO observation) is the §11.3 PASS; an observed-but-failed
    # write is the product letting a write fall through and execute in planning.
    events = [
        msg(1, "user", "build"),
        action(2, "file_write", args={"path": "index.html", "content": "bad"}, action_id="a2"),
        observation(3, "a2", tool="file_write", success=False),  # ran, then failed
        plan(4, revision=1),
        status(5, "AWAITING_PLAN_APPROVAL", "evt_4"),
    ]
    c = classify(events)
    assert c["status"] == "FAIL"
    assert c["code"] == "WRITE_TOOL_ATTEMPTED_IN_PLANNING"


def test_action_no_observation_from_fixture():
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="a5"),
        status(6, "FINISHED"),
    ]
    scenario = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}
    c = classify(events, scenario=scenario)
    assert c["code"] == "ACTION_NO_OBSERVATION"
    assert c["facts"]["action_id"] == "a5"


def test_first_broken_link_wins_event_chain_before_output_truth():
    # Both an action/observation break AND a missing output file; the earlier link
    # (event chain) wins over output truth.
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", action_id="a5"),  # no observation
        status(6, "FINISHED"),
    ]
    c = classify(events, scenario=_SCN, workspace_manifest={"nope.html": "x"})
    assert c["code"] == "ACTION_NO_OBSERVATION"  # not FALSE_FINISH_NO_OUTPUT


def test_row_shaped_events_normalize_identically():
    # The DB row shape {seq, kind, source, payload} must classify the same as the
    # full-event-dict shape.
    full = clean_smoke_log()
    rows = [{"seq": e["seq"], "kind": e["kind"], "source": e["source"], "payload": e} for e in full]
    scenario = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}
    assert classify(full, scenario=scenario)["status"] == "PASS"
    assert classify(rows, scenario=scenario)["status"] == "PASS"


def test_empty_log_is_invalid_run():
    c = classify([])
    assert c["status"] == "INVALID_RUN"
    assert c["code"] == "NO_EVENTS"
    assert c["required_evidence_present"] is False


def test_evidence_hash_mismatch_is_invalid_run():
    c = classify(clean_smoke_log(), evidence_intact=False)
    assert c["status"] == "INVALID_RUN"
    assert c["code"] == "EVIDENCE_HASH_MISMATCH"


def test_agent_error_pairing_does_not_break_chain():
    # A tool REJECTED during EXECUTION (post-approval) is answered by an
    # AgentErrorEvent — a valid pairing, NOT a missing observation. The run is
    # otherwise clean, so the whole chain passes.
    events = [
        msg(1, "user", "build"),
        plan(2),
        awaiting(3, 2),
        status(4, "RUNNING", "plan_approved"),
        action(5, "shell", args={"cmd": "boom"}, action_id="a5"),
        agent_error(6, "a5"),
        action(7, "shell", action_id="a7"),
        observation(8, "a7"),
        status(9, "FINISHED"),
    ]
    scenario = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}
    c = classify(events, scenario=scenario)
    assert c["status"] == "PASS"


def test_stuck_approve_no_execution_classifies_as_terminal_fail():
    # codex #1: the loop's approve-but-never-execute exit terminalizes the run as
    # StatusEvent(STUCK, detail=approve_plan_no_execution) (finish.py execution-nudge
    # cap). STUCK is a TERMINAL state — the run gave up, no further work without a
    # fresh user turn. The dossier (plan approved, NO action after approval) must
    # classify as the FAIL it is: APPROVE_PLAN_NO_EXECUTION / P0 — NOT INVALID_RUN and
    # NOT a false PASS (which is what happened while STUCK read as non-terminal).
    events = [
        msg(1, "user", "build a page"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        awaiting(4, 3),
        status(5, "RUNNING", "plan_approved"),
        # no execution action ever appears
        status(6, "STUCK", "approve_plan_no_execution"),
    ]
    scenario = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}
    c = classify(events, scenario=scenario)
    assert c["status"] == "FAIL"
    assert c["code"] == "APPROVE_PLAN_NO_EXECUTION"
    assert c["severity"] == "P0"
    assert c["first_broken_link"] == "approval_status -> execution_action"


def test_paused_incomplete_required_output_is_build_did_not_finish():
    # LIVE-SURFACED adjudicator gap (surfaced-bugs Bug 8): a bare-Build run that wrote
    # work but PAUSED "actionless" (no-progress valve) instead of FINISHING used to
    # classify PASS — the required output was never verified because OutputTruth skipped
    # any non-finished terminal. It must FAIL-CLOSED: BUILD_DID_NOT_FINISH / P1.
    events = [
        msg(1, "user", "build a bakery page"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        awaiting(4, 3),
        status(5, "RUNNING", "plan_approved"),
        action(6, "file_write", args={"path": "index.html", "content": "x"}, action_id="a6"),
        observation(7, "a6", tool="file_write"),
        status(8, "PAUSED", "actionless"),  # the no-progress valve — NOT finished
    ]
    c = classify(events, scenario=_SCN, workspace_manifest={"index.html": "Build Smoke OK"})
    assert c["status"] == "FAIL"
    assert c["code"] == "BUILD_DID_NOT_FINISH"
    assert c["severity"] == "P1"


def test_classify_run_folder_writes_classification(tmp_path):
    conv = tmp_path / "conversations" / "conv_x"
    conv.mkdir(parents=True)
    events_path = conv / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(e) for e in clean_smoke_log()) + "\n", encoding="utf-8"
    )
    files = {"events": "conversations/conv_x/events.jsonl"}
    manifest = EvidenceManifest(
        run_id="run1",
        scenario_id="static_html_minimal",
        evidence_files=files,
        evidence_hashes=compute_evidence_hashes(tmp_path, files),
    )
    write_manifest(tmp_path, manifest)
    scenario = {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}
    c = classify_run_folder(tmp_path, scenario=scenario)
    assert c["status"] == "PASS"
    written = json.loads((tmp_path / "classification.json").read_text())
    assert written["status"] == "PASS"
    assert written["run_id"] == "run1"


def test_classify_run_folder_detects_tamper(tmp_path):
    conv = tmp_path / "conversations" / "conv_x"
    conv.mkdir(parents=True)
    events_path = conv / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(e) for e in clean_smoke_log()) + "\n", encoding="utf-8"
    )
    files = {"events": "conversations/conv_x/events.jsonl"}
    manifest = EvidenceManifest(
        run_id="run1",
        scenario_id="s",
        evidence_files=files,
        evidence_hashes=compute_evidence_hashes(tmp_path, files),
    )
    write_manifest(tmp_path, manifest)
    # tamper after freeze
    events_path.write_text('{"tampered": true}\n', encoding="utf-8")
    c = classify_run_folder(tmp_path)
    assert c["status"] == "INVALID_RUN"
    assert c["code"] == "EVIDENCE_HASH_MISMATCH"


def _governed_thrash_scenario() -> dict:
    """Pilot-shaped scenario: governed verification required, one identical tool
    error allowed (the live monitor's strictness for seed 405210)."""
    return {
        "id": "governed-thrash-order",
        "assertions": {
            "thrash": {"max_same_tool_error_repeats": 1},
            "governed_verification": {
                "required": True,
                "route": "platform",
                "composition_authority": "build_platform_core",
                "delivery_mode": "interactive",
                "required_receipt_kinds": ["disco.web_functional@1"],
                "required_claim_kinds": {
                    "artifact_identity": 1,
                    "http_ready": 1,
                    "rendered_content": 1,
                    "console_clean": 1,
                    "network_clean": 1,
                },
                "required_execution_modality": "managed_preview",
            },
        },
    }


def _monitor_killed_thrash_log() -> list[dict]:
    """Seed 405210's shape: valid chain, two identical preview_start errors, then
    the live monitor killed the run — so no work terminal ever exists."""
    port_error = (
        "no free platform preview port found after checking 4 candidate(s): "
        "[3000, 8080, 5000, 4321]"
    )
    return [
        msg(1, "user", "build a page, serve it, verify it"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
        status(5, "RUNNING", "plan_approved"),
        status(6, "RUNNING"),
        action(7, "preview_start", args={"name": "app", "serve_dir": "/workspace"}, action_id="a7"),
        agent_error(8, "a7", error=port_error),
        action(9, "preview_start", args={"name": "app", "serve_dir": "/workspace"}, action_id="a9"),
        agent_error(10, "a9", error=port_error),
        status(11, "IDLE", "killed"),
    ]


def test_thrash_finding_outranks_downstream_missing_terminal():
    """The live monitor kills a thrashing run at the moment of the repeated error,
    so 'no successful work terminal exists' is DOWNSTREAM of the thrash. The
    classifier must report the earliest broken link, not the terminal shape."""
    c = classify(
        _monitor_killed_thrash_log(),
        scenario=_governed_thrash_scenario(),
        run_id="r-thrash-order",
        conversation_id="conv_thrash_order",
        commit="deadbeef",
    )
    assert c["status"] == "FAIL"
    assert c["code"] == "TOOL_ERROR_THRASH"
    assert c["first_broken_link"] == "tool_error -> repeated_same_tool_error"


def test_governed_missing_terminal_still_fails_closed_without_thrash():
    """Control: with no thrash in the log, the governed missing-terminal verdict
    is unchanged — reordering surfaced the earlier link, it weakened nothing."""
    events = _monitor_killed_thrash_log()
    del events[8:10]  # drop the second identical error pair; one error is allowed
    c = classify(
        events,
        scenario=_governed_thrash_scenario(),
        run_id="r-governed-terminal",
        conversation_id="conv_governed_terminal",
        commit="deadbeef",
    )
    assert c["status"] == "FAIL"
    assert c["code"] == "GOVERNED_ADMISSION_BYPASSED"
    assert c["first_broken_link"] == "event_chain -> terminal"
