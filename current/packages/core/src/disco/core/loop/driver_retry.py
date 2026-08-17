"""Bounded driver recovery, requery, and receipt-grounded escape mechanics."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..events import (
    ActionEvent,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    ObservationEvent,
)
from ..inspect import (
    inspect_enabled,
    record_progress_shadow,
    record_tool_scope,
)
from ..llm import (
    BudgetExceeded,
    LLMAuthError,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
    OperatingMode,
)
from ..view import View, repair_tool_call_adjacency
from . import signals, view_render
from .boundaries import AgentStep
from .control import Disp
from .driver_read_escape import (
    stuck_escape_blocked_tools_for_step,
    trusted_receipt_since,
)
from .messages import _describe_llm_error
from .no_progress_detector import debug_probe_blocked_tools
from .progress import reduce_progress

if TYPE_CHECKING:
    from .driver import Driver

_LOG = logging.getLogger("disco.loop")
_STUCK_ESCAPE_TEMP = 0.9
_PLANNING_TOOL_REFUSAL_NEEDLE = "is not available in PLANNING mode"
_PLANNING_TOOL_REFUSAL_ESCALATE_AT = 2
_PLANNING_TOOL_REFUSAL_NARROW_AT = 3
_PLANNING_TOOL_REFUSAL_READ_TOOLS = frozenset({"file_read"})


def _view_has_current_objective(view: View) -> bool:
    return any("<current-objective>" in (message.content or "") for message in view.messages)


def _is_planning_tool_refusal(event: Event) -> bool:
    from ..events import AgentErrorEvent

    return isinstance(event, AgentErrorEvent) and _PLANNING_TOOL_REFUSAL_NEEDLE in event.error


def planning_tool_refusal_streak(events: list[Event]) -> int:
    from ..events import AgentErrorEvent, MessageEvent, PlanEvent

    streak = 0
    for event in reversed(events):
        if _is_planning_tool_refusal(event):
            streak += 1
            continue
        if isinstance(event, ActionEvent):
            continue
        if isinstance(event, ObservationEvent | PlanEvent):
            break
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            break
        if isinstance(event, AgentErrorEvent):
            break
    return streak


def _record_progress(
    driver: Driver,
    events: list[Event],
    in_escape: bool,
) -> None:
    if not inspect_enabled():
        return
    try:
        progress = reduce_progress(events)
        record_progress_shadow(
            driver._loop.conversation_id,
            latest_event_seq=progress.latest_event_seq,
            last_progress_seq=progress.last_progress_seq,
            evidence_fingerprint=progress.evidence_fingerprint,
            progress_kinds=sorted(kind.value for kind in progress.progress_kinds),
            current_resource_count=len(progress.current_resources),
            observation_count=len(progress.observations),
            mutation_count=len(progress.mutations),
            verification_count=len(progress.verifications),
            executed_invocations=progress.executed_invocations,
            zero_progress_invocations=progress.zero_progress_invocations,
            unattributed_invocations=progress.unattributed_invocations,
            invalid_event_pairs=progress.invalid_event_pairs,
            invalid_receipt_groups=progress.invalid_receipt_groups,
            stale_receipts=progress.stale_receipts,
            recovery_lease_phase=(
                progress.recovery_lease.phase.value if progress.recovery_lease is not None else None
            ),
            recovery_candidate_capability=(
                progress.recovery_candidate.blocked_capability.value
                if progress.recovery_candidate is not None
                else None
            ),
            recovery_candidate_streak=(
                progress.recovery_candidate.zero_progress_streak
                if progress.recovery_candidate is not None
                else 0
            ),
            recovery_comparable=progress.recovery_comparable,
            invalid_log_order=progress.invalid_log_order,
            legacy_escape_active=in_escape,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "progress reducer shadow failed for %s",
            driver._loop.conversation_id,
        )


def _effective_mode(driver: Driver, view: View, events: list[Event]) -> OperatingMode:
    cached_mode = driver._loop.mode
    mode = driver._loop._reconcile_mode_from_events(events)
    if (
        cached_mode == OperatingMode.PLANNING
        and mode != OperatingMode.PLANNING
        and _view_has_current_objective(view)
    ):
        _LOG.error(
            "Mode desync corrected for %s at composition: cached=%s effective=%s",
            driver._loop.conversation_id,
            cached_mode.value,
            mode.value,
        )
    return mode


def prepare_drive_context(
    driver: Driver,
    view: View,
    events: list[Event],
) -> tuple[
    OperatingMode,
    float | None,
    bool,
    bool,
    frozenset[str] | None,
    frozenset[str],
]:
    escape_seq = signals.stuck_escape_seq(events)
    recovered = escape_seq is not None and trusted_receipt_since(events, escape_seq)
    in_escape = escape_seq is not None and not recovered
    escape_temp = _STUCK_ESCAPE_TEMP if in_escape else None
    blocked_tools = (
        stuck_escape_blocked_tools_for_step(events) | debug_probe_blocked_tools(events)
    )
    _record_progress(driver, events, in_escape)
    mode = _effective_mode(driver, view, events)
    fresh_session = (
        mode != OperatingMode.PLANNING and signals.actions_since_last_resume(events) == 0
    )
    refusal_force = (
        mode == OperatingMode.PLANNING
        and planning_tool_refusal_streak(events) >= _PLANNING_TOOL_REFUSAL_NARROW_AT
    )
    force_submit = mode == OperatingMode.PLANNING and (
        signals.prose_plan_force_submit(events) or refusal_force
    )
    force_reads = _PLANNING_TOOL_REFUSAL_READ_TOOLS if refusal_force else None
    return (
        mode,
        escape_temp,
        fresh_session,
        force_submit,
        force_reads,
        blocked_tools,
    )


async def wait_retry_backoff(
    driver: Driver,
    delay_s: float,
    sleep_fn: Callable[[float], Awaitable[Any]],
) -> bool:
    """Wait until the delay completes or the loop's control event interrupts."""
    import asyncio

    interrupt = driver._loop._retry_interrupt
    if interrupt.is_set():
        return False
    delay_task = asyncio.ensure_future(sleep_fn(delay_s))
    interrupt_task = asyncio.ensure_future(interrupt.wait())
    tasks = (delay_task, interrupt_task)
    try:
        done, _ = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if delay_task in done:
            await delay_task
        return interrupt_task not in done and not interrupt.is_set()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def repair_degenerate_step(
    driver: Driver,
    step: AgentStep,
    *,
    mode: OperatingMode,
    events: list[Event],
    transient_messages: list[LLMMessage],
    empty_reasoning_repair_count: int,
    prose_noop_repair_count: int,
    empty_reasoning_reminder: str,
    prose_noop_reminder: str,
) -> tuple[bool, list[LLMMessage], int, int]:
    if step.empty_reasoning_diagnostic is not None:
        await driver._persist_empty_reasoning_diagnostic(step.empty_reasoning_diagnostic)
        if empty_reasoning_repair_count < 1:
            driver._record_model_repair(
                "empty_reasoning",
                attempt=empty_reasoning_repair_count + 1,
            )
            return (
                True,
                transient_messages + [LLMMessage(role="user", content=empty_reasoning_reminder)],
                empty_reasoning_repair_count + 1,
                prose_noop_repair_count,
            )
    prose_noop = (
        mode != OperatingMode.PLANNING
        and step.tool_call is None
        and not step.finished
        and not step.truncated
        and bool(step.thought.strip())
        and prose_noop_repair_count < 1
        and not signals.prose_noop_repair_seen_current_execution_segment(events)
    )
    if not prose_noop:
        return (
            False,
            transient_messages,
            empty_reasoning_repair_count,
            prose_noop_repair_count,
        )
    await driver._persist_prose_noop_diagnostic(step)
    driver._record_model_repair(
        "prose_without_action",
        attempt=prose_noop_repair_count + 1,
    )
    return (
        True,
        transient_messages + [LLMMessage(role="user", content=prose_noop_reminder)],
        empty_reasoning_repair_count,
        prose_noop_repair_count + 1,
    )


@dataclass
class _DriveState:
    attempts: int = 0
    first_agent_invocation: bool = True
    requery_count: int = 0
    provider_retry_count: int = 0
    protocol_repair_count: int = 0
    empty_reasoning_repair_count: int = 0
    prose_noop_repair_count: int = 0
    transient_messages: list[LLMMessage] = field(default_factory=list)
    repaired_view: View | None = None


def _request_view(state: _DriveState, view: View) -> View:
    current = state.repaired_view or view
    if not state.transient_messages:
        return current
    return current.model_copy(update={"messages": current.messages + state.transient_messages})


def _request_tools(
    driver: Driver,
    state: _DriveState,
    *,
    fresh_session: bool,
    force_submit_only: bool,
    force_read_tools: frozenset[str] | None,
    blocked_tools: frozenset[str],
    mode: OperatingMode,
    initial_available_tools: list,
    initial_offered_tools: list,
) -> tuple[list, list]:
    if state.first_agent_invocation:
        return initial_available_tools, initial_offered_tools
    available = driver._loop.executor.available_tools()
    offered = driver.tools_for_step(
        suppress_meta_tools=fresh_session,
        force_submit_only=force_submit_only,
        force_read_tools=force_read_tools,
        blocked_tools=blocked_tools,
        mode=mode,
        available_tools=available,
    )
    return available, offered


def _record_tool_surface(
    driver: Driver,
    mode: OperatingMode,
    available_tools: list,
    offered_tools: list,
    blocked_tools: frozenset[str],
    attempt: int,
) -> None:
    if not inspect_enabled():
        return
    record_tool_scope(
        driver._loop.conversation_id,
        mode=mode.value,
        offered_tools={
            name for tool in offered_tools if isinstance((name := getattr(tool, "name", None)), str)
        },
        allowed_tools=driver.allowed_tool_names_for_mode(
            mode,
            available_tools=available_tools,
            blocked_tools=blocked_tools,
        ),
        attempt=attempt,
    )


async def _invoke_agent(
    driver: Driver,
    state: _DriveState,
    current_view: View,
    offered_tools: list,
    events: list[Event],
    mode: OperatingMode,
    escape_temp: float | None,
    provider_prefs: Callable[[int], dict],
) -> AgentStep:
    await driver._loop._assert_current_agent_view()
    state.first_agent_invocation = False
    step = await driver._loop.agent.step(
        current_view,
        offered_tools,
        mode=mode,
        overflow_signal=view_render.overflow_signal(events),
        on_stream=driver.build_stream_hook(),
        temperature=escape_temp,
        assist=driver._loop._assist,
        attempt=state.attempts + 1,
        provider_prefs=(
            provider_prefs(state.provider_retry_count) if state.provider_retry_count > 0 else None
        ),
    )
    await driver._loop._assert_current_agent_view()
    return step


def _prepare_unknown_tool_requery(
    driver: Driver,
    step: AgentStep,
    state: _DriveState,
    *,
    fresh_session: bool,
    force_submit_only: bool,
    force_read_tools: frozenset[str] | None,
    blocked_tools: frozenset[str],
    mode: OperatingMode,
) -> bool:
    tool_call = step.tool_call
    if tool_call is None:
        return False
    if tool_call.tool_name in driver.known_tool_names_for_requery():
        return False
    gates_by_name = getattr(driver._loop.policy, "gates_by_name", None)
    if callable(gates_by_name) and gates_by_name(tool_call.tool_name):
        return False
    if state.requery_count >= 2:
        return False
    state.requery_count += 1
    driver._record_model_repair(
        "unknown_tool",
        attempt=state.requery_count,
        tool_name=tool_call.tool_name,
    )
    _LOG.info("Unknown tool %s, requerying...", tool_call.tool_name)
    offered_names = {
        tool.name
        for tool in driver.tools_for_step(
            suppress_meta_tools=fresh_session,
            force_submit_only=force_submit_only,
            force_read_tools=force_read_tools,
            blocked_tools=blocked_tools,
            mode=mode,
        )
    }
    state.transient_messages += [
        LLMMessage(
            role="assistant",
            content=step.thought,
            tool_calls=[
                {
                    "id": tool_call.call_id,
                    "name": tool_call.tool_name,
                    "arguments": tool_call.arguments,
                }
            ],
        ),
        LLMMessage(
            role="user",
            content=driver.unknown_tool_requery_hint(
                tool_call.tool_name,
                offered_names,
            ),
        ),
    ]
    return True


async def _consume_step(
    driver: Driver,
    state: _DriveState,
    step: AgentStep,
    events: list[Event],
    *,
    mode: OperatingMode,
    fresh_session: bool,
    force_submit_only: bool,
    force_read_tools: frozenset[str] | None,
    blocked_tools: frozenset[str],
) -> AgentStep | None:
    (
        retry,
        state.transient_messages,
        state.empty_reasoning_repair_count,
        state.prose_noop_repair_count,
    ) = await driver._repair_degenerate_step(
        step,
        mode=mode,
        events=events,
        transient_messages=state.transient_messages,
        empty_reasoning_repair_count=state.empty_reasoning_repair_count,
        prose_noop_repair_count=state.prose_noop_repair_count,
    )
    if retry:
        return None
    if _prepare_unknown_tool_requery(
        driver,
        step,
        state,
        fresh_session=fresh_session,
        force_submit_only=force_submit_only,
        force_read_tools=force_read_tools,
        blocked_tools=blocked_tools,
        mode=mode,
    ):
        return None
    return step


async def _provider_unavailable(
    driver: Driver,
    state: _DriveState,
    error: LLMProviderUnavailable,
) -> tuple[bool, tuple[None, Disp] | None]:
    if state.provider_retry_count < 2:
        state.provider_retry_count += 1
        _LOG.warning(
            "Provider unavailable: %s. Escalating provider_prefs (retry %d/2)...",
            error,
            state.provider_retry_count,
        )
        return True, None
    return False, await driver._pause_driver_unavailable(cause=error)


async def _transient_error(
    driver: Driver,
    state: _DriveState,
    error: LLMTransientError,
    retry_backoffs: tuple[float, ...],
) -> tuple[bool, tuple[None, Disp] | None]:
    if state.attempts >= len(retry_backoffs):
        driver._record_model_repair(
            "driver_transient_exhausted",
            attempt=state.attempts + 1,
        )
        return False, await driver._pause_driver_unavailable(cause=error)
    attempt = state.attempts + 1
    driver._record_model_repair(
        "driver_transient_backoff",
        attempt=attempt,
    )
    if not await driver._wait_retry_backoff(retry_backoffs[state.attempts]):
        driver._record_model_repair(
            "driver_transient_interrupted",
            attempt=attempt,
        )
        return False, (None, Disp.CONTINUE)
    state.attempts += 1
    return True, None


def _repair_model_error(
    driver: Driver,
    state: _DriveState,
    error: LLMError,
    current_view: View,
    adjacency_error: Callable[[LLMError], bool],
) -> bool:
    if adjacency_error(error):
        if state.protocol_repair_count >= 1:
            return False
        state.protocol_repair_count += 1
        driver._record_model_repair(
            "tool_history_protocol",
            attempt=state.protocol_repair_count,
        )
        repaired_messages = repair_tool_call_adjacency(current_view.messages)
        state.repaired_view = current_view.model_copy(update={"messages": repaired_messages})
        state.transient_messages = []
        _LOG.error(
            "Provider rejected tool-call history ordering (%s); "
            "retrying once with repaired history (%d -> %d messages)",
            error,
            len(current_view.messages),
            len(repaired_messages),
        )
        return True
    if isinstance(error, (LLMAuthError, BudgetExceeded)):
        return False
    if state.requery_count >= 2:
        return False
    state.requery_count += 1
    driver._record_model_repair(
        "provider_rejected_request",
        attempt=state.requery_count,
    )
    _LOG.warning("Provider rejected request: %s, requerying...", error)
    state.transient_messages.append(
        LLMMessage(
            role="user",
            content=(
                f"The provider rejected the previous request: {error}. "
                "Please adjust your response (check tool names, "
                "JSON structure, or parameters) and try again."
            ),
        )
    )
    return True


async def _run_retry_loop(
    driver: Driver,
    view: View,
    events: list[Event],
    *,
    mode: OperatingMode,
    escape_temp: float | None,
    fresh_session: bool,
    force_submit_only: bool,
    force_read_tools: frozenset[str] | None,
    blocked_tools: frozenset[str],
    initial_available_tools: list,
    initial_offered_tools: list,
    retry_backoffs: tuple[float, ...],
    provider_prefs: Callable[[int], dict],
    adjacency_error: Callable[[LLMError], bool],
) -> tuple[AgentStep | None, Disp | None]:
    state = _DriveState()
    while True:
        current_view = _request_view(state, view)
        available, offered = _request_tools(
            driver,
            state,
            fresh_session=fresh_session,
            force_submit_only=force_submit_only,
            force_read_tools=force_read_tools,
            blocked_tools=blocked_tools,
            mode=mode,
            initial_available_tools=initial_available_tools,
            initial_offered_tools=initial_offered_tools,
        )
        _record_tool_surface(
            driver,
            mode,
            available,
            offered,
            blocked_tools,
            state.attempts + 1,
        )
        try:
            step = await _invoke_agent(
                driver,
                state,
                current_view,
                offered,
                events,
                mode,
                escape_temp,
                provider_prefs,
            )
            consumed = await _consume_step(
                driver,
                state,
                step,
                events,
                mode=mode,
                fresh_session=fresh_session,
                force_submit_only=force_submit_only,
                force_read_tools=force_read_tools,
                blocked_tools=blocked_tools,
            )
            if consumed is not None:
                return consumed, None
        except LLMContextWindowExceeded:
            raise
        except LLMProviderUnavailable as error:
            retry, terminal = await _provider_unavailable(driver, state, error)
            if not retry:
                assert terminal is not None
                return terminal[0], terminal[1]
        except LLMTransientError as error:
            retry, terminal = await _transient_error(
                driver,
                state,
                error,
                retry_backoffs,
            )
            if not retry:
                assert terminal is not None
                return terminal[0], terminal[1]
        except LLMError as error:
            if not _repair_model_error(
                driver,
                state,
                error,
                current_view,
                adjacency_error,
            ):
                raise


async def drive_step(
    driver: Driver,
    view: View,
    events: list[Event],
    *,
    retry_backoffs: tuple[float, ...],
    provider_prefs: Callable[[int], dict],
    adjacency_error: Callable[[LLMError], bool],
) -> tuple[AgentStep | None, Disp]:
    (
        mode,
        escape_temp,
        fresh_session,
        force_submit_only,
        force_read_tools,
        blocked_tools,
    ) = driver._prepare_drive_context(view, events)
    available_tools = driver._loop.executor.available_tools()
    offered_tools = driver.tools_for_step(
        suppress_meta_tools=fresh_session,
        force_submit_only=force_submit_only,
        force_read_tools=force_read_tools,
        blocked_tools=blocked_tools,
        mode=mode,
        available_tools=available_tools,
    )
    preview = await driver._try_request_budget_preview(
        view,
        events,
        mode,
        escape_temp,
        offered_tools,
    )
    if preview is not None:
        return None, preview
    try:
        step, terminal = await _run_retry_loop(
            driver,
            view,
            events,
            mode=mode,
            escape_temp=escape_temp,
            fresh_session=fresh_session,
            force_submit_only=force_submit_only,
            force_read_tools=force_read_tools,
            blocked_tools=blocked_tools,
            initial_available_tools=available_tools,
            initial_offered_tools=offered_tools,
            retry_backoffs=retry_backoffs,
            provider_prefs=provider_prefs,
            adjacency_error=adjacency_error,
        )
        if terminal is not None:
            return None, terminal
    except LLMContextWindowExceeded:
        if await driver._loop._hard_reset(await driver._loop._events()):
            return None, Disp.CONTINUE
        await driver._loop._emit(
            ErrorEvent(
                code="context_window",
                detail="hard reset made no progress",
            )
        )
        return None, Disp.HALT
    except LLMError as error:
        code = "auth_error" if isinstance(error, (LLMAuthError, BudgetExceeded)) else "model_error"
        await driver._loop._emit(ErrorEvent(code=code, detail=_describe_llm_error(error)))
        return None, Disp.HALT
    return step, Disp.FALLTHROUGH
