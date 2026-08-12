"""View projection helpers — microcompact, pinning, and message rendering.

Extracted from ``view.py`` so the projection logic stays under the module
logical-LOC limit. These functions are pure over event lists.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, cast

from ._view_condensation import _is_cumulative_model_summary
from ._view_plan import _latest_plan, effective_plan_progress
from .events import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    DatasourceEvent,
    Event,
    EventSource,
    KnowledgeEvent,
    LLMConvertible,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    RuntimeConstraintEvent,
)

# Tools that DO change durable state, so their turns are never microcompacted.
_DURABLE_TOOLS = frozenset({"file_write", "file_append", "file_edit", "plan_step"})

# Tools whose latest observation may carry a rendered-page screenshot.
_SCREENSHOT_TOOLS = frozenset({"browser", "verify_web_app"})

_MASK_KEEP_RECENT = 8
_MASK_MIN_CHARS = 600


def repair_tool_call_adjacency(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Return a provider-safe message list with dangling tool-call pairs dropped."""
    out: list[LLMMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            ids: list[str] = []
            for tc in msg.tool_calls:
                cid = tc.get("id") if isinstance(tc, dict) else None
                if isinstance(cid, str) and cid:
                    ids.append(cid)
            if len(ids) == len(msg.tool_calls):
                following = messages[i + 1 : i + 1 + len(ids)]
                if len(following) == len(ids) and all(
                    f.role == "tool" and f.tool_call_id == cid
                    for f, cid in zip(following, ids, strict=True)
                ):
                    out.append(msg)
                    out.extend(following)
                    i += 1 + len(ids)
                    continue
            i += 1
            continue
        if msg.role == "tool":
            i += 1
            continue
        out.append(msg)
        i += 1
    return out


def _call_key(a: ActionEvent) -> str:
    """A stable identity for a tool call: tool name + canonical args."""
    return (
        a.tool_call.tool_name
        + "\x00"
        + json.dumps(a.tool_call.arguments or {}, sort_keys=True, default=str)
    )


def _is_microcompact_candidate(
    a: ActionEvent,
    events: list[Event],
    obs_by_action: dict[str, ObservationEvent],
    succeeded_keys: set[str],
    forgotten: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """Return (start_seq, end_seq) if ``a`` is a microcompact candidate, else None."""
    if a.tool_call is None or a.tool_call.tool_name in _DURABLE_TOOLS:
        return None
    o = obs_by_action.get(a.id)
    if o is None or o.tool_result.success:
        return None
    if a.seq is None or o.seq is None or o.seq != a.seq + 1:
        return None
    if any(lo <= a.seq <= hi for lo, hi in forgotten):
        return None
    if _call_key(a) not in succeeded_keys:
        return None
    return a.seq, o.seq


def microcompact(events: list[Event]) -> list[CondensationEvent]:
    """S3 Microcompact (GAP A) — tombstone NO-OP turns: a tool call that FAILED
    and was later SUPERSEDED by an IDENTICAL call that SUCCEEDED."""
    forgotten: list[tuple[int, int]] = [
        (e.forgotten_start_seq, e.forgotten_end_seq)
        for e in events
        if isinstance(e, CondensationEvent)
    ]
    obs_by_action: dict[str, ObservationEvent] = {
        e.action_id: e for e in events if isinstance(e, ObservationEvent) and e.action_id
    }
    succeeded_keys: set[str] = set()
    for a in events:
        if not isinstance(a, ActionEvent) or a.tool_call is None:
            continue
        o = obs_by_action.get(a.id)
        if o is not None and o.tool_result.success:
            succeeded_keys.add(_call_key(a))

    tombstones: list[CondensationEvent] = []
    for a in events:
        if not isinstance(a, ActionEvent):
            continue
        span = _is_microcompact_candidate(a, events, obs_by_action, succeeded_keys, forgotten)
        if span is None:
            continue
        start, end = span
        tombstones.append(
            CondensationEvent(
                forgotten_start_seq=start,
                forgotten_end_seq=end,
                summary=(
                    f"[microcompacted: a failed `{a.tool_call.tool_name}` attempt was "
                    "dropped — an identical call succeeded later]"
                ),
                summary_role="user",
                reason="events",
            )
        )
    return tombstones


def recover_span(events: list[Event], tombstone: CondensationEvent) -> list[Event]:
    """C11 — Re-materialize the ORIGINAL events a tombstone dropped."""
    start, end = tombstone.forgotten_start_seq, tombstone.forgotten_end_seq
    if start > end:
        return []
    return [e for e in events if e.seq is not None and start <= e.seq <= end]


def _live_runtime_constraint_seqs(events: list[Event]) -> set[int]:
    """Seqs of the runtime constraints that are still LIVE, one per key."""
    latest: dict[str, RuntimeConstraintEvent] = {}
    generations: list[str] = []
    for event in events:
        if not isinstance(event, RuntimeConstraintEvent):
            continue
        latest[event.constraint_key] = event
        if event.capability_generation:
            generations.append(event.capability_generation)

    current_generation = generations[-1] if generations else ""
    live: set[int] = set()
    for event in latest.values():
        if not event.active or event.seq is None:
            continue
        if current_generation and event.capability_generation != current_generation:
            continue
        live.add(event.seq)
    return live


def _pinned_seqs(events: list[Event]) -> set[int]:
    """Seqs that condensation/masking must not remove from this request."""
    from .loop.resource_context import essential_read_pair_seqs

    pinned: set[int] = set(essential_read_pair_seqs(events))
    first_user_turn = next(
        (
            e
            for e in events
            if isinstance(e, MessageEvent) and e.source == EventSource.USER and e.seq is not None
        ),
        None,
    )
    if first_user_turn is not None and first_user_turn.seq is not None:
        pinned.add(first_user_turn.seq)
    plan = _latest_plan(events)
    if plan is not None and plan.seq is not None:
        pinned.add(plan.seq)
    seen_knowledge: set[tuple[str, str]] = set()
    for e in events:
        if isinstance(e, KnowledgeEvent):
            h = hashlib.sha256(e.snippet.strip().encode()).hexdigest()
            key = (e.scope, h)
            if key in seen_knowledge:
                continue
            seen_knowledge.add(key)
            if e.seq is not None:
                pinned.add(e.seq)
        elif isinstance(e, DatasourceEvent) and e.seq is not None:
            pinned.add(e.seq)
    pinned |= _live_runtime_constraint_seqs(events)
    return pinned


def _recitation_message(
    events: list[Event], *, context_pack_active: bool = False
) -> LLMMessage | None:
    """GAP D recency recitation: an EPHEMERAL objective + step checklist."""
    plan, states = effective_plan_progress(events)
    if plan is None or not plan.steps:
        return None
    done = {i for i, st in states.items() if st == "done"}
    active = {i for i, st in states.items() if st == "active"}
    lines = []
    next_pending = None
    for i, step in enumerate(plan.steps, start=1):
        if i in done:
            mark = "✓"
        elif i in active:
            mark = "→"
        else:
            mark = "□"
            if next_pending is None:
                next_pending = i
        lines.append(f"  {mark} {i}. {step.title}")
    if context_pack_active:
        current_idx = min(active) if active else next_pending
        if current_idx is not None:
            current = f"Current step: {current_idx}. {plan.steps[current_idx - 1].title}"
            drift_gate = (
                "Drift gate: if this step no longer matches reality, call "
                "update_plan_progress. Otherwise take the step's next concrete "
                "action immediately — do NOT restate or answer this reminder."
            )
        else:
            current = "Current step: all plan steps are marked done."
            drift_gate = "Drift gate: verify the result, then finish."
        body = f"<current-objective>\n{current}\n{drift_gate}\n</current-objective>"
        return LLMMessage(role="user", content=body)
    nxt = (
        f"\nNext incomplete step: {next_pending}. {plan.steps[next_pending - 1].title}"
        if next_pending is not None
        else "\nAll steps marked done — verify, then finish."
    )
    body = (
        "<current-objective>\n"
        f"Goal: {plan.summary}\n"
        f"Plan progress ({len(done)}/{len(plan.steps)} done):\n"
        + "\n".join(lines)
        + nxt
        + "\n</current-objective>"
    )
    return LLMMessage(role="user", content=body)


def _build_action_result_maps(
    events: list[Event],
) -> tuple[
    dict[str, ActionEvent],
    dict[str, ActionEvent],
    dict[str, ObservationEvent | AgentErrorEvent],
    dict[str, ObservationEvent | AgentErrorEvent],
]:
    """Build action/result lookup maps by id and call_id."""
    action_by_id: dict[str, ActionEvent] = {}
    action_by_call_id: dict[str, ActionEvent] = {}
    result_by_action_id: dict[str, ObservationEvent | AgentErrorEvent] = {}
    result_by_call_id: dict[str, ObservationEvent | AgentErrorEvent] = {}
    for e in events:
        if isinstance(e, ActionEvent):
            action_by_id[e.id] = e
            action_by_call_id[e.tool_call.call_id] = e
        elif isinstance(e, ObservationEvent):
            if isinstance(e.action_id, str) and e.action_id:
                result_by_action_id.setdefault(e.action_id, e)
            result_by_call_id.setdefault(e.tool_result.call_id, e)
        elif isinstance(e, AgentErrorEvent):
            if isinstance(e.action_id, str) and e.action_id:
                result_by_action_id.setdefault(e.action_id, e)
            if isinstance(e.tool_call_id, str) and e.tool_call_id:
                result_by_call_id.setdefault(e.tool_call_id, e)
    return action_by_id, action_by_call_id, result_by_action_id, result_by_call_id


def _find_latest_screenshot_seq(
    events: list[Event],
    is_visible_fn: Callable[[Event], bool],
) -> int | None:
    """Find the latest visible browser screenshot seq."""
    for e in reversed(events):
        if (
            isinstance(e, ObservationEvent)
            and e.seq is not None
            and is_visible_fn(e)
            and e.tool_result.tool_name in _SCREENSHOT_TOOLS
            and (e.tool_result.structured or {}).get("screenshot_b64")
        ):
            return e.seq
    return None


def _apply_tail_variation(msg: LLMMessage, e: Event) -> LLMMessage:
    """B3: Deterministic-by-seq tail variation for thoughts and tool results."""
    seq = getattr(e, "seq", None)
    if seq is None:
        return msg
    if isinstance(e, ActionEvent):
        variants = [lambda t: t, lambda t: f"Reasoning: {t}", lambda t: f"Thought: {t}"]
        f = variants[seq % len(variants)]
        return msg.model_copy(update={"content": f(msg.content)})
    if isinstance(e, ObservationEvent):
        variants = [lambda c: c, lambda c: f"Observation: {c}", lambda c: f"Output: {c}"]
        f = variants[seq % len(variants)]
        return msg.model_copy(update={"content": f(msg.content)})
    return msg


def _render_observation_message(
    e: ObservationEvent,
    *,
    recent_obs_seqs: set[int],
    pinned: set[int],
    prompt_read_by_observation_seq: dict[int, Any],
    latest_screenshot_seq: int | None,
) -> tuple[LLMMessage, int | None, bool]:
    """Render one observation event to an LLM message, applying masking."""
    if (
        e.seq is not None
        and e.seq not in recent_obs_seqs
        and e.seq not in pinned
        and len(e.tool_result.content) > _MASK_MIN_CHARS
    ):
        digest = hashlib.sha256(e.tool_result.content.encode()).hexdigest()[:12]
        return (
            LLMMessage(
                role="tool",
                content=(
                    f"[masked output: {e.tool_result.tool_name} #{e.seq} — "
                    f"{len(e.tool_result.content):,} chars, sha256:{digest}. "
                    "Re-run the tool (or file_read the same path) to see it again.]"
                ),
                tool_call_id=e.tool_result.call_id,
            ),
            e.seq,
            False,
        )
    exact_read = prompt_read_by_observation_seq.get(e.seq) if e.seq is not None else None
    if exact_read is not None and _rendered_content_matches(
        exact_read.receipt, e.tool_result.content
    ):
        msg = LLMMessage(
            role="tool",
            content=e.tool_result.content,
            tool_call_id=e.tool_result.call_id,
        )
    else:
        msg = e.to_llm_message()
    if e.seq == latest_screenshot_seq and latest_screenshot_seq is not None:
        assert e.tool_result.structured is not None
        b64 = e.tool_result.structured.get("screenshot_b64")
        msg = msg.model_copy(update={"images": [f"data:image/png;base64,{b64}"]})
    return msg, e.seq, True


def _rendered_content_matches(receipt: Any, content: str) -> bool:
    """Check if rendered content matches a read receipt."""
    from .loop.resource_context import rendered_content_matches_receipt

    return rendered_content_matches_receipt(receipt, content)


def _process_visible_event(
    e: Event,
    *,
    is_visible_fn: Callable[[Event], bool],
    result_for_action_fn: Callable[[ActionEvent], ObservationEvent | AgentErrorEvent | None],
    action_for_result_fn: Callable[[ObservationEvent | AgentErrorEvent], ActionEvent | None],
    append_fn: Callable[[LLMConvertible, list[LLMMessage], list[int]], None],
    msgs: list[LLMMessage],
    visible: list[int],
    paired_results_rendered: set[int],
) -> bool:
    """Process one visible event. Returns True if the event was handled (paired
    or appended), False if it should be appended as a standalone message."""
    if isinstance(e, ActionEvent):
        result = result_for_action_fn(e)
        if result is not None and is_visible_fn(result):
            append_fn(e, msgs, visible)
            append_fn(result, msgs, visible)
            if result.seq is not None:
                paired_results_rendered.add(result.seq)
            return True
    elif isinstance(e, (ObservationEvent, AgentErrorEvent)):
        action = action_for_result_fn(e)
        if action is not None and is_visible_fn(action):
            if e.seq is not None and e.seq in paired_results_rendered:
                return True
            return True  # paired result already rendered with its action
    return False


def _walk_events_for_messages(
    events: list[Event],
    *,
    summary_at_start: dict[int, CondensationEvent],
    is_visible_fn: Callable[[Event], bool],
    result_for_action_fn: Callable[[ActionEvent], ObservationEvent | AgentErrorEvent | None],
    action_for_result_fn: Callable[[ObservationEvent | AgentErrorEvent], ActionEvent | None],
    append_fn: Callable[[LLMConvertible, list[LLMMessage], list[int]], None],
) -> tuple[list[LLMMessage], list[int]]:
    """Walk events in seq order, emitting summaries and visible messages."""
    msgs: list[LLMMessage] = []
    visible: list[int] = []
    emitted_summary_for: set[int] = set()
    paired_results_rendered: set[int] = set()

    for e in events:
        if e.seq is not None and e.seq in summary_at_start:
            key = e.seq
            if key not in emitted_summary_for:
                tomb = summary_at_start[key]
                msgs.append(LLMMessage(role=tomb.summary_role, content=tomb.summary))
                emitted_summary_for.add(key)
        if isinstance(e, CondensationEvent):
            continue
        if not isinstance(e, LLMConvertible):
            continue
        if not is_visible_fn(e):
            continue
        handled = _process_visible_event(
            e,
            is_visible_fn=is_visible_fn,
            result_for_action_fn=result_for_action_fn,
            action_for_result_fn=action_for_result_fn,
            append_fn=append_fn,
            msgs=msgs,
            visible=visible,
            paired_results_rendered=paired_results_rendered,
        )
        if not handled:
            append_fn(e, msgs, visible)
    return msgs, visible


def _setup_view_projection(
    events: list[Event],
) -> tuple[
    list[tuple[int, int]],
    dict[int, CondensationEvent],
    set[int],
    dict[int, Any],
    dict[str, ActionEvent],
    dict[str, ActionEvent],
    dict[str, ObservationEvent | AgentErrorEvent],
    dict[str, ObservationEvent | AgentErrorEvent],
]:
    """Build the projection setup: forgotten ranges, summaries, pinned seqs,
    and action/result maps. Returns all the lookup structures."""
    tombstones = [event for event in events if isinstance(event, CondensationEvent)]
    forgotten = [(event.forgotten_start_seq, event.forgotten_end_seq) for event in tombstones]
    summary_at_start: dict[int, CondensationEvent] = {}
    cumulative = [event for event in tombstones if _is_cumulative_model_summary(event)]
    if cumulative:
        # Each new model summary updates the previous one with a fresh delta.
        # Render only that latest cumulative summary, at the first forgotten
        # position, instead of stacking every historical rewrite in the prompt.
        summary_at_start[min(event.forgotten_start_seq for event in cumulative)] = cumulative[-1]
    for event in tombstones:
        if _is_cumulative_model_summary(event):
            continue
        summary_at_start.setdefault(event.forgotten_start_seq, event)
    pinned = _pinned_seqs(events)
    from .loop.resource_context import (
        read_receipt_records,
        select_prompt_read_records,
    )

    prompt_read_by_observation_seq = {
        record.observation_seq: record
        for record in select_prompt_read_records(read_receipt_records(events))
        if record.observation_seq is not None
    }
    maps = _build_action_result_maps(events)
    return (forgotten, summary_at_start, pinned, prompt_read_by_observation_seq, *maps)


def build_view_messages(events: list[Event]) -> tuple[list[LLMMessage], list[int], int, int]:
    """Build the LLM-visible messages from the event log.

    Returns (messages, visible_seqs, total_events, forgotten_count).
    This is the exact projection logic extracted from ``View.of``.
    """
    from .events import agent_view_consistent_events

    events = cast(list[Event], agent_view_consistent_events(events))
    (
        forgotten,
        summary_at_start,
        pinned,
        prompt_read_by_observation_seq,
        action_by_id,
        action_by_call_id,
        result_by_action_id,
        result_by_call_id,
    ) = _setup_view_projection(events)

    def is_forgotten(seq: int | None) -> bool:
        if seq is not None and seq in pinned:
            return False
        return seq is not None and any(a <= seq <= b for a, b in forgotten)

    def result_for_action(e: ActionEvent) -> ObservationEvent | AgentErrorEvent | None:
        return result_by_action_id.get(e.id) or result_by_call_id.get(e.tool_call.call_id)

    def action_for_result(e: ObservationEvent | AgentErrorEvent) -> ActionEvent | None:
        if isinstance(e, ObservationEvent):
            action = action_by_id.get(e.action_id) if isinstance(e.action_id, str) else None
            return action or action_by_call_id.get(e.tool_result.call_id)
        action = action_by_id.get(e.action_id) if isinstance(e.action_id, str) else None
        if action is not None:
            return action
        return action_by_call_id.get(e.tool_call_id) if isinstance(e.tool_call_id, str) else None

    def pair_omitted(e: Event) -> bool:
        if isinstance(e, ActionEvent):
            result = result_for_action(e)
            return result is not None and is_forgotten(result.seq)
        if isinstance(e, (ObservationEvent, AgentErrorEvent)):
            action = action_for_result(e)
            return action is not None and is_forgotten(action.seq)
        return False

    def is_visible(e: Event) -> bool:
        return not is_forgotten(e.seq) and not pair_omitted(e)

    from .env import disco_env

    driver_has_vision = disco_env("DRIVER_VISION") == "1"
    latest_screenshot_seq = None
    if driver_has_vision:
        latest_screenshot_seq = _find_latest_screenshot_seq(events, is_visible)

    visible_obs = [
        e.seq
        for e in events
        if isinstance(e, ObservationEvent) and e.seq is not None and is_visible(e)
    ]
    recent_obs_seqs = set(visible_obs[-_MASK_KEEP_RECENT:])

    def append_event_message(e: LLMConvertible, msgs: list[LLMMessage], visible: list[int]) -> None:
        if isinstance(e, ObservationEvent):
            msg, seq, vary = _render_observation_message(
                e,
                recent_obs_seqs=recent_obs_seqs,
                pinned=pinned,
                prompt_read_by_observation_seq=prompt_read_by_observation_seq,
                latest_screenshot_seq=latest_screenshot_seq,
            )
            msgs.append(msg)
            if seq is not None:
                if vary:
                    msgs[-1] = _apply_tail_variation(msgs[-1], e)
                visible.append(seq)
            return
        msg = e.to_llm_message()
        msgs.append(msg)
        seq = getattr(e, "seq", None)
        if seq is not None:
            msgs[-1] = _apply_tail_variation(msgs[-1], cast(Event, e))
            visible.append(seq)

    msgs, visible = _walk_events_for_messages(
        events,
        summary_at_start=summary_at_start,
        is_visible_fn=is_visible,
        result_for_action_fn=result_for_action,
        action_for_result_fn=action_for_result,
        append_fn=append_event_message,
    )

    recitation = _recitation_message(events)
    if recitation is not None:
        msgs.append(recitation)

    forgotten_n = sum(b - a + 1 for a, b in forgotten)
    return msgs, visible, len(events), forgotten_n
