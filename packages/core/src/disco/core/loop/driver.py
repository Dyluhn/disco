"""The model-driving step: tool-set assembly + one bounded `agent.step()`.

Extracted from engine.py as a stateful collaborator: `Driver` holds a back-ref
to its `_LoopFacet` and runs the (e) drive step — mode-scoped tool visibility, the
watch-it-write stream hook, the weak-model invalid-tool requery ladder (Rung 7),
transient-retry backoff, and the context-window hard-reset path. Bodies are
byte-identical to the former _LoopFacet methods with `self.` rewritten to
`self._loop.` (sibling calls stay in-collaborator).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal, Protocol, cast

from ..context.compaction import CompactionPolicy, context_compact_if_needed
from ..events import (
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ToolCall,
)
from ..inspect import inspect_enabled
from ..llm import (
    LLMError,
    LLMTransientError,
    OperatingMode,
)
from ..llm.request_budget import RequestBudgetEstimate
from ..llm.types import EMPTY_REASONING_ONLY_METADATA_KEY
from ..obs import log_event
from ..view import View
from . import signals, view_render
from .boundaries import AgentStep
from .context_live import context_pack_enabled, protected_context_compaction_seqs
from .control import Disp
from .driver_read_escape import (
    stuck_escape_blocked_tools_for_step,
    stuck_escape_redundant_read_refusal,
    trusted_receipt_since,
)
from .driver_retry import (
    _PLANNING_TOOL_REFUSAL_ESCALATE_AT as _PLANNING_TOOL_REFUSAL_ESCALATE_AT,
)
from .driver_retry import (
    _PLANNING_TOOL_REFUSAL_NARROW_AT as _PLANNING_TOOL_REFUSAL_NARROW_AT,
)
from .driver_retry import (
    drive_step as _drive_step,
)
from .driver_retry import (
    planning_tool_refusal_streak as planning_tool_refusal_streak,
)
from .driver_retry import (
    prepare_drive_context,
    repair_degenerate_step,
    wait_retry_backoff,
)
from .stream_extract import extract_partial_string_field
from .tool_visibility import ToolVisibility

if TYPE_CHECKING:
    from ..llm import StreamChunk
    from .boundaries import StreamHook
    from .ports import (
        ContextGroundingPort,
        ConversationModePort,
        FinishVerificationPort,
        GateCounterPort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        TurnControlPort,
    )

    class _LoopFacet(
        ContextGroundingPort,
        ConversationModePort,
        FinishVerificationPort,
        GateCounterPort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        TurnControlPort,
        Protocol,
    ):
        """The loop capability this module uses: context grounding, conversation mode, finish
        verification, gate counters, the event log, the plan lifecycle, tool execution, turn
        control.
        """


_LOG = logging.getLogger("disco.loop")

_sleep = asyncio.sleep
_DRIVER_RETRY_BACKOFFS_S: tuple = (10.0, 30.0, 90.0)

# FORCED-SUBMIT read grace: how many ADDITIONAL grounding reads a model may make AFTER
# force_submit fired (at _PLAN_EXPLORE_READ_CAP) before the offered tools collapse to
# submit_plan only. ~30% of revision re-plans want to file_read the current files to ground
# the diff BEFORE submitting; narrowing to submit-only stranded them (file_read rejected →
# actionless → killed). A few grounding reads then submit-only bounds a runaway (the
# actionless valve already catches tool-LESS prose turns; this bounds tool-CALL read loops).
_FORCE_SUBMIT_READ_GRACE = 3


def _escalated_provider_prefs(n: int) -> dict:
    """Legacy callback seam; retries never inject vendor routing instructions."""
    return {}


def _empty_reasoning_repair_reminder(attempt: int = 1) -> str:
    """Empty-response repair reminder, repetition-aware (constraint 4).

    The repair ladder already COUNTS these — `empty_reasoning_repair_count` is a
    parameter of `repair_degenerate_step`, passed in alongside the reminder — but
    the reminder itself was a constant, so the second and third identical empty
    responses drew the identical sentence. The count is now rendered.
    """
    if attempt <= 1:
        return (
            "Your previous response produced no visible text and no tool call — call exactly "
            "one tool now, or say in plain text what you need."
        )
    return (
        f"This is repair attempt {attempt}: your last {attempt} responses produced no "
        "visible text and no tool call. Call exactly one tool now, or say in plain "
        "text what you need — another empty response ends this turn."
    )


def _prose_noop_repair_reminder(attempt: int = 1) -> str:
    """Prose-noop repair reminder, repetition-aware (constraint 4). See above."""
    if attempt <= 1:
        return (
            "You described the next action instead of performing it — call the tool "
            "for it in THIS turn."
        )
    return (
        f"This is repair attempt {attempt}: you have now described the next action "
        f"{attempt} times without performing it. Call the tool for it in THIS turn."
    )


# First-firing values, kept as module names for the durable-log content match in
# the diagnostics below and for callers that hold the reminder as configuration.
_EMPTY_REASONING_REPAIR_REMINDER = _empty_reasoning_repair_reminder(1)
_PROSE_NOOP_REPAIR_REMINDER = _prose_noop_repair_reminder(1)


async def _repair_attempt(loop: _LoopFacet, diagnostic: str) -> int:
    """How many times this repair has already been persisted, plus this one.

    Read from the durable log so the escalation survives a restart exactly as
    the run's own evidence does. Module-level, not a `Driver` method: the class
    is at its size cap.
    """
    events = await loop._events()
    return 1 + sum(
        1
        for event in events
        if isinstance(event, MessageEvent) and event.meta.get("diagnostic") == diagnostic
    )


async def _empty_reasoning_repair_event(loop: _LoopFacet, diagnostic: dict) -> MessageEvent:
    """The empty-response repair reminder, escalated on the durable count."""
    attempt = await _repair_attempt(loop, EMPTY_REASONING_ONLY_METADATA_KEY)
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content=_empty_reasoning_repair_reminder(attempt)),
        meta={"diagnostic": EMPTY_REASONING_ONLY_METADATA_KEY, **diagnostic},
    )


async def _prose_noop_repair_event(loop: _LoopFacet, step: AgentStep) -> MessageEvent:
    """The prose-noop repair reminder, escalated on the durable count."""
    attempt = await _repair_attempt(loop, signals.PROSE_NOOP_REPAIR_DIAGNOSTIC)
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content=_prose_noop_repair_reminder(attempt)),
        meta={
            "diagnostic": signals.PROSE_NOOP_REPAIR_DIAGNOSTIC,
            "content_len": len(step.thought),
            "tool_call_count": 0,
            "llm_response_id": step.llm_response_id,
        },
    )


def _is_tool_result_adjacency_protocol_error(err: LLMError) -> bool:
    """Provider-side protocol rejection for broken tool_call/tool-result order."""
    text = " ".join(
        str(part)
        for part in (
            err,
            getattr(err, "provider", ""),
            getattr(err, "model", ""),
            getattr(err, "code", ""),
            getattr(err, "error_code", ""),
        )
        if part
    ).lower()
    if "tool call result does not follow" in text:
        return True
    return "2013" in text and "invalid" in text and "param" in text


class Driver:
    def __init__(self, loop: _LoopFacet) -> None:
        self._loop = loop
        self._tools = ToolVisibility(loop)

    def _record_model_repair(
        self, repair_kind: str, *, attempt: int, tool_name: str | None = None
    ) -> None:
        """Record bounded structural repair diagnostics outside chat history."""
        fields: dict[str, object] = {
            "cid": self._loop.conversation_id,
            "repair_kind": repair_kind,
            "attempt": attempt,
        }
        if tool_name:
            fields["tool_name"] = tool_name
        log_event("agent.repair", **fields)

    async def _wait_retry_backoff(self, delay_s: float) -> bool:
        return await wait_retry_backoff(self, delay_s, _sleep)

    async def _persist_empty_reasoning_diagnostic(self, diagnostic: dict) -> None:
        await self._loop._emit(await _empty_reasoning_repair_event(self._loop, diagnostic))

    async def _persist_prose_noop_diagnostic(self, step: AgentStep) -> None:
        await self._loop._emit(await _prose_noop_repair_event(self._loop, step))

    def build_stream_hook(self) -> StreamHook | None:
        """Build the per-step watch-it-write hook when a sink is wired."""
        sink = self._loop.stream_sink
        if sink is None:
            return None
        # Per-step, per-tool-index accumulator (fresh each step → no stale state).
        state: dict[int, dict] = {}

        async def _hook(chunk: StreamChunk) -> None:
            st = state.setdefault(
                chunk.tool_index, {"args": "", "sent": 0, "path": None, "tool": ""}
            )
            st["args"] += chunk.tool_args_delta
            if chunk.tool_name:
                st["tool"] = chunk.tool_name
            if st["tool"] not in self._loop._STREAMING_WRITE_TOOLS:
                return  # only stream tools that carry a file body/edit replacement
            if not st["path"]:
                # Require the WHOLE path (closing quote present) so a frame never
                # shows a half-typed filename like "styles" for "styles.css".
                p = extract_partial_string_field(st["args"], "path", require_complete=True)
                if p:
                    st["path"] = p
            if not st["path"]:
                return
            field = "new" if st["tool"] == "file_edit" else "content"
            content = extract_partial_string_field(st["args"], field)
            if content is None:
                return
            new = content[st["sent"] :]
            if len(new) < self._loop._STREAM_FLUSH_CHARS and "\n" not in new:
                return  # coalesce — wait for more
            st["sent"] = len(content)
            sink(
                {
                    "type": "file_stream",
                    "tool": st["tool"],
                    "path": st["path"] or "",
                    "index": chunk.tool_index,
                    "delta": new,
                    "field": field,
                    "agent_view_id": self._loop._current_agent_view_id(),
                }
            )

        return _hook

    async def _pause_driver_unavailable(self, cause: LLMError | None = None) -> tuple[None, Disp]:
        """Explain a driver outage and HALT at the user-question gate.
        Called from both the LLMProviderUnavailable and LLMTransientError
        exhaustion paths to keep drive_step within its LOC budget.

        `cause` (when supplied) is labeled into the landing events' non-semantic
        `meta` channel so the UI can say WHY the driver stayed unavailable (e.g.
        a provider 429 usage limit) instead of a generic pause. The reason,
        guidance, statuses, and model-visible text are byte-identical either
        way — meta never enters the model view or any loop/oracle signal."""
        extra_meta: dict[str, str | int] | None = None
        if cause is not None:
            # str(cause) is the provider adapter's SAFE summary (never the raw
            # body) — e.g. "provider opencode-go returned HTTP 429 type=…".
            extra_meta = {
                "driver_error": str(cause),
                "driver_error_kind": type(cause).__name__,
            }
            if cause.provider:
                extra_meta["driver_error_provider"] = cause.provider
            if isinstance(cause, LLMTransientError) and cause.http_status is not None:
                extra_meta["driver_error_http_status"] = cause.http_status
        await self._loop._land_blocked(
            reason="driver-unavailable",
            guidance=(
                "The model driver stayed unavailable after the bounded provider "
                "retry path was exhausted. Last provider error on record: "
                f"{str(cause) if cause is not None else 'none recorded'}."
            ),
            legacy_status=ConversationStatus.PAUSED,
            legacy_detail="driver-unavailable",
            extra_meta=extra_meta,
        )
        return None, Disp.HALT

    def readonly_tool_names(self) -> frozenset[str] | None:
        return self._tools.readonly_tool_names()

    def _available_tools(self, available_tools: list | None) -> list:
        return self._tools._available_tools(available_tools)

    def planning_allowed_tool_names(
        self,
        available_tools: list | None = None,
    ) -> frozenset[str]:
        return self._tools.planning_allowed_tool_names(available_tools)

    def force_submit_read_calls_remaining(self) -> int:
        return self._tools.force_submit_read_calls_remaining()

    def tools_for_step(
        self,
        *,
        suppress_meta_tools: bool = False,
        force_submit_only: bool = False,
        force_read_tools: frozenset[str] | None = None,
        blocked_tools: frozenset[str] = frozenset(),
        mode: OperatingMode | None = None,
        available_tools: list | None = None,
    ) -> list:
        return self._tools.tools_for_step(
            suppress_meta_tools=suppress_meta_tools,
            force_submit_only=force_submit_only,
            force_read_tools=force_read_tools,
            blocked_tools=blocked_tools,
            mode=mode,
            available_tools=available_tools,
        )

    def _executor_callable_tool_names(self) -> set[str]:
        return self._tools._executor_callable_tool_names()

    def _virtual_tool_names(self) -> set[str]:
        return self._tools._virtual_tool_names()

    def known_tool_names_for_requery(self) -> set[str]:
        return self._tools.known_tool_names_for_requery()

    def allowed_tool_names_for_mode(
        self,
        mode: OperatingMode,
        *,
        available_tools: list,
        blocked_tools: frozenset[str] = frozenset(),
    ) -> set[str]:
        return self._tools.allowed_tool_names_for_mode(
            mode,
            available_tools=available_tools,
            blocked_tools=blocked_tools,
        )

    def unknown_tool_requery_hint(
        self,
        tool_name: str,
        offered_names: set[str],
    ) -> str:
        return self._tools.unknown_tool_requery_hint(tool_name, offered_names)

    def _prepare_drive_context(
        self,
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
        return prepare_drive_context(self, view, events)

    def stuck_escape_blocked_tools_for_step(
        self,
        events: list[Event],
    ) -> frozenset[str]:
        return stuck_escape_blocked_tools_for_step(events)

    @staticmethod
    def _trusted_receipt_since(
        events: list[Event],
        boundary_seq: int,
    ) -> bool:
        return trusted_receipt_since(events, boundary_seq)

    def stuck_escape_redundant_read_refusal(
        self,
        events: list[Event],
        tool_call: ToolCall,
    ) -> dict[str, object] | None:
        return stuck_escape_redundant_read_refusal(events, tool_call)

    async def _repair_degenerate_step(
        self,
        step: AgentStep,
        *,
        mode: OperatingMode,
        events: list[Event],
        transient_messages: list[LLMMessage],
        empty_reasoning_repair_count: int,
        prose_noop_repair_count: int,
    ) -> tuple[bool, list[LLMMessage], int, int]:
        return await repair_degenerate_step(
            self,
            step,
            mode=mode,
            events=events,
            transient_messages=transient_messages,
            empty_reasoning_repair_count=empty_reasoning_repair_count,
            prose_noop_repair_count=prose_noop_repair_count,
            empty_reasoning_reminder=_empty_reasoning_repair_reminder(
                empty_reasoning_repair_count + 1
            ),
            prose_noop_reminder=_prose_noop_repair_reminder(prose_noop_repair_count + 1),
        )

    async def _try_request_budget_preview(
        self,
        view: View,
        events: list[Event],
        mode,
        escape_temp,
        initial_offered_tools: list,
    ) -> Disp | None:
        agent = self._loop.agent
        preview_fn = getattr(agent, "request_budget_preview", None)
        if not callable(preview_fn):
            self._log_budget_event(outcome="unavailable")
            return None

        try:
            overflow = view_render.overflow_signal(events)
            preview = cast(
                RequestBudgetEstimate | None,
                preview_fn(
                    view,
                    initial_offered_tools,
                    mode=mode,
                    overflow_signal=overflow,
                    temperature=escape_temp,
                    assist=self._loop._assist,
                ),
            )
        except Exception:
            self._log_budget_event(outcome="unavailable")
            return None
        if preview is None:
            self._log_budget_event(outcome="unavailable")
            return None

        p = preview  # narrowed to non-None

        # --- should_condense (fail-open: unavailable → normal provider call) ---
        try:
            req = self._loop.condenser.should_condense(view, token_count=p.pressure_tokens)
        except Exception:
            self._log_budget_event(outcome="unavailable", estimate=p)
            return None

        if req is None:
            self._log_budget_event(outcome="within_budget", estimate=p)
            return None

        pressure_kind: Literal["soft", "hard"] = "soft" if req.soft else "hard"

        # --- context pack (fail-open: try condenser fallback) ---
        try:
            if context_pack_enabled():
                policy = CompactionPolicy.default()
                snips = context_compact_if_needed(
                    events,
                    policy,
                    protected_seqs=protected_context_compaction_seqs(events),
                    pressure_chars=p.canonical_payload_bytes,
                )
            else:
                snips = None
        except Exception:
            snips = None
        if snips:
            for snip in snips:
                await self._loop._assert_current_agent_view()
                await self._loop._emit(snip)
            self._log_budget_event(
                outcome="compacted",
                estimate=p,
                method="context_pack",
                kind=pressure_kind,
            )
            return Disp.CONTINUE

        # --- worth a summarizer call? (the same rule the view builder applies) ---
        if not view_render.worth_a_summarizer_call(
            self._loop.condenser, events, estimate=p.pressure_tokens, soft=req.soft
        ):
            self._log_budget_event(outcome="deferred", estimate=p, kind=pressure_kind)
            return None

        # --- condenser (fail-open: no_progress → normal provider call) ---
        await self._loop._assert_current_agent_view()
        try:
            view_of_events = View.of(events)
            tombstone = await self._loop.condenser.condense(
                events, view_of_events, summarizer=self._loop.summarizer
            )
        except Exception:
            self._log_budget_event(outcome="no_progress", estimate=p, kind=pressure_kind)
            return None

        if tombstone is not None:
            await self._loop._assert_current_agent_view()
            await self._loop._emit(tombstone)
            self._log_budget_event(
                outcome="compacted",
                estimate=p,
                method="condenser",
                kind=pressure_kind,
            )
            return Disp.CONTINUE

        self._log_budget_event(outcome="no_progress", estimate=p, kind=pressure_kind)
        return None

    def _log_budget_event(
        self,
        *,
        outcome: Literal["unavailable", "within_budget", "compacted", "no_progress", "deferred"],
        estimate: RequestBudgetEstimate | None = None,
        kind: Literal["soft", "hard"] | None = None,
        method: Literal["context_pack", "condenser"] | None = None,
    ) -> None:
        try:
            if not inspect_enabled():
                return
            fields: dict[str, object] = {
                "cid": self._loop.conversation_id,
                "outcome": outcome,
            }
            if estimate is not None:
                fields["driver_context_window"] = estimate.driver_context_window
                fields["max_output_tokens"] = estimate.max_output_tokens
                fields["canonical_payload_bytes"] = estimate.canonical_payload_bytes
                fields["messages_json_bytes"] = estimate.messages_json_bytes
                fields["tools_json_bytes"] = estimate.tools_json_bytes
                fields["estimated_input_tokens"] = estimate.estimated_input_tokens
                fields["pressure_tokens"] = estimate.pressure_tokens
                fields["message_count"] = estimate.message_count
                fields["tool_count"] = estimate.tool_count
            if kind is not None:
                fields["kind"] = kind
            if method is not None:
                fields["method"] = method
            log_event("request_budget.preview", **fields)
        except Exception:
            pass

    async def drive_step(
        self,
        view: View,
        events: list[Event],
    ) -> tuple[AgentStep | None, Disp]:
        return await _drive_step(
            self,
            view,
            events,
            retry_backoffs=_DRIVER_RETRY_BACKOFFS_S,
            provider_prefs=_escalated_provider_prefs,
            adjacency_error=_is_tool_result_adjacency_protocol_error,
        )
