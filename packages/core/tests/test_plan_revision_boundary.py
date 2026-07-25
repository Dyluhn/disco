"""Durable plan-revision authority, weakening, and idempotence contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    DoDSpec,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    PlanVerificationTransition,
    PlanVerifierFailure,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.dod import (
    CommandExitPredicate,
    FileExistsPredicate,
    predicate_fingerprint,
    predicate_fingerprints,
)
from disco.core.loop import signals
from disco.core.loop.control import Disp
from disco.core.loop.plan_revisions import (
    IDEMPOTENT_PLAN_DETAIL,
    normalized_execution_contract,
)
from disco.core.loop.tool_specs import _PROPOSE_PLAN_UPDATE_SCHEMA
from disco.core.loop.view_render import ViewBuilder
from disco.core.view import effective_plan_progress
from event_fakes import with_seqs
from loop_fakes import ScriptedAgent, action_step, build_loop


def _step(title: str, path: str | None, *, renamed_from: str | None = None) -> dict:
    step: dict = {"title": title, "detail": f"Deliver {title}"}
    if path is not None:
        condition: dict = {"kind": "file_exists", "path": path}
        if renamed_from is not None:
            condition["renamed_from"] = renamed_from
        step["done_condition"] = condition
    return step


async def _approve_initial(loop, *, paths: list[str]) -> PlanEvent:
    plan = loop._plan_from_args(
        {
            "summary": "Initial contract",
            "steps": [_step(f"step {index}", path) for index, path in enumerate(paths, 1)],
        },
        await loop._events(),
    )
    await loop._emit(plan)
    await loop._emit(await loop._plan_approval_status(plan, await loop._events()))
    return plan


def _failure(old: PlanEvent, failed: FileExistsPredicate) -> StatusEvent:
    all_predicates = [step.done_condition for step in old.steps if step.done_condition is not None]
    return StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_verification_failed",
        plan_verifier_failure=PlanVerifierFailure(
            plan_revision=old.revision,
            plan_event_id=old.id,
            predicate_fingerprints=predicate_fingerprints(all_predicates),
            failed_predicate_fingerprints=[predicate_fingerprint(failed)],
            spec_fingerprint="sha256:spec",
            failure_fingerprint="sha256:failure",
            failure_kinds=["file_exists"],
            attempt_for_approved_plan=1,
            approvals_with_same_predicates=1,
        ),
    )


def test_propose_schema_and_normalized_contract_cover_typed_revision_fields() -> None:
    condition = _PROPOSE_PLAN_UPDATE_SCHEMA["properties"]["steps"]["items"]["properties"][
        "done_condition"
    ]
    assert {branch["properties"]["kind"]["const"] for branch in condition["oneOf"]} == {
        "file_exists",
        "command",
        "http_ok",
    }
    file_branch = next(
        branch
        for branch in condition["oneOf"]
        if branch["properties"]["kind"]["const"] == "file_exists"
    )
    assert "renamed_from" in file_branch["properties"]

    renamed = PlanEvent(
        summary="ignored summary",
        context="ignored context",
        steps=[
            PlanStep(
                title="Build",
                detail="Exact detail",
                done_condition=FileExistsPredicate(path="new.txt", renamed_from="old.txt"),
            )
        ],
        revision=2,
    )
    consummated = renamed.model_copy(
        update={
            "summary": "different summary",
            "context": "different context",
            "steps": [
                renamed.steps[0].model_copy(
                    update={"done_condition": FileExistsPredicate(path="new.txt")}
                )
            ],
        }
    )
    assert normalized_execution_contract(renamed) == normalized_execution_contract(consummated)
    assert normalized_execution_contract(renamed) != normalized_execution_contract(
        consummated.model_copy(
            update={"steps": [consummated.steps[0].model_copy(update={"detail": "Changed"})]}
        )
    )


@pytest.mark.asyncio
async def test_failure_revision_approval_then_duplicate_redirects_without_a_gate() -> None:
    """The composed raw failure→revision→approval→duplicate lifecycle."""
    loop, store = build_loop(ScriptedAgent([]), conversation_id="plan-revision-lifecycle")
    external = DoDSpec(predicates=[FileExistsPredicate(path="owner-required.txt")])
    await store.set_dod_spec(loop.conversation_id, external, set_by="harness")
    old = await _approve_initial(loop, paths=["old-a.txt", "keep-b.txt"])

    failure = _failure(old, FileExistsPredicate(path="old-a.txt"))
    failure_message = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="old-a.txt is still missing; revise the plan"),
        meta={"blocking": "plan_verifier_failed", "failure_fingerprint": "sha256:failure"},
    )
    external_message = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="owner-required.txt is still required"),
        meta={"blocking": "dod_unmet"},
    )
    await loop._emit(failure)
    await loop._emit(failure_message)
    await loop._emit(external_message)

    revised_args = {
        "summary": "Repair the failed path",
        "steps": [
            _step("step 1", "new-a.txt", renamed_from="old-a.txt"),
            _step("step 2", "keep-b.txt"),
        ],
    }
    assert (
        await loop._meta.handle_propose_plan_update(
            action_step("propose_plan_update", revised_args), await loop._events()
        )
        is Disp.HALT
    )
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    await loop.approve_plan()

    after_approval = await loop._events()
    plans_after_approval = [event for event in after_approval if isinstance(event, PlanEvent)]
    approvals_after_approval = [
        event
        for event in after_approval
        if isinstance(event, StatusEvent) and event.detail == "plan_approved"
    ]
    assert len(plans_after_approval) == 2
    assert len(approvals_after_approval) == 2  # initial + exactly one replacement approval
    assert approvals_after_approval[-1].plan_verification_transition is not None
    assert approvals_after_approval[-1].plan_verification_transition.reason == (
        "approved_plan_revision"
    )
    assert await store.get_external_dod_spec(loop.conversation_id) == external

    duplicate_args = {
        "summary": "Cosmetic summary change",
        "context": "Cosmetic context change",
        "steps": [_step("step 1", "new-a.txt"), _step("step 2", "keep-b.txt")],
    }
    assert (
        await loop._meta.handle_propose_plan_update(
            action_step("propose_plan_update", duplicate_args), await loop._events()
        )
        is Disp.CONTINUE
    )
    after_duplicate = await loop._events()
    assert len([event for event in after_duplicate if isinstance(event, PlanEvent)]) == 2
    assert (
        len(
            [
                event
                for event in after_duplicate
                if isinstance(event, StatusEvent) and event.detail == "plan_approved"
            ]
        )
        == 2
    )
    assert any(
        isinstance(event, StatusEvent) and event.detail == IDEMPOTENT_PLAN_DETAIL
        for event in after_duplicate
    )
    assert any(
        isinstance(event, MessageEvent)
        and event.meta.get("diagnostic") == "identical_plan_redirect"
        and "Continue execution now" in event.message.content
        for event in after_duplicate
    )
    assert (await loop.get_state()).execution_status == ConversationStatus.RUNNING

    view = await ViewBuilder(loop).build(after_duplicate)
    rendered = "\n".join(message.content for message in view.messages)
    assert failure_message.message.content not in rendered
    assert external_message.message.content in rendered
    assert "new-a.txt" in rendered
    assert not signals.verifier_repair_execution_active(after_duplicate)

    # Identical bytes become a legitimate revision after new USER evidence.
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Apply a new product instruction."),
        )
    )
    assert (
        await loop._meta.handle_propose_plan_update(
            action_step("propose_plan_update", duplicate_args), await loop._events()
        )
        is Disp.HALT
    )
    changed_scope = await loop._events()
    assert len([event for event in changed_scope if isinstance(event, PlanEvent)]) == 3
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


@pytest.mark.asyncio
async def test_eight_to_zero_and_malformed_revisions_are_recoverably_blocked() -> None:
    loop, _store = build_loop(ScriptedAgent([]), conversation_id="plan-revision-weakening")
    paths = [f"required-{index}.txt" for index in range(8)]
    await _approve_initial(loop, paths=paths)
    weak_args = {
        "summary": "Silently empty the verifier",
        "steps": [_step(f"step {index}", None) for index in range(1, 9)],
    }
    for _ in range(2):
        assert (
            await loop._meta.handle_propose_plan_update(
                action_step("propose_plan_update", weak_args), await loop._events()
            )
            is Disp.CONTINUE
        )
    events = await loop._events()
    assert len([event for event in events if isinstance(event, PlanEvent)]) == 1
    assert not any(
        isinstance(event, StatusEvent) and event.status == ConversationStatus.STUCK
        for event in events
    )
    guidance = [
        event.message.content
        for event in events
        if isinstance(event, MessageEvent)
        and event.meta.get("blocking") == "plan_predicate_weakening"
    ]
    assert guidance and paths[0] in guidance[-1] and paths[-1] in guidance[-1]

    malformed = {
        "summary": "Malformed typed condition",
        "steps": [{"title": "step 1", "done_condition": "file_exists required-0.txt"}],
    }
    assert (
        await loop._meta.handle_propose_plan_update(
            action_step("propose_plan_update", malformed), await loop._events()
        )
        is Disp.CONTINUE
    )
    events = await loop._events()
    assert len([event for event in events if isinstance(event, PlanEvent)]) == 1
    assert any(
        isinstance(event, StatusEvent) and event.detail == "invalid_plan_done_conditions"
        for event in events
    )


@pytest.mark.asyncio
async def test_first_failed_command_may_be_replaced_once_without_bypass_or_scope_loss() -> None:
    """A durable failure can request approval before the two-failure bypass bar."""
    loop, store = build_loop(ScriptedAgent([]), conversation_id="first-command-repair")
    external = DoDSpec(predicates=[FileExistsPredicate(path="owner-required.txt")])
    await store.set_dod_spec(loop.conversation_id, external, set_by="harness")
    retained = FileExistsPredicate(path="artifact.txt")
    failed = CommandExitPredicate(cmd="host-specific-check artifact.txt")
    old = loop._plan_from_args(
        {
            "summary": "Initial contract",
            "steps": [
                {
                    "title": "Build artifact",
                    "done_condition": retained.model_dump(mode="json"),
                },
                {
                    "title": "Verify artifact",
                    "done_condition": failed.model_dump(mode="json"),
                },
            ],
        },
        await loop._events(),
    )
    await loop._emit(old)
    await loop._emit(await loop._plan_approval_status(old, await loop._events()))

    productive = ActionEvent(
        thought="build it",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "touch artifact.txt"}),
    )
    await loop._emit(productive)
    await loop._emit(
        ObservationEvent(
            action_id=productive.id,
            tool_result=ToolResult(
                call_id=productive.tool_call.call_id,
                tool_name="shell",
                success=True,
                content="built",
            ),
        )
    )
    failure = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_verification_failed",
        plan_verifier_failure=PlanVerifierFailure(
            plan_revision=old.revision,
            plan_event_id=old.id,
            predicate_fingerprints=predicate_fingerprints([retained, failed]),
            failed_predicate_fingerprints=[predicate_fingerprint(failed)],
            spec_fingerprint="sha256:spec",
            failure_fingerprint="sha256:first-command-failure",
            failure_kinds=["command"],
            attempt_for_approved_plan=1,
            approvals_with_same_predicates=1,
            replan_allowed=True,
        ),
    )
    await loop._emit(failure)

    replacement = CommandExitPredicate(cmd="portable-check artifact.txt")
    revised_args = {
        "summary": "Use the portable verifier",
        "steps": [
            {
                "title": "Build artifact",
                "done_condition": retained.model_dump(mode="json"),
            },
            {
                "title": "Verify artifact portably",
                "done_condition": replacement.model_dump(mode="json"),
            },
        ],
    }
    assert (
        await loop._meta.handle_propose_plan_update(
            action_step("propose_plan_update", revised_args), await loop._events()
        )
        is Disp.HALT
    )
    await loop.approve_plan()

    events = await loop._events()
    assert len([event for event in events if isinstance(event, PlanEvent)]) == 2
    transition = next(
        event.plan_verification_transition
        for event in reversed(events)
        if isinstance(event, StatusEvent) and event.plan_verification_transition is not None
    )
    assert transition.reason == "approved_plan_revision"
    assert transition.old_predicate_fingerprints == predicate_fingerprints([retained, failed])
    assert transition.new_predicate_fingerprints == predicate_fingerprints([retained, replacement])
    assert not signals.verifier_repair_execution_active(events)
    assert await store.get_external_dod_spec(loop.conversation_id) == external


@pytest.mark.asyncio
async def test_first_failure_cannot_replace_an_unfailed_predicate_at_same_width() -> None:
    loop, _store = build_loop(ScriptedAgent([]), conversation_id="scoped-command-repair")
    retained = FileExistsPredicate(path="artifact.txt")
    failed = CommandExitPredicate(cmd="bad-check artifact.txt")
    old = loop._plan_from_args(
        {
            "summary": "Initial contract",
            "steps": [
                {"title": "Build", "done_condition": retained.model_dump(mode="json")},
                {"title": "Verify", "done_condition": failed.model_dump(mode="json")},
            ],
        },
        await loop._events(),
    )
    await loop._emit(old)
    await loop._emit(await loop._plan_approval_status(old, await loop._events()))
    productive = ActionEvent(
        thought="build it",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "touch artifact.txt"}),
    )
    await loop._emit(productive)
    await loop._emit(
        ObservationEvent(
            action_id=productive.id,
            tool_result=ToolResult(
                call_id=productive.tool_call.call_id,
                tool_name="shell",
                success=True,
                content="built",
            ),
        )
    )
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_verification_failed",
            plan_verifier_failure=PlanVerifierFailure(
                plan_revision=old.revision,
                plan_event_id=old.id,
                predicate_fingerprints=predicate_fingerprints([retained, failed]),
                failed_predicate_fingerprints=[predicate_fingerprint(failed)],
                spec_fingerprint="sha256:spec",
                failure_fingerprint="sha256:scoped-command-failure",
                failure_kinds=["command"],
                attempt_for_approved_plan=1,
                approvals_with_same_predicates=1,
                replan_allowed=True,
            ),
        )
    )

    unrelated = {
        "summary": "Replace unrelated scope too",
        "steps": [
            {
                "title": "Move artifact without an audited rename",
                "done_condition": {"kind": "file_exists", "path": "other.txt"},
            },
            {
                "title": "Portable verifier",
                "done_condition": {"kind": "command", "cmd": "portable-check artifact.txt"},
            },
        ],
    }
    assert (
        await loop._meta.handle_propose_plan_update(
            action_step("propose_plan_update", unrelated), await loop._events()
        )
        is Disp.CONTINUE
    )
    events = await loop._events()
    assert len([event for event in events if isinstance(event, PlanEvent)]) == 1
    assert any(
        isinstance(event, StatusEvent) and event.detail == "plan_predicate_weakening_blocked"
        for event in events
    )


@pytest.mark.asyncio
async def test_strict_appkit_revision_uses_the_initial_plan_validator() -> None:
    loop, _store = build_loop(
        ScriptedAgent([]),
        conversation_id="plan-revision-appkit",
        strict_appkit_active=lambda: True,
    )
    await _approve_initial(loop, paths=[])
    args = {"summary": "Guess generated output", "steps": [_step("generated", "src/app.tsx")]}
    assert (
        await loop._meta.handle_propose_plan_update(
            action_step("propose_plan_update", args), await loop._events()
        )
        is Disp.CONTINUE
    )
    events = await loop._events()
    assert len([event for event in events if isinstance(event, PlanEvent)]) == 1
    assert any(
        isinstance(event, MessageEvent) and "canonical strict AppKit" in event.message.content
        for event in events
    )


def test_retirement_requires_actual_failed_fingerprint_to_leave_new_authority() -> None:
    failed = FileExistsPredicate(path="failed.txt")
    retained = FileExistsPredicate(path="retained.txt")
    old = PlanEvent(
        summary="old",
        steps=[
            PlanStep(title="failed", done_condition=failed),
            PlanStep(title="retained", done_condition=retained),
        ],
        revision=1,
    )
    failure = _failure(old, failed)
    reminder = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="failed verifier"),
        meta={"blocking": "plan_verifier_failed", "failure_fingerprint": "sha256:failure"},
    )
    initial = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_approved",
        plan_verification_transition=PlanVerificationTransition(
            new_plan_revision=old.revision,
            new_plan_event_id=old.id,
            new_predicate_fingerprints=predicate_fingerprints([failed, retained]),
        ),
    )

    renamed = FileExistsPredicate(path="replacement.txt", renamed_from="failed.txt")
    replacement = PlanEvent(
        summary="replacement",
        steps=[
            PlanStep(title="failed", done_condition=renamed),
            PlanStep(title="retained", done_condition=retained),
        ],
        revision=2,
    )
    replacement_approval = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_approved",
        plan_verification_transition=PlanVerificationTransition(
            old_plan_revision=old.revision,
            old_plan_event_id=old.id,
            old_predicate_fingerprints=predicate_fingerprints([failed, retained]),
            new_plan_revision=replacement.revision,
            new_plan_event_id=replacement.id,
            new_predicate_fingerprints=predicate_fingerprints([renamed, retained]),
        ),
    )
    renamed_events = with_seqs([old, initial, failure, reminder, replacement, replacement_approval])
    assert reminder.id in signals.superseded_plan_owned_failure(renamed_events)

    additive = PlanEvent(
        summary="additive",
        steps=[*old.steps, PlanStep(title="extra", done_condition=FileExistsPredicate(path="x"))],
        revision=2,
    )
    additive_approval = replacement_approval.model_copy(
        update={
            "plan_verification_transition": PlanVerificationTransition(
                old_plan_revision=old.revision,
                old_plan_event_id=old.id,
                old_predicate_fingerprints=predicate_fingerprints([failed, retained]),
                new_plan_revision=additive.revision,
                new_plan_event_id=additive.id,
                new_predicate_fingerprints=predicate_fingerprints(
                    [failed, retained, FileExistsPredicate(path="x")]
                ),
            )
        }
    )
    additive_events = with_seqs([old, initial, failure, reminder, additive, additive_approval])
    assert reminder.id not in signals.superseded_plan_owned_failure(additive_events)


def test_completion_carries_only_for_an_exact_approved_same_index_contract() -> None:
    first = PlanEvent(
        summary="first",
        steps=[PlanStep(title="Build", done_condition=FileExistsPredicate(path="a.txt"))],
        revision=1,
    )
    first_approval = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_approved",
        plan_verification_transition=PlanVerificationTransition(
            new_plan_revision=1,
            new_plan_event_id=first.id,
            new_predicate_fingerprints=predicate_fingerprints([FileExistsPredicate(path="a.txt")]),
        ),
    )
    done = action_step("plan_step", {"index": 1, "state": "done"}).tool_call
    assert done is not None
    from disco.core import ActionEvent

    done_event = ActionEvent(thought="done", tool_call=done)

    exact = first.model_copy(update={"id": "evt_exact", "revision": 2})
    exact_approval = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_approved",
        plan_verification_transition=PlanVerificationTransition(
            old_plan_revision=1,
            old_plan_event_id=first.id,
            old_predicate_fingerprints=predicate_fingerprints([FileExistsPredicate(path="a.txt")]),
            new_plan_revision=2,
            new_plan_event_id=exact.id,
            new_predicate_fingerprints=predicate_fingerprints([FileExistsPredicate(path="a.txt")]),
        ),
    )
    _, exact_states = effective_plan_progress(
        with_seqs([first, first_approval, done_event, exact, exact_approval])
    )
    assert exact_states == {1: "done"}

    changed = exact.model_copy(
        update={
            "id": "evt_changed",
            "steps": [PlanStep(title="Build", done_condition=FileExistsPredicate(path="b.txt"))],
        }
    )
    changed_approval = exact_approval.model_copy(
        update={
            "plan_verification_transition": exact_approval.plan_verification_transition.model_copy(  # type: ignore[union-attr]
                update={
                    "new_plan_event_id": changed.id,
                    "new_predicate_fingerprints": predicate_fingerprints(
                        [FileExistsPredicate(path="b.txt")]
                    ),
                }
            )
        }
    )
    _, changed_states = effective_plan_progress(
        with_seqs([first, first_approval, done_event, changed, changed_approval])
    )
    assert changed_states == {}


@pytest.mark.asyncio
async def test_idempotent_authority_reconstructs_after_sqlite_restart(tmp_path: Path) -> None:
    db = Path(tmp_path) / "revision.db"
    store1 = SqliteEventStore(db)
    loop1, _ = build_loop(ScriptedAgent([]), store=store1, conversation_id="plan-revision-restart")
    await _approve_initial(loop1, paths=["same.txt"])
    store1.close()

    store2 = SqliteEventStore(db)
    loop2, _ = build_loop(ScriptedAgent([]), store=store2, conversation_id="plan-revision-restart")
    before = await loop2._events()
    assert (
        await loop2._meta.handle_propose_plan_update(
            action_step(
                "propose_plan_update",
                {"summary": "same", "steps": [_step("step 1", "same.txt")]},
            ),
            before,
        )
        is Disp.CONTINUE
    )
    after = await loop2._events()
    assert len([event for event in after if isinstance(event, PlanEvent)]) == 1
    assert not any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.AWAITING_PLAN_APPROVAL
        and (event.seq or 0) > max(event.seq or 0 for event in before)
        for event in after
    )
    store2.close()


# ---- a command predicate the agent PROVED is broken can be replaced -----------


def _approved(plan: PlanEvent, predicates) -> StatusEvent:
    return StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="plan_approved",
        plan_verification_transition=PlanVerificationTransition(
            new_plan_revision=plan.revision,
            new_plan_event_id=plan.id,
            new_predicate_fingerprints=predicate_fingerprints(predicates),
        ),
    )


def _ran_and_failed(command: str) -> list:
    """The agent executing `command` itself and seeing it fail."""
    act = ActionEvent(
        thought="run my own acceptance command",
        tool_call=ToolCall(call_id="c1", tool_name="shell", arguments={"command": command}),
    )
    return [
        act,
        ObservationEvent(
            action_id=act.id,
            tool_result=ToolResult(
                call_id="c1",
                tool_name="shell",
                success=False,
                content="SyntaxError: invalid syntax",
            ),
        ),
    ]


def _productive_write() -> list:
    act = ActionEvent(
        thought="write the working verifier",
        tool_call=ToolCall(
            call_id="c2", tool_name="file_write", arguments={"path": "verify_app.py"}
        ),
    )
    return [
        act,
        ObservationEvent(
            action_id=act.id,
            tool_result=ToolResult(
                call_id="c2",
                tool_name="file_write",
                success=True,
                content="wrote 703 bytes",
            ),
        ),
    ]


def test_command_predicate_the_agent_proved_broken_may_be_replaced() -> None:
    """A command acceptance condition is authored by the agent, so it can be
    wrong — and the agent discovers that by RUNNING it, not by waiting for the
    plan verifier. Both prior escapes keyed on a `plan_verifier_failure`, so a
    predicate the verifier never ran had no legal repair path at all.

    Counted seed 440028 authored an invalid `python3 -c` one-liner as a
    done-condition, ran it, got a SyntaxError, wrote a working verifier that
    proved the required strings were rendered, and was refused SIXTEEN times when
    it tried to swap the broken condition for the working one.
    """
    from disco.core.loop.plan_revisions import assert_plan_revision_approvable

    broken = CommandExitPredicate(cmd='python3 -c "async def main(): async with x"')
    working = CommandExitPredicate(cmd="python3 /workspace/verify_app.py")
    old = PlanEvent(
        summary="old",
        steps=[PlanStep(title="verify", done_condition=broken)],
        revision=1,
    )
    candidate = PlanEvent(
        summary="repaired",
        steps=[PlanStep(title="verify", done_condition=working)],
        revision=2,
    )
    events = with_seqs(
        [old, _approved(old, [broken]), *_ran_and_failed(broken.cmd), *_productive_write()]
    )

    assert_plan_revision_approvable(events, candidate)  # must not raise


def test_command_predicate_never_run_may_not_be_replaced() -> None:
    """Negative control: without demonstrated failure this is still weakening."""
    from disco.core.loop.plan_revisions import (
        PlanRevisionWeakeningError,
        assert_plan_revision_approvable,
    )

    broken = CommandExitPredicate(cmd='python3 -c "async def main(): async with x"')
    working = CommandExitPredicate(cmd="python3 /workspace/verify_app.py")
    old = PlanEvent(
        summary="old", steps=[PlanStep(title="verify", done_condition=broken)], revision=1
    )
    candidate = PlanEvent(
        summary="repaired", steps=[PlanStep(title="verify", done_condition=working)], revision=2
    )
    events = with_seqs([old, _approved(old, [broken]), *_productive_write()])

    with pytest.raises(PlanRevisionWeakeningError):
        assert_plan_revision_approvable(events, candidate)


def test_demonstrated_failure_does_not_license_dropping_other_conditions() -> None:
    """Negative control: the escape is one-for-one. Proving ONE command broken
    must not let an unrelated acceptance condition disappear with it."""
    from disco.core.loop.plan_revisions import (
        PlanRevisionWeakeningError,
        assert_plan_revision_approvable,
    )

    broken = CommandExitPredicate(cmd='python3 -c "async def main(): async with x"')
    unrelated = FileExistsPredicate(path="dist/index.html")
    working = CommandExitPredicate(cmd="python3 /workspace/verify_app.py")
    old = PlanEvent(
        summary="old",
        steps=[
            PlanStep(title="verify", done_condition=broken),
            PlanStep(title="build", done_condition=unrelated),
        ],
        revision=1,
    )
    # Drops BOTH, adds only the repaired command — a cardinality reduction.
    candidate = PlanEvent(
        summary="repaired",
        steps=[PlanStep(title="verify", done_condition=working)],
        revision=2,
    )
    events = with_seqs(
        [
            old,
            _approved(old, [broken, unrelated]),
            *_ran_and_failed(broken.cmd),
            *_productive_write(),
        ]
    )

    with pytest.raises(PlanRevisionWeakeningError):
        assert_plan_revision_approvable(events, candidate)
