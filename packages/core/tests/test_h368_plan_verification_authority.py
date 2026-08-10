"""H368 authority split, replan recovery, and durable audit regressions."""

from __future__ import annotations

import time
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
    PlanVerificationTransition,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.dod import CommandExitPredicate, FileExistsPredicate, predicate_fingerprints
from disco.core.dod_evaluator import DoDEvaluator
from disco.core.llm import OperatingMode
from disco.core.loop import signals
from disco.core.loop.control import Disp
from disco.core.loop.finish import _plan_file_exists_paths
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop

pytestmark = pytest.mark.asyncio


async def _approve(loop, *, summary: str, predicate) -> PlanEvent:
    return await _approve_predicates(loop, summary=summary, predicates=[predicate])


async def _approve_predicates(loop, *, summary: str, predicates: list) -> PlanEvent:
    events = await loop._events()
    plan = loop._plan_from_args(
        {
            "summary": summary,
            "steps": [
                {
                    "title": f"{summary} {index}",
                    "done_condition": predicate.model_dump(mode="json"),
                }
                for index, predicate in enumerate(predicates, start=1)
            ],
        },
        events,
    )
    await loop._emit(plan)
    await loop._emit(await loop._plan_approval_status(plan, await loop._events()))
    return plan


@pytest.mark.parametrize("authority", ["user", "system", "profile", "harness"])
async def test_external_authority_survives_every_plan_revision_byte_for_byte(
    tmp_path: Path, authority: str
) -> None:
    loop, store = build_loop(ScriptedAgent([]), conversation_id=f"h368-{authority}")
    external = DoDSpec(
        predicates=[FileExistsPredicate(path="required-by-owner.txt")],
        note=f"{authority}-owned",
    )
    original = external.model_dump_json()
    await store.set_dod_spec(loop.conversation_id, external, set_by=authority)

    old = await _approve(
        loop,
        summary="first plan",
        predicate=FileExistsPredicate(path="old-plan-only.txt"),
    )
    new = await _approve(
        loop,
        summary="replacement plan",
        predicate=FileExistsPredicate(path="new-plan-only.txt", renamed_from="old-plan-only.txt"),
    )

    persisted = await store.get_external_dod_spec(loop.conversation_id)
    assert persisted is not None and persisted.model_dump_json() == original
    events = await store.get_events(loop.conversation_id)
    current = signals.latest_approved_plan(events)
    assert current is not None and current.id == new.id
    assert [step.done_condition for step in current.steps] == [
        FileExistsPredicate(path="new-plan-only.txt", renamed_from="old-plan-only.txt")
    ]
    assert any(isinstance(event, PlanEvent) and event.id == old.id for event in events)
    latest_transition = next(
        event.plan_verification_transition
        for event in reversed(events)
        if isinstance(event, StatusEvent) and event.plan_verification_transition is not None
    )
    assert latest_transition.old_plan_event_id == old.id
    assert latest_transition.new_plan_event_id == new.id
    assert latest_transition.old_predicate_fingerprints == predicate_fingerprints(
        [FileExistsPredicate(path="old-plan-only.txt")]
    )
    assert latest_transition.new_predicate_fingerprints == predicate_fingerprints(
        [FileExistsPredicate(path="new-plan-only.txt", renamed_from="old-plan-only.txt")]
    )
    assert latest_transition.external_predicate_fingerprints == predicate_fingerprints(
        external.predicates
    )
    assert _plan_file_exists_paths(events) == ["new-plan-only.txt"]


async def test_failed_replacement_approval_leaves_prior_plan_and_mode_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, store = build_loop(ScriptedAgent([]), conversation_id="h368-failed-replacement")
    approved = await _approve(
        loop,
        summary="approved",
        predicate=FileExistsPredicate(path="approved.txt"),
    )
    proposed = loop._plan_from_args(
        {
            "summary": "not approved",
            "steps": [
                {
                    "title": "not approved",
                    "done_condition": {"kind": "file_exists", "path": "approved.txt"},
                }
            ],
        },
        await store.get_events(loop.conversation_id),
    )
    await loop._emit(proposed)
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
            detail=proposed.id,
        )
    )
    loop.mode = OperatingMode.PLANNING
    original_emit = loop._emit

    async def fail_approval(event):
        if isinstance(event, StatusEvent) and event.detail == "plan_approved":
            raise RuntimeError("injected approval persistence failure")
        return await original_emit(event)

    monkeypatch.setattr(loop, "_emit", fail_approval)
    with pytest.raises(RuntimeError, match="approval persistence failure"):
        await loop.approve_plan()

    events = await store.get_events(loop.conversation_id)
    current = signals.latest_approved_plan(events)
    assert current is not None and current.id == approved.id
    assert loop.mode is OperatingMode.PLANNING


async def test_restart_reconstructs_external_and_current_plan_authority(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    store1 = SqliteEventStore(db)
    loop1, _ = build_loop(ScriptedAgent([]), store=store1, conversation_id="h368-restart")
    external = DoDSpec(predicates=[FileExistsPredicate(path="external.txt")])
    await store1.set_dod_spec(loop1.conversation_id, external, set_by="harness")
    approved = await _approve(
        loop1,
        summary="durable plan",
        predicate=FileExistsPredicate(path="plan.txt"),
    )
    store1.close()

    store2 = SqliteEventStore(db)
    loop2, _ = build_loop(ScriptedAgent([]), store=store2, conversation_id="h368-restart")
    events = await store2.get_events(loop2.conversation_id)
    current = signals.latest_approved_plan(events)
    assert current is not None and current.id == approved.id
    assert await store2.get_external_dod_spec(loop2.conversation_id) == external
    store2.close()


async def test_dangling_approval_evidence_pauses_fail_closed(tmp_path: Path) -> None:
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h368-dangling-approval",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_approved",
            plan_verification_transition=PlanVerificationTransition(
                new_plan_revision=7,
                new_plan_event_id="missing-plan-event",
                new_predicate_fingerprints=predicate_fingerprints(
                    [FileExistsPredicate(path="missing.txt")]
                ),
            ),
        )
    )

    assert await loop._finish_dod_gate_passed() is False
    assert loop._pause_requested.is_set()
    events = await store.get_events(loop.conversation_id)
    assert any(
        isinstance(event, StatusEvent) and event.detail == "plan_verification_evidence_invalid"
        for event in events
    )


async def test_run_768_bad_verifier_fails_closed_but_replan_finishes(tmp_path: Path) -> None:
    (tmp_path / "primes.py").write_text("print('ready')\n")
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h368-advisory-command",
        dod_evaluator_factory=lambda: DoDEvaluator(
            tmp_path,
            command_timeout_seconds=0.25,
        ),
    )
    bad = CommandExitPredicate(
        cmd=(
            "python -c \"import sys; exec(open('primes.py').read()) if input('check?') else None\""
        )
    )
    await _approve(loop, summary="advisory check", predicate=bad)
    await _emit_successful_shell_receipt(loop, "python primes.py")

    started = time.monotonic()
    assert await loop._finish_dod_gate_passed() is True
    assert await loop._finish_dod_gate_passed() is True
    assert time.monotonic() - started < 2.0
    events = await store.get_events(loop.conversation_id)
    assert not any(
        isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
        for event in events
    )
    assert await store.get_external_dod_spec(loop.conversation_id) is None

    normalized, disp = await loop._finish.normalize_finish_step(
        action_step("finish", {"summary": "verified"}),
        events,
    )
    assert disp is Disp.FALLTHROUGH and normalized.finished is True
    assert (
        await loop._finish.finalize_finish(
            normalized,
            await loop.get_state(),
            await store.get_events(loop.conversation_id),
        )
        is Disp.HALT
    )
    assert (await loop.get_state()).execution_status == ConversationStatus.FINISHED


async def test_harness_requirement_cannot_be_removed_or_shadowed_by_plan(
    tmp_path: Path,
) -> None:
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h368-harness-boundary",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    external = DoDSpec(predicates=[FileExistsPredicate(path="harness-required.txt")])
    await store.set_dod_spec(loop.conversation_id, external, set_by="harness")
    await _approve(
        loop,
        summary="model tries a different bar",
        predicate=CommandExitPredicate(cmd="true"),
    )

    assert await loop._finish_dod_gate_passed() is False
    assert await store.get_external_dod_spec(loop.conversation_id) == external
    events = await store.get_events(loop.conversation_id)
    assert not any(
        isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
        for event in events
    ), "external failure must short-circuit before model-owned verification"


async def test_dropping_advisory_plan_condition_cannot_bypass_external_dod(
    tmp_path: Path,
) -> None:
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h368-external-after-plan-drop",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    external = DoDSpec(predicates=[FileExistsPredicate(path="owner-required.txt")])
    await store.set_dod_spec(loop.conversation_id, external, set_by="harness")
    await _approve(
        loop,
        summary="initial layout",
        predicate=FileExistsPredicate(path="speculative-name.txt"),
    )
    replacement = loop._plan_from_args(
        {"summary": "implementation-selected layout", "steps": [{"title": "ship output"}]},
        await loop._events(),
    )
    await loop._emit(replacement)
    await loop._emit(await loop._plan_approval_status(replacement, await loop._events()))

    normalized, disp = await loop._finish.normalize_finish_step(
        action_step("finish", {"summary": "done"}),
        await loop._events(),
    )

    assert normalized.finished is False and disp is Disp.CONTINUE
    assert await store.get_external_dod_spec(loop.conversation_id) == external
    events = await loop._events()
    latest = signals.latest_approved_plan(events)
    assert latest is not None
    assert latest.id == replacement.id
    assert not any(
        isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
        for event in events
    )


async def test_identical_plan_verifier_failure_replans_then_lands_stuck(
    tmp_path: Path,
) -> None:
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h368-bounded-replan",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    bad = CommandExitPredicate(cmd="test -f never-created.txt")
    await _approve(loop, summary="bad verifier", predicate=bad)

    assert await loop._finish_dod_gate_passed() is True
    assert await loop._finish_dod_gate_passed() is True
    assert await loop._finish_dod_gate_passed() is True
    events = await store.get_events(loop.conversation_id)
    assert not any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.STUCK
        for event in events
    )
    assert not any(
        isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
        for event in events
    )
    assert signals.latest_approved_plan(events) is not None


async def test_unchanged_plan_retry_bound_survives_alternating_failure_payloads(
    tmp_path: Path,
) -> None:
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h368-alternating-failures",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    predicates = [
        FileExistsPredicate(path="first.txt"),
        FileExistsPredicate(path="second.txt"),
    ]
    await _approve_predicates(loop, summary="two advisory checks", predicates=predicates)

    assert await loop._finish_dod_gate_passed() is True
    (tmp_path / "first.txt").write_text("now present\n")
    assert await loop._finish_dod_gate_passed() is True
    events = await store.get_events(loop.conversation_id)
    assert not any(
        isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
        for event in events
    )


async def _emit_successful_shell_receipt(loop, command: str) -> ActionEvent:
    action = ActionEvent(
        thought="verified deliverable",
        tool_call=ToolCall(tool_name="shell", arguments={"command": command}),
    )
    await loop._emit(action)
    await loop._emit(
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(
                call_id=action.tool_call.call_id,
                tool_name="shell",
                success=True,
                content="VERIFIED: 20 primes, last is 71",
            ),
        )
    )
    return action


async def test_h567_exact_finish_retry_replans_then_finishes_without_thrash(
    tmp_path: Path,
) -> None:
    """A plan diagnostic cannot reopen finish or duplicate a prior check."""
    (tmp_path / "primes.py").write_text(
        "print('First 20 prime numbers:')\n"
        "print(''.join(f'{i:2}: {p}\\n' for i, p in enumerate("
        "[2,3,5,7,11,13,17,19,23,29,31,37,41,43,47,53,59,61,67,71], 1)), "
        "end='')\n"
    )
    verify_cmd = "cd /workspace && python3 verify_primes.py"
    executor = FakeExecutor()
    loop, store = build_loop(
        ScriptedAgent([]),
        executor=executor,
        conversation_id="h567-exact-flow",
        planning_tools=frozenset({"file_read", "submit_plan"}),
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    bad = CommandExitPredicate(
        cmd=(
            'python3 -c "import subprocess; '
            "out=subprocess.check_output(['python3','primes.py'],text=True); "
            "lines=out.strip().split(); assert len(lines)==20; "
            "assert lines[-1]=='71'\""
        )
    )
    await _approve(loop, summary="advisory verifier", predicate=bad)
    await _emit_successful_shell_receipt(loop, verify_cmd)
    finish_step = action_step(
        "finish",
        {"summary": "verified", "verify": verify_cmd},
    )

    first, first_disp = await loop._finish.normalize_finish_step(
        finish_step, await store.get_events(loop.conversation_id)
    )
    assert first.finished is True and first_disp is Disp.FALLTHROUGH
    assert len(executor.calls) == 0
    assert (
        await loop._finish.finalize_finish(
            first,
            await loop.get_state(),
            await store.get_events(loop.conversation_id),
        )
        is Disp.HALT
    )
    assert (await loop.get_state()).execution_status == ConversationStatus.FINISHED

    events = await store.get_events(loop.conversation_id)
    exact_shell_actions = [
        event
        for event in events
        if isinstance(event, ActionEvent)
        and event.tool_call.tool_name == "shell"
        and event.tool_call.arguments == {"command": verify_cmd}
    ]
    assert len(exact_shell_actions) == 1
    assert any(
        isinstance(event, MessageEvent)
        and event.meta.get("finish_verify_receipt_reused") is True
        for event in events
    )
    assert not any(
        isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
        for event in events
    )


async def test_h567_user_revision_cannot_inherit_verifier_repair_bypass(
    tmp_path: Path,
) -> None:
    """Changing advisory checks never creates privileged verifier-repair mode."""

    (tmp_path / "ready.txt").write_text("ready\n")
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h567-user-revision",
        planning_tools=frozenset({"file_read", "submit_plan"}),
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    await _approve(
        loop,
        summary="bad verifier",
        predicate=FileExistsPredicate(path="missing.txt"),
    )
    await _emit_successful_shell_receipt(loop, "test -s ready.txt")
    finish_step = action_step("finish", {"summary": "ready"})

    normalized, disp = await loop._finish.normalize_finish_step(
        finish_step, await store.get_events(loop.conversation_id)
    )
    assert normalized.finished is True and disp is Disp.FALLTHROUGH

    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Also change the product copy."),
        )
    )
    await _approve(
        loop,
        summary="Change product copy",
        predicate=FileExistsPredicate(path="ready.txt", renamed_from="missing.txt"),
    )

    events = await store.get_events(loop.conversation_id)
    transition = next(
        event.plan_verification_transition
        for event in reversed(events)
        if isinstance(event, StatusEvent) and event.plan_verification_transition is not None
    )
    assert transition.reason == "approved_plan_revision"
    assert not signals.verifier_repair_execution_active(events)

    loop.mode = OperatingMode.LONG_HORIZON
    assert await loop._finish.gate_execution_nudge(finish_step, events) is Disp.CONTINUE
    events = await store.get_events(loop.conversation_id)
    assert any(
        isinstance(event, MessageEvent)
        and "approved plan has not been executed" in event.message.content.lower()
        for event in events
    )


async def test_h567_verifier_repair_refusal_cannot_harvest_stale_user_plan(
    tmp_path: Path,
) -> None:
    """An unmet model check cannot switch the loop into a repair authority mode."""

    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id="h567-no-repair-state",
        planning_tools=frozenset({"file_read", "submit_plan"}),
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Build the original site."),
        )
    )
    approved = await _approve(
        loop,
        summary="Build original site",
        predicate=FileExistsPredicate(path="missing-verifier.txt"),
    )
    assert await loop._finish_dod_gate_passed() is True
    assert await loop._finish_dod_gate_passed() is True

    events = await store.get_events(loop.conversation_id)
    latest = signals.latest_approved_plan(events)
    assert latest is not None
    assert latest.id == approved.id
    assert not signals.verifier_repair_execution_active(events)
    assert loop.mode is not OperatingMode.PLANNING
    assert not any(
        isinstance(event, StatusEvent)
        and (
            event.plan_verifier_failure is not None
            or event.detail == "harvested_revision_plan"
        )
        for event in events
    )


async def test_h567_unchanged_replacement_stucks_before_optional_verify_repeat(
    tmp_path: Path,
) -> None:
    verify_cmd = "python3 verify_primes.py"
    executor = FakeExecutor()
    loop, store = build_loop(
        ScriptedAgent([]),
        executor=executor,
        conversation_id="h567-unchanged-replacement",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    bad = CommandExitPredicate(cmd="test -f never-created.txt")
    await _approve(loop, summary="bad verifier", predicate=bad)
    finish_step = action_step("finish", {"summary": "done", "verify": verify_cmd})

    first, first_disp = await loop._finish.normalize_finish_step(
        finish_step, await store.get_events(loop.conversation_id)
    )
    assert first.finished is True and first_disp is Disp.FALLTHROUGH
    assert len(executor.calls) == 1

    second, second_disp = await loop._finish.normalize_finish_step(
        finish_step, await store.get_events(loop.conversation_id)
    )
    assert second.finished is True and second_disp is Disp.FALLTHROUGH
    assert len(executor.calls) == 1
    events = await store.get_events(loop.conversation_id)
    assert not any(
        isinstance(event, StatusEvent)
        and (
            event.status == ConversationStatus.STUCK
            or event.plan_verifier_failure is not None
        )
        for event in events
    )


async def test_h567_trusted_mutation_preserves_normal_verify_then_dod_retry(
    tmp_path: Path,
) -> None:
    verify_cmd = "python3 verify_primes.py"
    executor = FakeExecutor()
    loop, store = build_loop(
        ScriptedAgent([]),
        executor=executor,
        conversation_id="h567-trusted-mutation",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    await _approve(
        loop,
        summary="bad verifier",
        predicate=CommandExitPredicate(cmd="test -f never-created.txt"),
    )
    finish_step = action_step("finish", {"summary": "done", "verify": verify_cmd})
    initial, initial_disp = await loop._finish.normalize_finish_step(
        finish_step, await store.get_events(loop.conversation_id)
    )
    assert initial.finished is True and initial_disp is Disp.FALLTHROUGH
    assert len(executor.calls) == 1

    mutation = ActionEvent(
        thought="repair deliverable",
        tool_call=ToolCall(
            tool_name="file_write",
            arguments={"path": "repair.txt", "content": "fixed"},
        ),
    )
    await loop._emit(mutation)
    await loop._emit(
        ObservationEvent(
            action_id=mutation.id,
            tool_result=ToolResult(
                call_id=mutation.tool_call.call_id,
                tool_name="file_write",
                success=True,
                content="wrote repair.txt",
                structured={
                    "path": "repair.txt",
                    "sha256": "f" * 64,
                },
            ),
        )
    )

    retried, retried_disp = await loop._finish.normalize_finish_step(
        finish_step, await store.get_events(loop.conversation_id)
    )
    assert retried.finished is True and retried_disp is Disp.FALLTHROUGH
    assert len(executor.calls) == 2
    events = await store.get_events(loop.conversation_id)
    assert (
        len(
            [
                event
                for event in events
                if isinstance(event, ActionEvent)
                and event.tool_call.tool_name == "shell"
                and event.tool_call.arguments == {"command": verify_cmd}
            ]
        )
        == 2
    )
    assert not any(
        isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
        for event in events
    )
