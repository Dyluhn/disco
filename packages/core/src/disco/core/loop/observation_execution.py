"""Ordered action execution and observation persistence."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

from ..effects import ActionProfile
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    RuntimeConstraintEvent,
    ToolResult,
    find_elided_arg_markers,
    value_is_only_elision_marker,
)
from ..llm import LLMContextWindowExceeded
from .dedup import (
    _W39_NOTICE_TOOLS,
    _W39_PLAN_TOOLS,
    _f8_confirmed_file_writes,
    _f9_dedupable_read,
    _w39_shell_verify_reminder,
)
from .dedup_plan_notice import _w39_plan_progress_reminder
from .observation_dedup import (
    _has_confirmed_prior_append,
    _redirect_marker_write_with_read_provenance,
)

if TYPE_CHECKING:
    from .ports import ConversationModePort, LoopEventPort, ToolExecutionPort

    class _LoopFacet(ConversationModePort, LoopEventPort, ToolExecutionPort, Protocol):
        """The loop capability this module uses: mode, the event log, tools."""

_LOG = logging.getLogger("disco.loop")
_ERROR_DETAIL_CAP = 600
_DELIVERED_DETAIL_CAP = 80_000


async def _persist_runtime_constraints(loop: _LoopFacet, result: ToolResult) -> None:
    """Persist non-transient host constraints beside the primary observation."""
    for declaration in result.runtime_constraints:
        if declaration.transient:
            continue
        await loop._emit(
            RuntimeConstraintEvent(
                constraint_key=declaration.constraint_key,
                scope=declaration.scope,
                guidance=declaration.guidance,
                alternative=declaration.alternative,
                capability_generation=declaration.capability_generation,
            )
        )


def _error_detail(
    err: str,
    content: str | None,
    *,
    carries_delivery: bool = False,
) -> str | None:
    value = (content or "").strip()
    cap = _DELIVERED_DETAIL_CAP if carries_delivery else _ERROR_DETAIL_CAP
    return value[:cap] if value and value != err else None


def _ground_read(loop: _LoopFacet, path: str) -> None:
    """Tell a compatible executor that current bytes were already grounded."""
    note = getattr(getattr(loop, "executor", None), "note_grounding_read", None)
    if not callable(note) or not isinstance(path, str) or not path:
        return
    try:
        note(path)
    except Exception:  # noqa: BLE001
        _LOG.debug("note_grounding_read failed for %s", path, exc_info=True)


def _action_profile_for_call(
    loop: _LoopFacet,
    action: ActionEvent,
) -> ActionProfile | None:
    classify = getattr(loop.executor, "action_profile_for_call", None)
    if not callable(classify) or action.tool_call is None:
        return None
    try:
        profile = classify(
            action.tool_call.tool_name,
            action.tool_call.arguments,
        )
    except Exception:  # noqa: BLE001
        _LOG.debug("action_profile_for_call failed", exc_info=True)
        return None
    return profile if isinstance(profile, ActionProfile) else None


def _recover_elided_file_write_content(
    events: list[Event],
    path: str,
    *,
    before_id: str | None,
) -> str | None:
    """Recover marker-only write bytes only from a confirmed earlier write."""
    if not path:
        return None
    confirmed = _f8_confirmed_file_writes(events)
    seen_current = before_id is None
    for event in reversed(events):
        if not isinstance(event, ActionEvent) or event.tool_call is None:
            continue
        if not seen_current:
            seen_current = event.id == before_id
            continue
        call = event.tool_call
        if call.tool_name != "file_write":
            continue
        if call.arguments.get("path") != path or call.call_id not in confirmed:
            continue
        content = call.arguments.get("content")
        if not isinstance(content, str) or not content:
            continue
        if not find_elided_arg_markers({"content": content}):
            return content
    return None


async def _emit_dedup_observation(
    loop: _LoopFacet,
    action: ActionEvent,
    prior_id: str | None,
    pointer: str,
) -> None:
    assert action.tool_call is not None
    call = action.tool_call
    _LOG.info(
        "F9 read-dedup: short-circuited %s (call_id=%s, prior_action_id=%s)",
        call.tool_name,
        call.call_id,
        prior_id,
    )
    path = call.arguments.get("path")
    if isinstance(path, str) and path:
        _ground_read(loop, path)
    await loop._emit(
        ObservationEvent(
            tool_result=ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content=pointer,
                action_profile=_action_profile_for_call(loop, action),
            ),
            action_id=action.id,
        )
    )


def _shell_reminder(
    action: ActionEvent,
    events: list[Event],
) -> str | None:
    assert action.tool_call is not None
    call = action.tool_call
    remind, prior_seq, text = _w39_shell_verify_reminder(
        call.tool_name,
        call.arguments,
        events,
    )
    if not remind:
        return None
    _LOG.info(
        "W-39 shell-verify reminder: %s already passed at step %s "
        "(call_id=%s) — advisory, executing anyway",
        call.tool_name,
        prior_seq,
        call.call_id,
    )
    return text


def _answered_question_reminder(
    action: ActionEvent,
    events: list[Event],
) -> str | None:
    """The notice half of F47, across every tool class that carries one.

    *"Answered-question repetition is culpable only after notice"* — so this is
    the one place that decides whether a notice exists for the call about to
    execute, and it dispatches by class rather than assuming the shell class is
    the only one. Before 2026-08-07f the shell class WAS the only one, while
    `ThrashOracle` counted identical repeats over every tool; `update_plan_progress`
    @98651 was faulted at seqs 229/232/235 having been told nothing before seq 237.

    Advisory in both branches: nothing is skipped, the call executes either way.
    """
    assert action.tool_call is not None
    call = action.tool_call
    if call.tool_name in _W39_PLAN_TOOLS:
        remind, prior_seq, text = _w39_plan_progress_reminder(
            call.tool_name,
            call.arguments,
            events,
        )
        if not remind:
            return None
        _LOG.info(
            "F47 plan-tracking dedup notice: %s already succeeded at step %s "
            "(call_id=%s) — advisory, executing anyway",
            call.tool_name,
            prior_seq,
            call.call_id,
        )
        return text
    # Shell tools have two reminder classes (exact + script identity); check them
    # before falling through to generic.
    if call.tool_name in _W39_SHELL_TOOLS:
        return _shell_reminder(action, events)
    # F59 — generic notice for every other tool the oracle counts. Shell and
    # plan retain richer renderers; this covers verify_web_app and all
    # non-shell, non-plan tools with a repetition-aware, consequence-naming
    # template. The two excluded tools are owned by the oracle's scope change.
    from .dedup_generic_notice import _w39_generic_reminder

    remind, prior_seq, text = _w39_generic_reminder(
        call.tool_name, call.arguments, events
    )
    if not remind:
        return None
    _LOG.info(
        "F59 generic dedup notice: %s already succeeded at step %s "
        "(call_id=%s) — advisory, executing anyway",
        call.tool_name,
        prior_seq,
        call.call_id,
    )
    return text


async def _prepare_observation(
    loop: _LoopFacet,
    action: ActionEvent,
) -> tuple[bool, str | None]:
    """Return whether F9 handled the call and any deferred freshness memo.

    **This is the re-derived injection point for register #5's freshness memo
    (amendment A11 §1).** A6.3 named the seam only as "tool-dispatch time, in
    `core/loop/`" and predates Epic 13's port migration, so it was re-derived
    against the current tree rather than trusted: dispatch runs through
    `execute_and_observe` below, and this is the one place that sees the action
    before it executes and can return a message for the next turn's context.

    The two paths are gated DIFFERENTLY, on purpose:

    * **F9 read-dedup stays assist-gated.** It SHORT-CIRCUITS the call — it
      changes which tools actually run — and A6.3 §3.4 is explicit that the memo
      must never suppress a call. Un-gating a suppressing path is a much larger
      product change than this boundary is scoped for.
    * **The W-39 freshness memo is NOT gated.** Assist is OFF for capable models,
      which is exactly the population register #5's three firings came from, so
      an assist-gated memo would have been dead code in every canary that
      reproduced the defect. It never suppresses anything: the command always
      executes and the only observable change is one advisory message.

    Cost of un-gating, bounded deliberately: the event log is read only when a
    memo could actually fire — assist on (F9's path), or a tool class that carries
    a notice (`_W39_NOTICE_TOOLS`). Any other call with assist off takes the same
    early return it always did and reads nothing, so the un-gating adds no work
    to the paths it cannot affect.

    **2026-08-07f (F47).** That gate read `_W39_SHELL_TOOLS` until this boundary,
    and the narrowing was invisible: `ThrashOracle` counts identical repeats over
    EVERY tool, so every non-shell class was counted with no notice reachable.
    It now reads `_W39_NOTICE_TOOLS`, the single set the notice classes are
    declared in, so adding a class is one edit and cannot leave a second guard
    behind.
    """
    if action.tool_call is None:
        return False, None
    call = action.tool_call
    # F59 — the generic notice covers every tool the oracle counts (every tool
    # via tool_call_fingerprint). Shell and plan retain richer renderers; the
    # generic path handles any other tool. The two deliberately excluded tools
    # (propose_plan_update, submit_plan) are owned by the oracle's counted
    # population too — they are broken by a scope change, so the exclusion is
    # proved non-weakening. No parallel silent list remains.
    from .dedup_generic_notice import _W39_GENERIC_EXCLUDED

    is_generic = call.tool_name not in _W39_NOTICE_TOOLS and call.tool_name not in _W39_GENERIC_EXCLUDED
    if not loop._assist and call.tool_name not in _W39_NOTICE_TOOLS and not is_generic:
        return False, None
    events = await loop._events()
    if loop._assist:
        deduped, prior_id, pointer = _f9_dedupable_read(
            call.tool_name,
            call.arguments,
            events,
            readonly_names=loop._readonly_tool_names(),
        )
        if deduped:
            await _emit_dedup_observation(loop, action, prior_id, pointer)
            return True, None
    return False, _answered_question_reminder(action, events)


def _marker_only_call(action: ActionEvent, tool_name: str) -> bool:
    call = action.tool_call
    return bool(
        call is not None
        and call.tool_name == tool_name
        and value_is_only_elision_marker(call.arguments.get("content"))
    )


async def _recover_marker_write(
    loop: _LoopFacet,
    action: ActionEvent,
) -> bool:
    if not _marker_only_call(action, "file_write"):
        return False
    assert action.tool_call is not None
    call = action.tool_call
    path = call.arguments.get("path")
    if not isinstance(path, str) or not path:
        return False
    original = _recover_elided_file_write_content(
        await loop._events(),
        path,
        before_id=action.id,
    )
    if original is None:
        return False
    _LOG.info(
        "K1 recovery: re-expanded elided file_write content for %s (%d chars, call_id=%s)",
        path,
        len(original),
        call.call_id,
    )
    call.arguments["content"] = original
    _ground_read(loop, path)
    return True


async def _redirect_marker_append(
    loop: _LoopFacet,
    action: ActionEvent,
) -> bool:
    if not _marker_only_call(action, "file_append"):
        return False
    assert action.tool_call is not None
    call = action.tool_call
    path = call.arguments.get("path")
    if not isinstance(path, str) or not path:
        return False
    if not _has_confirmed_prior_append(
        await loop._events(),
        path,
        before_id=action.id,
    ):
        return False
    _LOG.info(
        "K1 redirect: file_append elision copy-back to %s of an already-"
        "applied append — redirect, no re-execute (call_id=%s)",
        path,
        call.call_id,
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nYour last file_append `content` was"
                    " the engine's internal elision placeholder (a"
                    " context-saving stand-in), NOT real text — and that"
                    f" content was ALREADY appended to {path} earlier. Do"
                    " NOT re-issue it; re-appending would DUPLICATE it."
                    " Continue with the next step of your plan (file_read"
                    " the path first if you need to confirm its current"
                    " content).\n</system-reminder>"
                ),
            ),
        )
    )
    return True


async def _emit_marker_error(
    loop: _LoopFacet,
    action: ActionEvent,
    bad_arguments: list[str],
) -> None:
    assert action.tool_call is not None
    call = action.tool_call
    _LOG.info(
        "K1 guard: rejected %s — arg(s) %s carry an elision placeholder (call_id=%s)",
        call.tool_name,
        bad_arguments,
        call.call_id,
    )
    recovery = (
        "actual current content from the CURRENT WORKSPACE block "
        "above (or call file_read) and resend the FULL argument."
        if loop._assist
        else "actual current content — call file_read on the path for the "
        "authoritative content — and resend the FULL argument."
    )
    await loop._emit(
        AgentErrorEvent(
            error=(
                f"Argument(s) {bad_arguments} contain an internal elision "
                "placeholder (e.g. '[[DISCO-ELIDED: ...]]' or historical "
                "angle-bracket elision text), not real content. That marker "
                "is a context-saving stand-in for content you ALREADY wrote "
                "— it is NOT the content itself, and it was NOT executed. Do "
                "not copy the placeholder into a tool call. Re-issue the "
                "call with real content, or read the " + recovery
            ),
            action_id=action.id,
            tool_call_id=call.call_id,
        )
    )


async def _handle_elision_marker(
    loop: _LoopFacet,
    action: ActionEvent,
) -> bool:
    """Return True when K1 redirected or rejected without executor entry."""
    if action.tool_call is None:
        return False
    bad_arguments = find_elided_arg_markers(action.tool_call.arguments)
    if bad_arguments == ["content"] and await _recover_marker_write(loop, action):
        bad_arguments = []
    if bad_arguments == ["content"] and await _redirect_marker_append(loop, action):
        return True
    if (
        bad_arguments == ["content"]
        and _marker_only_call(action, "file_write")
        and await _redirect_marker_write_with_read_provenance(loop, action)
    ):
        return True
    if not bad_arguments:
        return False
    await _emit_marker_error(loop, action, bad_arguments)
    return True


async def _persist_primary_result(
    loop: _LoopFacet,
    action: ActionEvent,
    result: ToolResult,
) -> None:
    events = await loop.store.get_events(loop.conversation_id)
    already_persisted = any(
        isinstance(event, (ObservationEvent, AgentErrorEvent)) and event.action_id == action.id
        for event in events
    )
    if already_persisted:
        return
    await _persist_runtime_constraints(loop, result)
    if result.success:
        await loop._emit(ObservationEvent(tool_result=result, action_id=action.id))
        return
    error = result.error or "tool failed"
    await loop._emit(
        AgentErrorEvent(
            error=error,
            detail=_error_detail(
                error,
                result.content,
                carries_delivery=bool((result.structured or {}).get("delivered_read")),
            ),
            action_id=action.id,
            tool_call_id=action.tool_call.call_id if action.tool_call else None,
            action_profile=result.action_profile,
            effect_receipts=result.effect_receipts,
        )
    )


async def _emit_execution_error(
    loop: _LoopFacet,
    action: ActionEvent,
    error: Exception,
) -> None:
    await loop._emit(
        AgentErrorEvent(
            error=str(error),
            action_id=action.id,
            tool_call_id=action.tool_call.call_id if action.tool_call else None,
        )
    )


async def maybe_emit_sandbox_restart(
    loop: _LoopFacet,
    sandbox: object | None,
    generation_before: int,
) -> None:
    if sandbox is None or generation_before == 0:
        return
    if getattr(sandbox, "generation", generation_before) <= generation_before:
        return
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    "Your sandbox container was restarted mid-session (the prior "
                    "container died and was transparently re-created); this run is "
                    f"now on container generation "
                    f"{getattr(sandbox, 'generation', generation_before)}, up from "
                    f"{generation_before}. Files previously written to /workspace "
                    "remain; any background processes or unsaved in-memory state "
                    "are gone. If you relied on running state, re-establish it "
                    "before continuing.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )


async def _emit_success_followups(
    loop: _LoopFacet,
    action: ActionEvent,
    reminder: str | None,
) -> None:
    if reminder is not None:
        await loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=reminder),
            )
        )
    await loop._maybe_emit_plan_step_done_condition_note(action)


async def execute_and_observe(
    loop: _LoopFacet,
    action: ActionEvent,
    *,
    restart_emitter: Callable[[object | None, int], Awaitable[None]] | None = None,
) -> None:
    """Execute one action and persist its ordered observation or error."""
    emit_restart = restart_emitter or partial(maybe_emit_sandbox_restart, loop)
    handled, reminder = await _prepare_observation(loop, action)
    if handled or await _handle_elision_marker(loop, action):
        return
    sandbox = getattr(loop.executor, "sandbox", None)
    generation_before = getattr(sandbox, "generation", 0) if sandbox is not None else 0
    persist = partial(_persist_primary_result, loop, action)
    primary_persisted = False
    try:
        attributed_execute = getattr(loop.executor, "execute_attributed", None)
        if callable(attributed_execute):
            result = await cast(Any, attributed_execute)(
                action.tool_call,
                action.agent_view_id,
                persist,
                loop._prepare_executor,
            )
            primary_persisted = True
        else:
            await loop._prepare_executor()
            result = await loop.executor.execute(action.tool_call)
    except LLMContextWindowExceeded:
        raise
    except Exception as error:  # noqa: BLE001
        await _emit_execution_error(loop, action, error)
        await emit_restart(sandbox, generation_before)
        return
    if not primary_persisted:
        await persist(result)
    if result.success:
        await _emit_success_followups(loop, action, reminder)
    await emit_restart(sandbox, generation_before)
