"""Regression tests for the PLANNING-mode tool gate (engine.py _gate_planning_mode).

Closes WRITE_TOOL_ALLOWED_IN_PLANNING (P0) + WRITE_BEFORE_REVISION_APPROVAL (P1):
a non-allowlist tool (file_write/shell/browser/...) attempted in PLANNING must be
REJECTED with a recoverable, model-visible AgentErrorEvent — NEVER executed — both on
the first plan and on a revision re-entry, while submit_plan still works afterward.

Drives the REAL AgentLoop via loop_fakes (no live model, no sandbox).
"""

from __future__ import annotations

import pytest
from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from disco.core.context import ArtifactMemoryStore
from disco.core.dod import FileExistsPredicate
from disco.core.events import ConversationStatus, EventSource
from disco.core.llm import OperatingMode
from disco.core.loop import signals
from disco.core.loop.driver_retry import _PLANNING_TOOL_REFUSAL_NEEDLE
from loop_fakes import ScriptedAgent, action_step


class _MemorySandbox:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _submit_plan_step(summary="p"):
    return action_step("submit_plan", {"summary": summary, "steps": [{"title": "do"}]})


async def test_empty_initial_plan_is_corrected_before_approval_and_seeds_design() -> None:
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "Proposed plan", "steps": []}),
            action_step(
                "submit_plan",
                {
                    "summary": "Build the two-page static site",
                    "steps": [{"title": "Create the Home and About pages"}],
                },
            ),
        ]
    )
    executor = BuildExecutor()
    executor.sandbox = _MemorySandbox()  # type: ignore[attr-defined]
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-empty-initial-corrected",
        executor=executor,
    )

    await loop.send_message("Create a two-page static site with Home and About pages.")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    events = await store.get_events(loop.conversation_id)
    plans = [event for event in events if isinstance(event, PlanEvent)]
    assert len(plans) == 1
    assert plans[0].revision == 1
    assert [step.title for step in plans[0].steps] == ["Create the Home and About pages"]
    assert any(
        isinstance(event, StatusEvent) and event.detail == "invalid_plan_no_steps"
        for event in events
    )
    assert any(
        isinstance(event, MessageEvent) and event.meta.get("blocking") == "invalid_plan_no_steps"
        for event in events
    )

    await loop.approve_plan()
    memory = ArtifactMemoryStore(executor.sandbox)  # type: ignore[attr-defined]
    direction = await memory.read_design_direction()
    tokens = await memory.read_design_direction_tokens()
    assert direction is not None and "## Design Direction:" in direction
    assert tokens is not None and "disco direction tokens" in tokens


async def test_second_empty_initial_plan_recovers_exact_user_instruction() -> None:
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "Proposed plan", "steps": []}),
            action_step("submit_plan", {"summary": "Proposed plan", "steps": []}),
        ]
    )
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-empty-initial-user-recovery",
        executor=BuildExecutor(),
    )
    instruction = "Create a two-page static site with Home and About pages."

    await loop.send_message(instruction)
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    events = await store.get_events(loop.conversation_id)
    plans = [event for event in events if isinstance(event, PlanEvent)]
    assert len(plans) == 1
    assert plans[0].revision == 1
    assert [step.title for step in plans[0].steps] == [instruction]
    assert any(
        isinstance(event, StatusEvent)
        and event.detail == "plan_steps_recovered_from_user_instruction"
        for event in events
    )


async def test_repeated_empty_initial_plan_without_user_instruction_fails_closed() -> None:
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "Proposed plan", "steps": []}),
            action_step("submit_plan", {"summary": "Proposed plan", "steps": []}),
        ]
    )
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-empty-initial-no-user",
        executor=BuildExecutor(),
    )
    loop._autonomous = True
    await store.append(
        loop.conversation_id,
        StatusEvent(status=ConversationStatus.RUNNING, detail="test_start"),
    )

    state = await loop.run()

    assert state.execution_status == ConversationStatus.STUCK
    events = await store.get_events(loop.conversation_id)
    assert not any(isinstance(event, PlanEvent) for event in events)
    assert any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.STUCK
        and event.meta.get("legacy_detail") == "initial_plan_no_concrete_steps"
        for event in events
    )


def _unsafe_done_conditions_plan():
    return action_step(
        "submit_plan",
        {
            "summary": "unsafe external gates",
            "steps": [
                {
                    "title": "Create fonts directory",
                    "done_condition": {"kind": "file_exists", "path": "release/fonts"},
                },
                {
                    "title": "Write proof font",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "/workspace/release/fonts/proof.woff2",
                    },
                },
                {
                    "title": "Serve the release",
                    "done_condition": {
                        "kind": "http_ok",
                        "url": "http://localhost:3000",
                    },
                },
                {
                    "title": "Malformed condition",
                    "done_condition": {"kind": "file_exists"},
                },
                {
                    "title": "Escaping file",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "/workspace/../../etc/passwd",
                    },
                },
                {
                    "title": "Unsafe command",
                    "done_condition": {
                        "kind": "command",
                        "cmd": "rm -rf /",
                    },
                },
            ],
        },
    )


async def test_unsafe_done_conditions_rejected_then_corrected_plan_arms_cleanly():
    corrected = action_step(
        "submit_plan",
        {
            "summary": "safe exact gate",
            "steps": [
                {
                    "title": "Write proof font",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "release/fonts/proof.woff2",
                    },
                }
            ],
        },
    )
    agent = ScriptedAgent([_unsafe_done_conditions_plan(), corrected])
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-invalid-done-condition-recovers",
        executor=BuildExecutor(),
    )
    await loop.send_message("build a release")
    await loop.run()

    events = await store.get_events("pw-invalid-done-condition-recovers")
    plans = [event for event in events if isinstance(event, PlanEvent)]
    assert len(plans) == 1
    assert plans[0].summary == "safe exact gate"
    assert [step.done_condition for step in plans[0].steps] == [
        FileExistsPredicate(path="release/fonts/proof.woff2")
    ]
    assert (
        sum(
            isinstance(event, StatusEvent) and event.detail == "invalid_plan_done_conditions"
            for event in events
        )
        == 1
    )
    feedback = [
        event.message.content
        for event in events
        if isinstance(event, MessageEvent) and event.source == EventSource.ENVIRONMENT
    ]
    assert any("parent directory" in message for message in feedback)
    assert any("local preview host" in message for message in feedback)
    assert any("done_condition is malformed" in message for message in feedback)
    assert any("safe exact workspace file" in message for message in feedback)
    assert any("command condition is hard-denied" in message for message in feedback)
    assert any("Invalid plan attempt 1/3" in message for message in feedback)

    await loop.approve_plan()
    assert await store.get_external_dod_spec("pw-invalid-done-condition-recovers") is None
    approved = signals.latest_approved_plan(
        await store.get_events("pw-invalid-done-condition-recovers")
    )
    assert approved is not None
    assert [step.done_condition for step in approved.steps] == [
        FileExistsPredicate(path="release/fonts/proof.woff2")
    ]


async def test_repeated_unsafe_done_conditions_land_bounded_stuck():
    agent = ScriptedAgent([_unsafe_done_conditions_plan() for _ in range(3)])
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-invalid-done-condition-bounded",
        executor=BuildExecutor(),
    )
    loop._autonomous = True
    await loop.send_message("build a release")
    await loop.run()

    events = await store.get_events("pw-invalid-done-condition-bounded")
    assert not any(isinstance(event, PlanEvent) for event in events)
    assert (
        sum(
            isinstance(event, StatusEvent)
            and event.status == ConversationStatus.RUNNING
            and event.detail == "invalid_plan_done_conditions"
            for event in events
        )
        == 3
    )
    state = await loop.get_state()
    assert state.execution_status == ConversationStatus.STUCK
    assert any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.STUCK
        and event.detail == "invalid_plan_done_conditions"
        for event in events
    )


async def test_strict_appkit_rejects_noncanonical_gates_then_accepts_canonical_plan():
    """The public submit_plan boundary enforces AppKit's generated-file contract."""
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "invented generated paths",
                    "steps": [
                        {
                            "title": "Create the app",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "app/.app-kit-generated/app.json",
                            },
                        },
                        {
                            "title": "Run a source-level check",
                            "done_condition": {
                                "kind": "command",
                                "cmd": "test -s app/index.html",
                            },
                        },
                    ],
                },
            ),
            action_step(
                "submit_plan",
                {
                    "summary": "canonical semantic plan",
                    "steps": [
                        {
                            "title": "Create the semantic app",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "/workspace/.disco/appspec.json",
                            },
                        },
                        {"title": "Verify the app"},
                    ],
                },
            ),
        ]
    )
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-appkit-canonical-dod",
        executor=BuildExecutor(),
        strict_appkit_active=lambda: True,
    )
    await loop.send_message("build an AppKit app")
    await loop.run()

    events = await store.get_events("pw-appkit-canonical-dod")
    plans = [event for event in events if isinstance(event, PlanEvent)]
    assert [plan.summary for plan in plans] == ["canonical semantic plan"]
    assert plans[0].steps[0].done_condition == FileExistsPredicate(
        path="/workspace/.disco/appspec.json"
    )
    feedback = [
        event.message.content
        for event in events
        if isinstance(event, MessageEvent) and event.source == EventSource.ENVIRONMENT
    ]
    assert any(
        "not a canonical strict AppKit finish gate" in message
        and ".disco/appspec.json" in message
        and ".disco/designspec.json" in message
        and "verify_appkit_app owns behavioral proof" in message
        for message in feedback
    )


@pytest.mark.parametrize("strict_appkit_active", [None, lambda: False])
async def test_ordinary_and_custom_builds_preserve_noncanonical_done_conditions(
    strict_appkit_active,
):
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "ordinary plan",
                    "steps": [
                        {
                            "title": "Create output",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "dist/custom-output.json",
                            },
                        }
                    ],
                },
            )
        ]
    )
    cid = f"pw-appkit-widened-{strict_appkit_active is not None}"
    loop, store = build_plan_loop(
        agent,
        conversation_id=cid,
        executor=BuildExecutor(),
        strict_appkit_active=strict_appkit_active,
    )
    await loop.send_message("build it")
    await loop.run()

    plans = [event for event in await store.get_events(cid) if isinstance(event, PlanEvent)]
    assert len(plans) == 1
    assert plans[0].steps[0].done_condition == FileExistsPredicate(path="dist/custom-output.json")


async def test_write_in_planning_is_rejected_then_recovers_to_plan():
    """A file_write in PLANNING is rejected (paired AgentErrorEvent, never executed);
    the model gets another turn and submit_plan still produces a PlanEvent + halts at
    AWAITING_PLAN_APPROVAL."""
    agent = ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "bad"}),
            _submit_plan_step("plan after rejection"),
        ]
    )
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id="pw-reject", executor=executor)
    await loop.send_message("create a page")
    await loop.run()

    events = await store.get_events("pw-reject")

    # The write was ATTEMPTED (one ActionEvent recorded for audit/KV pairing) ...
    write_actions = [
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "file_write"
    ]
    assert len(write_actions) == 1, "expected exactly one attempted file_write ActionEvent"
    write_action = write_actions[0]

    # ... but it was REJECTED with a recoverable AgentErrorEvent paired by tool_call_id,
    # and NEVER executed.
    rejections = [
        e for e in events if isinstance(e, AgentErrorEvent) and e.action_id == write_action.id
    ]
    assert len(rejections) == 1, "expected exactly one paired rejection for the write"
    assert rejections[0].tool_call_id == write_action.tool_call.call_id
    assert "PLANNING" in rejections[0].error

    assert not any(c.tool_name == "file_write" for c in executor.calls), "write reached executor"
    assert "index.html" not in executor.world, "the file must NOT have been written"
    assert not any(
        isinstance(e, ObservationEvent) and e.tool_result.tool_name == "file_write" for e in events
    ), "no observation for an unexecuted write"

    # The agent got a SECOND turn and recovered with submit_plan.
    assert len(agent.seen_tools) >= 2, "the model did not get a second turn after rejection"
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert len(plans) == 1, "submit_plan after the rejection must produce a PlanEvent"
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_write_during_revision_is_rejected_before_revised_approval():
    """On a revision re-entry (back in PLANNING), a write is rejected before the
    revised plan is approved; submit_plan then yields PlanEvent.revision == 2."""
    agent = ScriptedAgent([_submit_plan_step("first")])
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id="pw-revise", executor=executor)
    await loop.send_message("build a page")
    await loop.run()
    await loop.approve_plan()

    # Re-enter planning for a revision; the agent tries to write, then submits.
    loop.agent = ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "x"}),
            _submit_plan_step("second"),
        ]
    )
    await loop.enter_planning("revise the heading")
    await loop.run()

    events = await store.get_events("pw-revise")

    write_actions = [
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "file_write"
    ]
    assert len(write_actions) == 1
    assert any(
        isinstance(e, AgentErrorEvent) and e.action_id == write_actions[0].id for e in events
    ), "the revision write must be rejected"
    assert not any(c.tool_name == "file_write" for c in executor.calls)
    assert "index.html" not in executor.world

    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2], "the revised plan submitted after rejection"
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_revision_refusal_escalation_harvests_one_step_plan():
    agent = ScriptedAgent([_submit_plan_step("first")])
    executor = BuildExecutor()
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-revision-harvest",
        executor=executor,
    )
    await loop.send_message("build a page")
    await loop.run()
    await loop.approve_plan()

    followup = "Change the hero CTA to Start now"
    loop.agent = ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "bad"}),
            action_step("file_write", {"path": "index.html", "content": "still bad"}),
        ]
    )
    await loop.enter_planning(followup)
    await loop.run()

    events = await store.get_events("pw-revision-harvest")
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]
    assert [step.title for step in plans[-1].steps] == [followup]
    refused_writes = [
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "file_write"
    ]
    assert len(refused_writes) == 1, "revision recovery waited for a duplicate refusal"
    assert (
        len(
            [
                e
                for e in events
                if isinstance(e, AgentErrorEvent) and e.action_id == refused_writes[0].id
            ]
        )
        == 1
    )
    assert any(isinstance(e, StatusEvent) and e.detail == "harvested_revision_plan" for e in events)
    assert not any(c.tool_name == "file_write" for c in executor.calls)
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

    await loop.approve_plan()
    assert loop.mode == OperatingMode.LONG_HORIZON


# Every non-allowlist tool FAMILY — not just file_write. The meta/finish handlers
# (notify_user/remember/serve/delegate_explore/finish) run BEFORE the planning gate
# in the loop, so the gate must sit AHEAD of them: a scripted finish/serve/remember/
# notify_user/shell in PLANNING must be rejected (recoverable), never dispatched to
# its handler or executor.
@pytest.mark.parametrize("tool", ["finish", "serve", "remember", "notify_user", "shell"])
async def test_non_allowlist_tool_rejected_in_planning(tool):
    agent = ScriptedAgent([action_step(tool, {}), _submit_plan_step("recover")])
    executor = BuildExecutor()
    cid = f"pw-{tool}"
    loop, store = build_plan_loop(agent, conversation_id=cid, executor=executor)
    await loop.send_message("create a page")
    await loop.run()

    events = await store.get_events(cid)

    # The disallowed tool was ATTEMPTED + REJECTED (paired AgentErrorEvent) ...
    attempts = [e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == tool]
    assert len(attempts) == 1, f"expected exactly one attempted {tool} ActionEvent"
    rejections = [
        e for e in events if isinstance(e, AgentErrorEvent) and e.action_id == attempts[0].id
    ]
    assert len(rejections) == 1, f"{tool} was not rejected with a paired AgentErrorEvent"
    assert rejections[0].tool_call_id == attempts[0].tool_call.call_id

    # ... its handler/executor never ran: no FINISHED, no executor call for it.
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED for e in events
    ), f"{tool} reached the finish/meta handler in PLANNING"
    assert not any(c.tool_name == tool for c in executor.calls), f"{tool} reached the executor"

    # ... and the model recovered: submit_plan after the rejection halts for approval.
    assert len([e for e in events if isinstance(e, PlanEvent)]) == 1
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_repeated_planning_shell_refusals_escalate_and_narrow_to_submit_or_read():
    agent = ScriptedAgent(
        [
            action_step("shell", {"cmd": "pwd"}),
            action_step("shell", {"cmd": "pwd"}),
            action_step("shell", {"cmd": "pwd"}),
            _submit_plan_step("plan after refusal escalation"),
        ]
    )
    executor = BuildExecutor()
    executor.readonly_tool_names = lambda: frozenset(  # type: ignore[attr-defined]
        {"file_read", "file_list", "search", "extract", "think"}
    )
    loop, store = build_plan_loop(
        agent, conversation_id="pw-shell-refusal-escalates", executor=executor
    )
    await loop.send_message("create a page")
    await loop.run()

    events = await store.get_events("pw-shell-refusal-escalates")
    refusals = [
        e
        for e in events
        if isinstance(e, AgentErrorEvent) and _PLANNING_TOOL_REFUSAL_NEEDLE in e.error
    ]
    assert len(refusals) == 3
    assert "ONLY valid next action" not in refusals[0].error
    assert "Planning-mode refusal 2" in refusals[1].error
    assert "You have " in refusals[1].error
    assert "Your ONLY valid next action is `submit_plan`" in refusals[1].error
    assert "Planning-mode refusal 3" in refusals[2].error
    assert "only `submit_plan` + `file_read`" in refusals[2].error
    assert not any(
        isinstance(e, StatusEvent) and e.detail == "harvested_revision_plan" for e in events
    )

    assert len(agent.seen_tools) >= 4
    assert set(agent.seen_tools[3]) == {"submit_plan", "file_read"}
    assert not any(c.tool_name == "shell" for c in executor.calls), "shell reached executor"
    assert len([e for e in events if isinstance(e, PlanEvent)]) == 1
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_allowlist_read_tools_still_execute_in_planning():
    """The planning allowlist (read/explore) still runs in PLANNING — the gate
    rejects ONLY non-allowlist tools, it does not block exploration."""
    agent = ScriptedAgent(
        [
            action_step("file_read", {"path": "a.txt"}),
            action_step("file_list", {"path": "."}),
            action_step("search", {"query": "x"}),
            action_step("extract", {"path": "a.txt"}),
            _submit_plan_step("plan"),
        ]
    )
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id="pw-reads", executor=executor)
    await loop.send_message("build")
    await loop.run()

    events = await store.get_events("pw-reads")
    ran = {c.tool_name for c in executor.calls}
    assert {"file_read", "file_list", "search", "extract"} <= ran, f"a read was blocked: {ran}"
    assert not any(isinstance(e, AgentErrorEvent) for e in events), "a read tool was rejected"
    assert len([e for e in events if isinstance(e, PlanEvent)]) == 1
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_think_allowed_in_planning_executes_then_plans():
    """`think` is an allowlisted, read-only NO-OP scratchpad — it must NOT be rejected
    by the planning phase gate: it EXECUTES (a harmless no-op), the loop continues, and
    the model can then submit_plan (FIXED THINK_NOT_EXPOSED). It is also a 'free' step —
    it does not count as an exploration read (the explore-read cap is untouched)."""
    agent = ScriptedAgent(
        [
            action_step("think", {"thought": "weighing the approach before I plan"}),
            _submit_plan_step("plan after thinking"),
        ]
    )
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id="pw-think", executor=executor)
    await loop.send_message("create a page")
    await loop.run()

    events = await store.get_events("pw-think")

    # think was NOT rejected ...
    assert not any(isinstance(e, AgentErrorEvent) for e in events), "think was wrongly rejected"
    # ... it actually reached the executor (executed as a no-op) ...
    assert any(c.tool_name == "think" for c in executor.calls), "think did not execute"
    # ... it did NOT count as an explore read (no plan was forced; the cap is untouched) ...
    assert loop._plan_explore_reads == 0, "think must not count toward the explore-read cap"
    # ... and the model got another turn and submitted its plan, halting for approval.
    assert len([e for e in events if isinstance(e, PlanEvent)]) == 1
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_ask_user_allowed_in_planning_halts_for_input():
    """ask_user is an allowlisted virtual escape hatch — it must NOT be rejected; it
    halts at AWAITING_USER_QUESTION via its existing handler."""
    agent = ScriptedAgent([action_step("ask_user", {"question": "which color?"})])
    loop, store = build_plan_loop(agent, conversation_id="pw-ask")
    await loop.send_message("build a page")
    await loop.run()

    events = await store.get_events("pw-ask")
    assert not any(isinstance(e, AgentErrorEvent) for e in events), "ask_user was wrongly rejected"
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_USER_QUESTION


# ---------------------------------------------------------------------------
# H368 — model command strings are plan-owned, not external DoD
# ---------------------------------------------------------------------------


async def test_h368_interactive_input_predicate_is_approved_as_plan_owned_only():
    """The exact RUN-768 command remains valid syntax, but approval does not copy
    it into immutable external acceptance storage."""
    from disco.core.dod import CommandExitPredicate

    # The exact observed defect predicate from RUN-768.
    bad_input_cmd = (
        "python -c \"import sys; exec(open('primes.py').read()) if input('check?') else None\""
    )
    bad_plan = action_step(
        "submit_plan",
        {
            "summary": "bad input-gated predicate",
            "steps": [
                {
                    "title": "Verify primes with input gate",
                    "done_condition": {
                        "kind": "command",
                        "cmd": bad_input_cmd,
                        "expect_exit": 0,
                    },
                }
            ],
        },
    )
    agent = ScriptedAgent([bad_plan])
    loop, store = build_plan_loop(
        agent,
        conversation_id="pw-h368-input-plan-owned",
        executor=BuildExecutor(),
    )
    await loop.send_message("verify the primes")
    await loop.run()

    events = await store.get_events("pw-h368-input-plan-owned")
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert len(plans) == 1
    assert plans[0].summary == "bad input-gated predicate"
    assert [s.done_condition for s in plans[0].steps] == [
        CommandExitPredicate(cmd=bad_input_cmd, expect_exit=0)
    ]
    await loop.approve_plan()
    assert await store.get_external_dod_spec("pw-h368-input-plan-owned") is None
    events = await store.get_events("pw-h368-input-plan-owned")
    approved = signals.latest_approved_plan(events)
    assert approved is not None and approved.id == plans[0].id
    transition = next(
        event.plan_verification_transition
        for event in events
        if isinstance(event, StatusEvent) and event.detail == "plan_approved"
    )
    assert transition is not None
    assert transition.new_plan_event_id == plans[0].id
    assert transition.external_predicate_fingerprints == []
