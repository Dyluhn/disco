"""C18 — per-step `done_condition` advisory predicate.

# Contract

`PlanStepInput` gains an OPTIONAL `done_condition` predicate that reuses the
`disco.core.dod.DoDPredicate` discriminated union (file_exists / command /
http_ok). When the agent later marks the step done via
`plan_step(idx, 'done')`, the engine evaluates the predicate and emits a
visible pass/fail note in the trace. ADVISORY ONLY:

  * never blocks the run;
  * never nudges the agent (no <system-reminder>, no auto-continue, no
    speak-back to the model);
  * never duplicates the C1c finish gate (the C18 check uses a lightweight
    inline evaluator; C1c uses the heavy fresh-context DoDEvaluator and
    gates `finish` itself).

Steps WITHOUT a predicate behave exactly as today (back-compat): no
lookup, no note, no extra event. The check fires only on `state="done"`,
never on `state="active"`.

# Acceptance (this file)

  1. Step with a SATISFIED predicate → the run completes cleanly AND a
     `met` advisory note is emitted in the trace.
  2. Step with an UNSATISFIED predicate → a `NOT met` advisory note is
     emitted (visible — no silent pass).
  3. Step WITHOUT a predicate → no advisory note, no extra events;
     behavior is byte-identical to the pre-C18 path.

Tests use only fakes (no real model, no real container) — same pattern as
`test_c5_pmx_memory_persistence.py` and `test_c16_hard_reset_pointer_flush.py`.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step

CID = "conv-c18"


# ---------------------------------------------------------------------------
# Helper fakes — a sandbox-backed executor that lets the test plant a
# workspace and inject a successful `execute()` for every tool call (so
# plan_step completes and the post-execute C18 hook runs).
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """Minimal sandbox surface the C18 evaluator needs: `workspace_path` for
    `file_exists` path resolution. The list of files in the workspace is
    what the test plants up front, so the predicate's outcome is
    deterministic."""

    def __init__(self, workspace_path: str, files: dict[str, bytes] | None = None) -> None:
        self.workspace_path = workspace_path
        self._files: dict[str, bytes] = dict(files or {})

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = bytes(data)


class _SandboxExecutor:
    """ToolExecutor stub: any `execute()` returns success (so plan_step's
    in-process tool completes), and a `sandbox` attribute is exposed so the
    C18 evaluator can resolve `file_exists` paths against the workspace.

    `available_tools()` offers `shell` so the Rung-7 unknown-tool requery
    doesn't trip on tool names the agent uses between `submit_plan` and
    `plan_step`."""

    def __init__(self, sandbox: _FakeSandbox) -> None:
        self.sandbox = sandbox
        self.execute_calls: list[ToolCall] = []

    def available_tools(self):
        from disco.core.llm import ToolSpec
        return [
            ToolSpec(name="shell", description="run a shell command", parameters_schema={}),
            ToolSpec(name="plan_step", description="mark plan step progress", parameters_schema={}),
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        self.execute_calls.append(call)
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


def _advisory_notes(events: list) -> list[MessageEvent]:
    """Pull the C18 advisory notes out of the event log. They are
    `MessageEvent(source=ENVIRONMENT, meta={"advisory": "plan_step_done_condition", ...})`
    — NOT wrapped in <system-reminder> (so they are visible but
    non-nudging)."""
    return [
        e for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and isinstance(e.meta, dict)
        and e.meta.get("advisory") == "plan_step_done_condition"
    ]


def _has_no_advisory_note(events: list) -> bool:
    """Back-compat assertion helper: the trace contains zero C18 notes."""
    return _advisory_notes(events) == []


# ---------------------------------------------------------------------------
# Test 1 — step with a SATISFIED predicate → clean done + visible "met" note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c18_satisfied_predicate_emits_met_note(tmp_path):
    """A plan step whose `done_condition` (file_exists) is satisfied at the
    moment the agent marks it done → the C18 advisory note says `met`. The
    run completes cleanly; the note is visible in the trace."""
    # Plant a file the predicate will look for.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "artifact.txt").write_text("ok")
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            # submit_plan: one step with a satisfied file_exists predicate
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "artifact.txt",
                            },
                        }
                    ],
                },
            ),
            # plan_step(1, 'done') — the C18 hook evaluates the predicate
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    # The run landed FINISHED (the single step was marked done; finish was
    # accepted because the plan is complete).
    final_status = next(
        (e for e in reversed(events) if isinstance(e, StatusEvent)),
        None,
    )
    assert final_status is not None
    assert getattr(final_status, "status", None) == ConversationStatus.FINISHED
    # The C18 advisory note is present AND says "met".
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    assert "done-condition" in note.message.content
    assert "met" in note.message.content
    assert "NOT met" not in note.message.content
    assert note.meta.get("passed") is True
    # And the note is NOT wrapped in <system-reminder> (no nudge).
    assert "<system-reminder>" not in note.message.content


# ---------------------------------------------------------------------------
# Test 2 — step with an UNSATISFIED predicate → visible "NOT met" note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c18_unsatisfied_predicate_emits_not_met_note(tmp_path):
    """A plan step whose `done_condition` is NOT satisfied at the moment
    the agent marks it done → the C18 advisory note says `NOT met`. The
    note is visible (no silent pass) AND the agent is NOT nudged (no
    <system-reminder>, no auto-continue, no speak-back to the model)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # No artifact.txt planted — the predicate will fail.
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "artifact.txt",
                            },
                        }
                    ],
                },
            ),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    # The note is visible: a C18 advisory note saying "NOT met".
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    assert "done-condition" in note.message.content
    assert "NOT met" in note.message.content
    assert note.meta.get("passed") is False
    # And the note is NOT wrapped in <system-reminder> (no nudge, no
    # speak-back to the model — the run proceeds as if the step were
    # done, which is exactly the C18 "advisory, not a gate" contract).
    assert "<system-reminder>" not in note.message.content
    # The run still lands FINISHED (the plan is complete; the C18 note
    # is advisory only and does not duplicate the C1c finish gate).
    final_status = next(
        (e for e in reversed(events) if isinstance(e, StatusEvent)),
        None,
    )
    assert final_status is not None
    assert getattr(final_status, "status", None) == ConversationStatus.FINISHED


# ---------------------------------------------------------------------------
# Test 3 — step WITHOUT a predicate → no note, behavior unchanged (back-compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c18_no_predicate_emits_no_note(tmp_path):
    """A plan step with NO `done_condition` → no C18 advisory note, no
    extra event. This is the explicit back-compat target: today's
    plan_step flow is silent, and it stays silent when no predicate is
    attached."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            # submit_plan: a step with NO done_condition field
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {"title": "do the thing"},
                    ],
                },
            ),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    # Zero C18 advisory notes — back-compat preserved.
    assert _has_no_advisory_note(events), (
        f"expected no C18 notes for a no-predicate step, got "
        f"{[n.message.content for n in _advisory_notes(events)]}"
    )
    # The run still lands FINISHED (plan complete; finish accepted).
    final_status = next(
        (e for e in reversed(events) if isinstance(e, StatusEvent)),
        None,
    )
    assert final_status is not None
    assert getattr(final_status, "status", None) == ConversationStatus.FINISHED
    # The plan_step ActionEvent + its ObservationEvent are the only
    # trace artifacts for the mark-done — exactly as today.
    plan_step_actions = [
        e for e in events
        if isinstance(e, ActionEvent) and e.tool_call and e.tool_call.tool_name == "plan_step"
    ]
    assert len(plan_step_actions) == 1
    plan_step_observations = [
        e for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result is not None
        and e.tool_result.tool_name == "plan_step"
    ]
    assert len(plan_step_observations) == 1
