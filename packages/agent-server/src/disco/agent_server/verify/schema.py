"""Pydantic models for the disco-verify API-first runner (W15).

``Scenario`` describes what to submit to the app and what invariants to check.
``VerifyResult`` captures the outcome of one ``run_scenario`` call, including
the path to the written evidence dossier.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Scenario(BaseModel):
    """A single disco-verify scenario — what to submit and what to assert.

    The runner drives the app through its HTTP/WS API using these fields and
    then checks the declared invariants against the live evidence.
    """

    # Unique id used in dossier filenames and result reporting.
    id: str
    # Which surface to open: "research" | "build" | "agent" | "deep_research".
    surface: str = "agent"
    # The user message to send once the conversation is created.
    prompt: str
    # Optional model key to pin for this conversation (None → server default).
    model_override: str | None = None
    # When True, the runner waits for AWAITING_PLAN_APPROVAL over the WS and
    # sends {"type": "approve_plan"} before polling for terminal state.
    approve_plan: bool = False
    # Expected invariants (machine-readable). Known keys:
    #   terminal_status: "FINISHED" | "ERROR" | "STUCK" | "IDLE"
    #   deliverable_type: "deck" | "pdf" | "audio" | "sheet" | …
    expect: dict[str, Any] = Field(default_factory=dict)
    # Logical invariants that must NOT be present. Known tokens:
    #   "raw_html_default"         — no .html-only deliverable output
    #   "procedural_image_provider" — image_generate must not use provider=procedural
    forbid: list[str] = Field(default_factory=list)
    # Gap #3: extra WS command frames sent AFTER the initial send_message (and after
    # plan approval, if any) — e.g. [{"type": "steer", "content": "focus on tests"},
    # {"type": "stop"}]. Lets a scenario exercise the ~17 backend commands the runner
    # used to ignore (steer / stop / kill / resume / reject_action / pick_alternative /
    # answer / revise_plan / …) instead of only send_message + approve_plan.
    ws_commands: list[dict[str, Any]] = Field(default_factory=list)
    auto_answer: str | None = None
    # Gap #54: when set ("md" | "pdf" | "docx"), the runner calls
    # POST /conversations/{cid}/report/export?format=… after the run and validates the
    # returned BYTES — report export bypasses the event-log deliverable path, so it is
    # otherwise invisible to _locate_deliverables.
    report_export: str | None = None
    # Gap #98 (REGRESSION seam): when set, the runner POSTs to the schedule "fire now"
    # test hook for this schedule id so cron-driven behavior is exercised WITHOUT
    # waiting on wall-clock, then asserts a schedule_run event appears. Needs the
    # backend test endpoint (cross-file dependency — see runner.fire_schedule_now).
    fire_schedule_id: str | None = None
    # Per-scenario timeout in seconds (overridden downward by run_scenario timeout_s).
    timeout_s: int = 600


class VerifyResult(BaseModel):
    """The outcome of one ``run_scenario`` call.

    Written as ``result.json`` inside the dossier and returned to the caller.
    ``passed`` is the single go/no-go signal: True iff no validator problems
    AND the declared ``expect.terminal_status`` (if any) was reached.
    """

    # Mirrors the scenario id for traceability.
    scenario_id: str
    # The conversation's ``execution_status`` at the time polling terminated.
    terminal_status: str
    # Deliverables located from the event log: each dict has ``path``, ``kind``,
    # ``tool`` keys and is workspace-relative.
    deliverables: list[dict[str, Any]]
    # Non-empty when any validator or forbid check fired. Each entry is a
    # human-readable problem description.
    validator_problems: list[str]
    # True iff validator_problems is empty AND the expected terminal_status matched.
    passed: bool
    # Absolute path to the written dossier folder.
    dossier_path: str
