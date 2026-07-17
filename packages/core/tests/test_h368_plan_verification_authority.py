"""H368 authority split, replan recovery, and durable audit regressions."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from disco.core import (
    ConversationStatus,
    DoDSpec,
    PlanEvent,
    PlanVerificationTransition,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.dod import CommandExitPredicate, FileExistsPredicate, predicate_fingerprints
from disco.core.dod_evaluator import DoDEvaluator
from disco.core.llm import OperatingMode
from disco.core.loop import signals
from disco.core.loop.control import Disp
from disco.core.loop.finish.common import _plan_file_exists_paths
from loop_fakes import ScriptedAgent, action_step, build_loop

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
        predicate=FileExistsPredicate(path="new-plan-only.txt"),
    )

    persisted = await store.get_external_dod_spec(loop.conversation_id)
    assert persisted is not None and persisted.model_dump_json() == original
    events = await store.get_events(loop.conversation_id)
    current = signals.latest_approved_plan(events)
    assert current is not None and current.id == new.id
    assert [step.done_condition for step in current.steps] == [
        FileExistsPredicate(path="new-plan-only.txt")
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
        [FileExistsPredicate(path="new-plan-only.txt")]
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
                    "done_condition": {"kind": "file_exists", "path": "unapproved.txt"},
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
        conversation_id="h368-run-768",
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
    old = await _approve(loop, summary="RUN-768 bad verifier", predicate=bad)

    started = time.monotonic()
    assert await loop._finish_dod_gate_passed() is False
    assert time.monotonic() - started < 2.0
    events = await store.get_events(loop.conversation_id)
    failure = next(
        event.plan_verifier_failure
        for event in events
        if isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
    )
    assert failure.plan_event_id == old.id
    assert failure.authority == "plan"
    assert failure.replan_allowed is True
    assert await store.get_external_dod_spec(loop.conversation_id) is None

    replacement = await _approve(
        loop,
        summary="finite replacement",
        predicate=CommandExitPredicate(cmd="test -s primes.py"),
    )
    assert await loop._finish_dod_gate_passed() is True
    events = await store.get_events(loop.conversation_id)
    assert signals.latest_approved_plan(events).id == replacement.id  # type: ignore[union-attr]
    assert any(isinstance(event, PlanEvent) and event.id == old.id for event in events)
    assert any(
        isinstance(event, StatusEvent)
        and event.plan_verifier_failure is not None
        and event.plan_verifier_failure.plan_event_id == old.id
        for event in events
    )

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

    assert await loop._finish_dod_gate_passed() is False
    assert await loop._finish_dod_gate_passed() is False
    assert loop.mode is OperatingMode.PLANNING

    await _approve(loop, summary="unchanged replacement", predicate=bad)
    assert await loop._finish_dod_gate_passed() is False
    events = await store.get_events(loop.conversation_id)
    assert any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.STUCK
        and event.detail == "plan_verifier_replan_exhausted"
        for event in events
    )
    failures = [
        event.plan_verifier_failure
        for event in events
        if isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
    ]
    assert len(failures) == 3
    assert failures[-1].approvals_with_same_predicates == 2
    assert failures[-1].replan_allowed is False


async def test_unchanged_plan_retry_bound_survives_alternating_failure_payloads(
    tmp_path: Path,
) -> None:
    loop, _store = build_loop(
        ScriptedAgent([]),
        conversation_id="h368-alternating-failures",
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    predicates = [
        FileExistsPredicate(path="first.txt"),
        FileExistsPredicate(path="second.txt"),
    ]
    await _approve_predicates(loop, summary="two checks", predicates=predicates)

    assert await loop._finish_dod_gate_passed() is False
    (tmp_path / "first.txt").write_text("now present\n")
    # The failure payload changed from two unmet checks to one, but the approved
    # verifier set did not. The second attempt must still force re-planning.
    assert await loop._finish_dod_gate_passed() is False
    assert loop.mode is OperatingMode.PLANNING
