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
