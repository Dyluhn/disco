"""Memory: View + Condensation — event-state-contract.md §5.

The tension: an append-only log grows without bound, but the LLM context window
is finite. Resolution (BoD §7.3): never delete; to "forget", append a
`CondensationEvent` tombstone recording a span to drop and a summary to insert.
The `View` computes, at read time, "what the LLM currently sees" by applying
those tombstones. The agent loop and LLM router consume `View.messages` — never
the raw log.

The View's behavior and tombstone semantics are [CONTRACT]. The *condensation
strategy* (when/how much to summarize) is a swappable [INTERIOR] `Condenser`;
Phase 0 ships only the no-op condenser (the real LLMSummarizingCondenser is a
Phase 1 deliverable, BoD §22).

Reversible-compaction tier (C11): the tombstone is a MARKER, not a delete —
the dropped events stay on the append-only log. `recover_span(events,
tombstone)` (and `View.recover_span`) re-materializes the originals on demand.
The default `View.messages` is UNCHANGED by this tier: recovery is an explicit
accessor, not an automatic un-tombstone. The recovered events are returned to
the caller; nothing is re-injected into the live context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Literal, Protocol

from pydantic import BaseModel

from .env import disco_env
from .events import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    DatasourceEvent,
    Event,
    KnowledgeEvent,
    LLMConvertible,
    LLMMessage,
    ObservationEvent,
    PlanEvent,
    RuntimeConstraintEvent,
    StatusEvent,
    agent_view_consistent_events,
)

# BP-06 observation masking (arXiv 2508.21433): old tool-output bodies elide to a
# restorable stub; reasoning/actions stay verbatim. Render-time only — the event
# log always keeps full bodies.
_MASK_KEEP_RECENT = 8  # newest N observation events render in full
_MASK_MIN_CHARS = 600  # smaller bodies are never masked (cheap, often load-bearing)


# Tools that DO change durable state (the workspace / the plan), so their turns are
# never microcompacted even when an attempt failed — a failed file write still may
# have left partial state, and plan_step turns carry progress meaning.
_DURABLE_TOOLS = frozenset({"file_write", "file_append", "file_edit", "plan_step"})


# Tools whose latest observation may carry a rendered-page screenshot to attach to
# the vision-capable driver (BP-00). browser navigations and verify_web_app both
# capture a screenshot_b64 under vision; attach for neither more nor fewer tools.
_SCREENSHOT_TOOLS = frozenset({"browser", "verify_web_app"})


def repair_tool_call_adjacency(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Return a provider-safe message list with dangling tool-call pairs dropped.

    OpenAI-compatible providers require an assistant message with ``tool_calls``
    to be followed immediately by one tool message per call id. A broken history
    cannot be repaired by a model rephrase because the provider rejects the
    request before the model sees it, so the only safe retry payload is one that
    removes incomplete assistant/tool fragments.
    """
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
            # Broken assistant tool call: drop it. Any separated tool result
            # becomes an orphan and is dropped by the role=="tool" arm below.
            i += 1
            continue
        if msg.role == "tool":
            # Orphan tool result, or a tool result split away from a dropped
            # assistant call. Keeping it would trigger the same provider error.
            i += 1
            continue
        out.append(msg)
        i += 1
    return out


def microcompact(events: list[Event]) -> list[CondensationEvent]:
    """S3 Microcompact (GAP A) — a NO-MODEL, deterministic pass that tombstones
    NO-OP turns: a tool call that FAILED and was later SUPERSEDED by an IDENTICAL
    (same tool + same arguments) call that SUCCEEDED. The failed attempt holds no
    durable state and no information the successful retry doesn't — so it just eats
    context. Drop it (reversibly — the bytes stay on the log) with a one-line
    tombstone.

    Conservative by construction (dropping something useful would corrupt the
    model's context):
      - only EXACT retries (same tool_name + same arguments) count as a supersession;
      - durable-state tools (file writes, plan_step) are never touched;
      - only the action+observation pair when they are ADJACENT (obs.seq ==
        action.seq + 1) — so we never sweep an unrelated event caught between them;
      - spans already covered by a tombstone are skipped (idempotent — safe to run
        every step).
    Returns the tombstones to append (possibly empty)."""
    # Already-forgotten seqs — never re-tombstone them.
    forgotten: list[tuple[int, int]] = [
        (e.forgotten_start_seq, e.forgotten_end_seq)
        for e in events
        if isinstance(e, CondensationEvent)
    ]

    def already_forgotten(seq: int) -> bool:
        return any(a <= seq <= b for a, b in forgotten)

    # Pair each action with its observation (by action_id), keyed by (tool, args).
    obs_by_action: dict[str, ObservationEvent] = {
        e.action_id: e for e in events if isinstance(e, ObservationEvent) and e.action_id
    }
    # Which (tool, args) keys later SUCCEEDED — the supersession signal.
    succeeded_keys: set[str] = set()
    for a in events:
        if not isinstance(a, ActionEvent) or a.tool_call is None:
            continue
        o = obs_by_action.get(a.id)
        if o is not None and o.tool_result.success:
            succeeded_keys.add(_call_key(a))

    tombstones: list[CondensationEvent] = []
    for a in events:
        if not isinstance(a, ActionEvent) or a.tool_call is None:
            continue
        if a.tool_call.tool_name in _DURABLE_TOOLS:
            continue
        o = obs_by_action.get(a.id)
        if o is None or o.tool_result.success:
            continue  # only FAILED attempts are candidates
        if a.seq is None or o.seq is None or o.seq != a.seq + 1:
            continue  # require adjacency — never sweep an event caught between
        if already_forgotten(a.seq):
            continue
        if _call_key(a) not in succeeded_keys:
            continue  # not superseded → the failure still carries information; keep it
        tombstones.append(
            CondensationEvent(
                forgotten_start_seq=a.seq,
                forgotten_end_seq=o.seq,
                summary=(
                    f"[microcompacted: a failed `{a.tool_call.tool_name}` attempt was "
                    "dropped — an identical call succeeded later]"
                ),
                summary_role="user",
            )
        )
    return tombstones


def _call_key(a: ActionEvent) -> str:
    """A stable identity for a tool call: tool name + canonical args. Two calls with
    the same key are 'the same call' for supersession purposes."""
    return (
        a.tool_call.tool_name
        + "\x00"
        + json.dumps(a.tool_call.arguments or {}, sort_keys=True, default=str)
    )


def recover_span(events: list[Event], tombstone: CondensationEvent) -> list[Event]:
    """C11 — Reversible-compaction tier: re-materialize the ORIGINAL events a
    tombstone dropped from `View.messages`.

    Background: a `CondensationEvent` does NOT delete the events it covers — it is
    a MARKER on the append-only log (BoD Principle 3, event-state-contract §3)
    that records a seq range + a summary. `View.of` uses the range to FILTER
    events out of what the LLM sees; the bytes themselves stay on disk. This
    function is the mirror of that filter: it walks the log and returns the
    events whose seq falls in `[tombstone.forgotten_start_seq,
    tombstone.forgotten_end_seq]` (inclusive), in their original form and
    insertion order. The summary that replaced them in the View is NOT
    returned — this returns the dropped originals, not their substitute.

    On-demand + EXPLICIT only. The function does not mutate the event log, the
    View, the tombstone, or any other state. The default `View.messages` is
    unaffected. Recovery is an ACCESSOR for callers that need the originals
    (e.g. a debug/audit surface, a "what did the model forget?" explainer, a
    re-derivation tool that wants to re-evaluate a span against fresh context).
    The recovered events are returned to the caller; nothing is re-injected
    into the live context (no bloat, no accidental un-tombstone).

    Edge cases:
      - Degenerate range (start > end) → `[]`.
      - No events in `events` match the range (e.g. a tombstone whose range
        predates the log, or whose events were never persisted) → `[]`.
      - A tombstone inside the range (an OVERLAPPING or NESTED tombstone) is
        itself returned as a `CondensationEvent`; the log is the source of
        truth, and re-running the function on the inner tombstone re-walks
        the same log to resolve its span. The original events are NEVER
        destroyed, so they remain retrievable regardless of how many later
        tombstones also "forget" the same seq."""
    start, end = tombstone.forgotten_start_seq, tombstone.forgotten_end_seq
    if start > end:
        return []
    return [e for e in events if e.seq is not None and start <= e.seq <= end]


def _latest_plan(events: list[Event]) -> PlanEvent | None:
    """The maximal-revision PlanEvent — the plan the agent is currently executing.
    A re-plan supersedes prior ones."""
    plan: PlanEvent | None = None
    for e in events:
        if isinstance(e, PlanEvent) and (plan is None or e.revision >= plan.revision):
            plan = e
    return plan


def effective_plan_progress(
    events: list[Event],
) -> tuple[PlanEvent | None, dict[int, str]]:
    """The SINGLE source of truth for per-step plan completion. Merges BOTH progress
    channels: incremental `plan_step(index, state)` marks (small models) AND declarative
    `update_plan_progress({steps:[{index,state}...]})` full-state snapshots (capable
    models — the #3 redesign). Returns (latest_plan, {1-based index: effective state}).

    Why this exists: capable models report progress ONLY via update_plan_progress; the
    old plan_step-only readers saw 0/N done for them, so a finished build PAUSED on the
    actionless valve and the model's own recap kept showing the plan incomplete (it kept
    verifying). Every completion/recap reader now flows through this one function so the
    valve, the tail recap, the recitation drift-gate, and resume all agree.

    Resolution: only events AFTER the latest plan count directly.  A newly approved
    replacement may carry a prior ``done`` mark only at the same index when the
    complete frozen PlanStep contract is exactly unchanged; pending proposals and
    changed title/detail/predicate contracts reset. Marks/snapshots apply in event
    order — by `seq`, falling back to LIST
    POSITION when seqs are absent/zero (seqless fixtures) — and LATEST WINS, so a step can
    move done→active if a later event says so. A `plan_step` sets ONE index; an
    `update_plan_progress` applies the states it LISTS; indices a snapshot omits RETAIN
    their prior known state (a partial snapshot never silently un-completes an omitted
    step). Indices are bounded to the current plan (1..N); out-of-range marks are ignored.
    Steps never mentioned have no entry (callers treat absent as not-done)."""
    plan: PlanEvent | None = None
    plan_pos = -1
    for i, e in enumerate(events):
        if isinstance(e, PlanEvent) and (plan is None or e.revision >= plan.revision):
            plan, plan_pos = e, i
    if plan is None or not plan.steps:
        return (None, {})
    plan_seq = plan.seq or 0
    total = len(plan.steps)
    states: dict[int, str] = {}

    # Deterministic completed-work carry is approval-bound, not chronology-bound.
    # Reconstruct the predecessor from the durable transition and raw prefix so a
    # restart or condensation makes the same decision.  Only exact, same-index
    # frozen PlanStep equality carries completion; active/pending states and fuzzy
    # title matches never cross a revision boundary.
    approval = next(
        (
            event
            for event in events[plan_pos + 1 :]
            if isinstance(event, StatusEvent)
            and event.detail == "plan_approved"
            and event.plan_verification_transition is not None
            and event.plan_verification_transition.new_plan_event_id == plan.id
            and event.plan_verification_transition.new_plan_revision == plan.revision
        ),
        None,
    )
    if approval is not None and approval.plan_verification_transition is not None:
        transition = approval.plan_verification_transition
        prior_plan, prior_states = effective_plan_progress(events[:plan_pos])
        if (
            prior_plan is not None
            and prior_plan.id == transition.old_plan_event_id
            and prior_plan.revision == transition.old_plan_revision
        ):
            for index, (prior_step, current_step) in enumerate(
                zip(prior_plan.steps, plan.steps, strict=False),
                start=1,
            ):
                if prior_step == current_step and prior_states.get(index) == "done":
                    states[index] = "done"

    def _set(idx_raw: object, state_raw: object) -> None:
        idx = int(idx_raw)  # type: ignore[arg-type]
        if 1 <= idx <= total:  # bound to the current plan; ignore stale out-of-range
            states[idx] = str(state_raw)

    for i, e in enumerate(events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        eseq = e.seq or 0
        # Scope to AFTER the current plan: prefer seq; fall back to list position when
        # either seq is absent/zero (so seqless fixtures don't carry pre-replan marks).
        if eseq and plan_seq:
            if eseq < plan_seq:
                continue
        elif i < plan_pos:
            continue
        name = e.tool_call.tool_name
        args = e.tool_call.arguments or {}
        if name == "plan_step":
            try:
                _set(args.get("index"), args.get("state"))
            except (TypeError, ValueError):
                continue
        elif name == "update_plan_progress":
            for s in args.get("steps") or []:
                try:
                    _set(s.get("index"), s.get("state"))
                except (TypeError, ValueError, AttributeError):
                    continue
    return (plan, states)


_LOG = logging.getLogger("disco.view")


# Tool-call PROTOCOL residue. Deliberately a short, literal list of the shapes a
# provider actually leaks -- not a generic "looks like XML" rule.
#
# The distinction matters more than it first appears. A summary of a React build
# legitimately contains `<div>`, `<Button />`, `</section>`; a summary of a shell
# session legitimately contains `#!/bin/sh` and `2>&1`. A rule broad enough to
# catch protocol markup by its angle brackets would eat all of that, and a
# condenser that rejects honest summaries is worse than one that occasionally
# passes junk. So: only delimiters that carry no meaning outside a tool-call
# protocol. `openai_provider.py` already strips `</parameter>` on the tool-ARGUMENT
# path for exactly this reason; the summary path simply never checked.
_PROTOCOL_MARKERS: tuple[str, ...] = (
    "<parameter",
    "</parameter>",
    "<function_calls>",
    "</function_calls>",
    "<invoke ",
    "</invoke>",
    "<tool_call",
    "</tool_call>",
    "<|tool_call",
    '"tool_calls":',
    '"tool_call_id":',
)

_SUMMARY_REPAIR_INSTRUCTION = (
    "Your previous summary contained tool-call protocol markup. Re-write it as "
    "plain prose describing what happened. Do not include any tool-call syntax "
    "such as <parameter>, <invoke>, <function_calls>, or a tool_calls JSON "
    "payload. Ordinary code, HTML or shell snippets are fine when they are part "
    "of what you are describing."
)


def summary_rejection_reason(summary: str) -> str | None:
    """Why this summarizer output must not be persisted, or None if it is fine.

    Structural rejection, not taste: a summary carrying tool-call protocol markup
    is replayed to the model as though it were prose, which is how a provider's
    own protocol ends up looking like conversation history.
    """
    lowered = summary.lower()
    for marker in _PROTOCOL_MARKERS:
        if marker.lower() in lowered:
            return f"contains tool-call protocol markup: {marker!r}"
    return None


def _fallback_summary(start_seq: int, end_seq: int) -> str:
    """A truthful host-authored stand-in when the summarizer cannot be trusted.

    Asserts ONLY what the host knows for certain -- which events were dropped --
    and states plainly that their content was not summarized. It deliberately
    does not invent progress: an honest gap is recoverable, a fabricated summary
    is not.
    """
    return (
        f"[host summary] Events {start_seq}-{end_seq} were removed from context to "
        "stay within the model's window. The summarizer's output was rejected as "
        "unusable, so the work done in that span is NOT summarized here. Treat "
        "this range as unknown rather than as 'nothing happened': re-read files or "
        "re-check state before assuming any step in it was or was not completed."
    )


def _pinned_seqs(events: list[Event]) -> set[int]:
    """Seqs that condensation/masking must not remove from this request.

    This includes the latest plan, durable knowledge/data-source guidance, and
    the minimal revision/range-aware file-read set whose exact bytes are not yet
    duplicated by another retained read. Action and observation halves are
    pinned together so provider tool pairing remains valid. The head user
    message is already protected by ``keep_head``.
    """
    # Import locally: view is a foundational module while the resource fold is
    # a loop projection. The fold itself imports only event/effect value types,
    # so this call introduces no runtime cycle.
    from .loop.resource_context import essential_read_pair_seqs

    pinned: set[int] = set(essential_read_pair_seqs(events))
    plan = _latest_plan(events)
    if plan is not None and plan.seq is not None:
        pinned.add(plan.seq)
    seen_knowledge: set[tuple[str, str]] = set()
    for e in events:
        if isinstance(e, KnowledgeEvent):
            # Decision 3: KnowledgeEvent spam is exempted from pinning protection
            # ONLY when the events are exact duplicates (same scope + same snippet).
            # The first instance stays pinned; subsequent ones are forgettable.
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


def _live_runtime_constraint_seqs(events: list[Event]) -> set[int]:
    """Seqs of the runtime constraints that are still LIVE, one per key.

    Three rules, each answering a way a constraint could go wrong:

    * **One per key.** Only the newest event for a ``constraint_key`` is pinned.
      Re-observing the same prohibition therefore cannot grow context, however
      many times the model retries it.
    * **Superseded generations expire.** A constraint is only true of the host
      capabilities it was observed under. Once any constraint is recorded under a
      newer ``capability_generation``, older-generation constraints stop being
      live -- a prohibition must not outlive the configuration that justified it.
    * **Explicitly lifted constraints are dropped.** ``active=False`` for a key
      retires it rather than leaving a stale rule pinned forever.
    """
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


def _recitation_message(
    events: list[Event], *, context_pack_active: bool = False
) -> LLMMessage | None:
    """GAP D recency recitation: an EPHEMERAL objective + step checklist appended
    at the View TAIL so the goal stays in the high-attention recent window even
    after dozens of tool calls drift the plan toward the forgettable middle.
    Built purely from the log (latest plan + plan_step actions) — no model call,
    regenerated every materialization, never stored (preserves append-only)."""
    # Merged truth-source (plan_step + update_plan_progress) so the tail recap reflects a
    # capable model's DECLARATIVE progress, not just plan_step marks (the #3 redesign).
    # A re-plan starts a fresh checklist (effective_plan_progress scopes to the latest
    # PlanEvent — counting a prior plan's "done" marks would show every step done on the
    # new plan: observed live as "all 5 done" → confused → STUCK).
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
            # Live-caught (agent-05 soak autopsy): a reasoning model ANSWERS this
            # line in a think every turn instead of acting — 259 consecutive
            # "not stale, write the file NOW" thinks. The reminder must forbid
            # the meta-response so passing the gate costs zero turns.
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


class View(BaseModel):
    """The materialized "what the LLM sees right now", computed from the raw
    event log by applying condensation tombstones.

    [CONTRACT] View.of(events) is pure: same events -> same messages, every
    time. Computing it has no side effects and never appends.

    The C11 reversible-compaction tier adds a RECOVERY ACCESSOR:
    `View.recover_span(events, tombstone)` (and the module-level
    `recover_span`) returns the original events a tombstone dropped, on
    explicit demand. The default `View.messages` is NOT affected by recovery —
    it still omits the tombstoned span. Recovery is for callers that need the
    originals (audit, debug, re-derivation); the live LLM context is never
    re-injected with the recovered bytes.
    """

    messages: list[LLMMessage]
    # seqs of events currently visible (post-condensation), for diagnostics.
    visible_seqs: list[int]
    total_events: int
    forgotten_count: int

    @classmethod
    def of(cls, events: list[Event]) -> View:
        events = agent_view_consistent_events(events)
        # 1. Collect forgotten seq ranges + a map of where each summary belongs.
        #    NOTE: we intentionally diverge from the contract's *illustrative*
        #    body, which emits the summary when it reaches the CondensationEvent
        #    in append order. Because a tombstone is appended *after* the span it
        #    forgets, that would place an old summary after recent messages. The
        #    [CONTRACT] behavior (§7.3) is "replace the first half with a single
        #    summary, leave the back half untouched" — i.e. the summary takes the
        #    *position of the forgotten span*. So we key summaries by
        #    forgotten_start_seq and emit them in chronological place.
        forgotten: list[tuple[int, int]] = []
        summary_at_start: dict[int, CondensationEvent] = {}
        for e in events:
            if isinstance(e, CondensationEvent):
                forgotten.append((e.forgotten_start_seq, e.forgotten_end_seq))
                # First tombstone for a given start_seq wins its slot.
                summary_at_start.setdefault(e.forgotten_start_seq, e)

        # GAP D: the latest PlanEvent is pinned — never forgotten even if a
        # tombstone range covers it.
        pinned = _pinned_seqs(events)
        # Authenticated exact file-read bodies in the bounded active resource
        # set must bypass the generic observation snip. Otherwise BP-06 can pin
        # a message whose middle was already destroyed by to_llm_message().
        from .loop.resource_context import (
            read_receipt_records,
            rendered_content_matches_receipt,
            select_prompt_read_records,
        )

        prompt_read_by_observation_seq = {
            record.observation_seq: record
            for record in select_prompt_read_records(read_receipt_records(events))
            if record.observation_seq is not None
        }

        def is_forgotten(seq: int | None) -> bool:
            if seq is not None and seq in pinned:
                return False
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

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

        def result_for_action(
            e: ActionEvent,
        ) -> ObservationEvent | AgentErrorEvent | None:
            return result_by_action_id.get(e.id) or result_by_call_id.get(e.tool_call.call_id)

        def action_for_result(
            e: ObservationEvent | AgentErrorEvent,
        ) -> ActionEvent | None:
            if isinstance(e, ObservationEvent):
                action = action_by_id.get(e.action_id) if isinstance(e.action_id, str) else None
                return action or action_by_call_id.get(e.tool_result.call_id)
            action = action_by_id.get(e.action_id) if isinstance(e.action_id, str) else None
            if action is not None:
                return action
            return (
                action_by_call_id.get(e.tool_call_id) if isinstance(e.tool_call_id, str) else None
            )

        def pair_omitted(e: Event) -> bool:
            if isinstance(e, ActionEvent):
                result = result_for_action(e)
                return result is not None and is_forgotten(result.seq)
            if isinstance(e, ObservationEvent | AgentErrorEvent):
                action = action_for_result(e)
                return action is not None and is_forgotten(action.seq)
            return False

        def is_visible(e: Event) -> bool:
            return not is_forgotten(e.seq) and not pair_omitted(e)

        # BP-00: when the driver has VISION, only the LATEST browser screenshot
        # renders as an image; older ones render as text only to save context.
        # Computed on the post-condensation sequence.
        driver_has_vision = disco_env("DRIVER_VISION") == "1"
        latest_screenshot_seq = None
        if driver_has_vision:
            for e in reversed(events):
                if (
                    isinstance(e, ObservationEvent)
                    and e.seq is not None
                    and is_visible(e)
                    and e.tool_result.tool_name in _SCREENSHOT_TOOLS
                    and (e.tool_result.structured or {}).get("screenshot_b64")
                ):
                    latest_screenshot_seq = e.seq
                    break

        # BP-06: the newest _MASK_KEEP_RECENT visible observations render in
        # full; every older one above _MASK_MIN_CHARS elides to a restorable
        # stub. Computed on the post-condensation sequence, so the window never
        # counts forgotten events.
        visible_obs = [
            e.seq
            for e in events
            if isinstance(e, ObservationEvent) and e.seq is not None and is_visible(e)
        ]
        recent_obs_seqs = set(visible_obs[-_MASK_KEEP_RECENT:])

        # 2. Walk events in seq order. When we reach the start of a forgotten
        #    span, emit its summary (once) in place; drop forgotten and
        #    non-LLMConvertible events; keep the rest.
        msgs: list[LLMMessage] = []
        visible: list[int] = []
        emitted_summary_for: set[int] = set()
        paired_results_rendered: set[int] = set()

        def append_event_message(e: LLMConvertible) -> None:
            if (
                isinstance(e, ObservationEvent)  # exact class — AgentErrorEvent is NOT masked (B4)
                and e.seq is not None
                and e.seq not in recent_obs_seqs
                and e.seq not in pinned
                and len(e.tool_result.content) > _MASK_MIN_CHARS
            ):
                digest = hashlib.sha256(e.tool_result.content.encode()).hexdigest()[:12]
                msgs.append(
                    LLMMessage(
                        role="tool",
                        content=(
                            f"[masked output: {e.tool_result.tool_name} #{e.seq} — "
                            f"{len(e.tool_result.content):,} chars, sha256:{digest}. "
                            "Re-run the tool (or file_read the same path) to see it again.]"
                        ),
                        # keep the pairing valid: provider-side tool_call ↔ tool message
                        tool_call_id=e.tool_result.call_id,
                    )
                )
                visible.append(e.seq)
                return

            exact_read = (
                prompt_read_by_observation_seq.get(e.seq)
                if isinstance(e, ObservationEvent) and e.seq is not None
                else None
            )
            if (
                isinstance(e, ObservationEvent)
                and exact_read is not None
                and rendered_content_matches_receipt(exact_read.receipt, e.tool_result.content)
            ):
                msg = LLMMessage(
                    role="tool",
                    content=e.tool_result.content,
                    tool_call_id=e.tool_result.call_id,
                )
            else:
                msg = e.to_llm_message()
            # BP-00: attach image ONLY to the latest browser screenshot.
            if (
                isinstance(e, ObservationEvent)
                and e.seq == latest_screenshot_seq
                and latest_screenshot_seq is not None
            ):
                # latest_screenshot_seq is only set (above) for an observation
                # whose structured payload carried a screenshot_b64, so the
                # seq-matched event here provably has a non-None `structured`.
                assert e.tool_result.structured is not None
                b64 = e.tool_result.structured.get("screenshot_b64")
                msg = msg.model_copy(update={"images": [f"data:image/png;base64,{b64}"]})

            msgs.append(msg)
            seq = getattr(e, "seq", None)
            if seq is not None:
                # B3: Deterministic-by-seq tail variation (arXiv 2407.10912).
                # Rotate the surface form of AGENT thoughts and TOOL results
                # to prevent the model from over-fitting to a single fixed
                # template, while maintaining KV-cache stability (B5) by
                # pinning the form to the seq.
                if isinstance(e, ActionEvent):
                    variants = [
                        lambda t: t,
                        lambda t: f"Reasoning: {t}",
                        lambda t: f"Thought: {t}",
                    ]
                    f = variants[seq % len(variants)]
                    msgs[-1] = msgs[-1].model_copy(update={"content": f(msgs[-1].content)})
                elif isinstance(e, ObservationEvent):
                    variants = [
                        lambda c: c,
                        lambda c: f"Observation: {c}",
                        lambda c: f"Output: {c}",
                    ]
                    f = variants[seq % len(variants)]
                    msgs[-1] = msgs[-1].model_copy(update={"content": f(msgs[-1].content)})

                visible.append(seq)

        for e in events:
            # Emit a span's summary at the chronological position it replaces.
            if e.seq is not None and e.seq in summary_at_start:
                key = e.seq
                if key not in emitted_summary_for:
                    tomb = summary_at_start[key]
                    msgs.append(LLMMessage(role=tomb.summary_role, content=tomb.summary))
                    emitted_summary_for.add(key)

            if isinstance(e, CondensationEvent):
                continue  # bookkeeping; its summary is emitted in-place above
            if not isinstance(e, LLMConvertible):
                continue  # status/error: never shown to the LLM
            if not is_visible(e):
                continue  # forgotten by a tombstone

            if isinstance(e, ActionEvent):
                result = result_for_action(e)
                if result is not None and is_visible(result):
                    append_event_message(e)
                    append_event_message(result)
                    if result.seq is not None:
                        paired_results_rendered.add(result.seq)
                    continue
            elif isinstance(e, ObservationEvent | AgentErrorEvent):
                action = action_for_result(e)
                if action is not None and is_visible(action):
                    if e.seq is not None and e.seq in paired_results_rendered:
                        continue
                    # Paired results are rendered with their action so no
                    # injected/condensation message can split the provider pair.
                    continue

            append_event_message(e)

        # GAP D recency recitation: append the ephemeral objective+checklist at
        # the TAIL so the goal sits in the high-attention recent window.
        recitation = _recitation_message(events)
        if recitation is not None:
            msgs.append(recitation)

        forgotten_n = sum(b - a + 1 for a, b in forgotten)
        return cls(
            messages=msgs,
            visible_seqs=visible,
            total_events=len(events),
            forgotten_count=forgotten_n,
        )

    def fingerprint(self) -> list[str]:
        """[CONTRACT BP-06/B5] Stable per-message hashes for KV-prefix verification.
        Role + content + images (BP-00: an attached image changes the wire shape —
        content-parts vs plain string — so it MUST change the hash; messages without
        images hash exactly as before). Pure and deterministic across processes."""
        return [
            hashlib.sha256(
                f"{m.role}{m.content}{''.join(m.images) if m.images else ''}".encode()
            ).hexdigest()[:16]
            for m in self.messages
        ]

    @classmethod
    def recover_span(cls, events: list[Event], tombstone: CondensationEvent) -> list[Event]:
        """C11 — Reverse of condensation for a single tombstone. Returns the
        events the tombstone dropped from `View.of(events).messages`, in their
        original form and insertion order. PURE: no mutation of the log, the
        View, or any other state. The default `View.messages` is unaffected.

        Thin wrapper over the module-level `recover_span`; provided for
        discoverability and to sit alongside `View.of` as a paired
        "materialize the condensed view" / "recover a dropped span" surface.
        See `recover_span` for the full contract."""
        return recover_span(events, tombstone)


# ---- Condenser / Summarizer seams (§5.2) ------------------------------------


class CondensationRequest(BaseModel):
    soft: bool  # soft = maintain a bound; hard = must condense now
    reason: Literal["tokens", "events", "request", "hard_reset"]


class Summarizer(Protocol):
    """Produces summary text for a span of messages. [CONTRACT at the LLM
    boundary] — implemented by the LLM router using a CHEAP model, separate
    from the agent model (BoD §7.3, §15)."""

    async def summarize(self, messages: list[LLMMessage]) -> str: ...


def _build_pointer_manifest(span: list[Event], artifact_paths: list[str]) -> str:
    """C16 — pointer-only summary used by the hard_reset path. Categorize the
    paths the engine collected so the model sees what kind of artifact each
    pointer is (deliverable / spill / memory) without having to guess. Pure:
    no model call, deterministic per input list. The summarizer is unused on
    this path — the dropped span is too large to re-ingest verbatim anyway, and
    a lossy prose recap would be worse than pointing to the bytes on disk.

    `span` is the LIVE span (events about to be forgotten) — used only to put
    a concrete seq range in the header so the model can refer to the drop
    ('the drop covering seq 7–42'). The manifest's RECOVERABILITY comes from
    `artifact_paths`: the engine has already verified every one of them
    resolves on disk before passing them in, so the manifest is honest."""
    start_seq = span[0].seq
    end_seq = span[-1].seq
    n = len(span)
    # Categorize — these prefixes are the public surface area the engine +
    # tools layer already use (see engine.py:1787 + the .pmx/ directory).
    deliverables: list[str] = []
    spills: list[str] = []
    memory: list[str] = []
    other: list[str] = []
    for p in artifact_paths:
        bn = os.path.basename(p)
        if bn.startswith(".disco-spill-"):
            spills.append(p)
        elif p.endswith(".pmx/MEMORY.md") or bn == "MEMORY.md":
            memory.append(p)
        else:
            # Heuristic: anything the agent mutated via the mutating-tool set
            # (file_write / file_edit / file_append / ...) — the engine passes
            # those as deliverables. A path that does not match any bucket
            # falls into `other` so the model still sees it (we never want to
            # silently drop a pointer the engine went to the trouble of
            # collecting).
            deliverables.append(p)
    # Render — order: deliverables first (the user's real work), then spill
    # logs (overflow context the model might re-grep), then memory (standing
    # facts), then anything uncategorized. Stable, de-duped (input is
    # already de-duped by the engine).
    lines: list[str] = []
    lines.append(
        f"[Hard reset — pointer-only flush; span seq {start_seq}–{end_seq} "
        f"({n} events) DROPPED from the live view.]"
    )
    lines.append(
        "The dropped content is NOT summarized here. It is fully recoverable "
        "from on-disk artifacts in your workspace. Use file_read (or grep) to "
        "retrieve the original content when you need it — the pointer list "
        "below IS the manifest, and the bytes are on disk RIGHT NOW. Do NOT "
        "trust your prose memory of the dropped span."
    )
    if deliverables:
        lines.append("")
        lines.append("Deliverables (files the agent wrote this run):")
        for p in deliverables:
            lines.append(f"  • {p}")
    if spills:
        lines.append("")
        lines.append("Spill logs (shell-output overflow; head/tail preserved):")
        for p in spills:
            lines.append(f"  • {p}")
    if memory:
        lines.append("")
        lines.append("Standing memory (recorded this run):")
        for p in memory:
            lines.append(f"  • {p}")
    if other:
        lines.append("")
        lines.append("Other on-disk artifacts (uncategorized):")
        for p in other:
            lines.append(f"  • {p}")
    return "\n".join(lines)


class Condenser(Protocol):
    """Decides whether/how to condense. The loop calls should_condense() (sync,
    pure over the View) then awaits condense() (async); the strategy is
    [INTERIOR] and swappable. condense() returns at most one CondensationEvent to
    append, or None (no-op). No event is ever deleted.

    Async rationale (event-state-contract v1.2 §5.2): condense() must be async
    because the only correct summarizer makes an async router call, invoked from
    the loop's running asyncio loop. should_condense stays sync (pure).

    `reason` (C16) tells the condenser WHICH call site invoked it:
      - "tokens"      → soft/maintain-bound path (the default; what
                        should_condense → condense produces). UNCHANGED: a
                        prose summary from the summarizer.
      - "hard_reset"  → the loop's context-overflow escape hatch. The dropped
                        span is too large to re-summarize usefully, so the
                        tombstone is a pointer manifest of on-disk artifacts
                        the model can re-read selectively (NOT a prose recap).
                        Caller supplies `artifact_paths` (verified to exist
                        on disk); without them the condenser makes no
                        progress and returns None (same as an empty prose
                        summary on the soft path)."""

    def should_condense(
        self, view: View, *, token_count: int | None
    ) -> CondensationRequest | None: ...

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer: Summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ) -> CondensationEvent | None: ...


class NoOpCondenser:
    """Phase 0 placeholder satisfying the `Condenser` protocol: never condenses.

    This lets the View/loop wire up against the real interface now; the
    `LLMSummarizingCondenser` is the real strategy used on the Agent surface.
    """

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        return None

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer: Summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ) -> CondensationEvent | None:
        return None


class LLMSummarizingCondenser:
    """[INTERIOR] The real condenser (§5.2). When the View grows past a token bound,
    summarize the OLDEST forgettable span — keeping an anchoring HEAD (the initial
    instruction) and a RECENT TAIL untouched — into ONE `CondensationEvent` tombstone.
    `View.of` places the summary at the span's chronological position, so the loop
    keeps going coherently instead of overflowing the context window.

    - Triggers: SOFT at `max_tokens` (maintain the bound), HARD at `hard_max_tokens`.
    - Guards: `keep_head` + `keep_recent` events are never forgotten; `min_forget`
      ensures each condensation makes real PROGRESS (no churn on tiny spans).

    `should_condense` is sync/pure over the View's token estimate; `condense` awaits the
    SUMMARIZER-role model (a cheap, separate model — BoD §7.3/§15) and never deletes an
    event (it appends a tombstone). It also self-computes its span, so the loop's
    hard-reset path (`_hard_reset`, which calls `condense` directly) works too.
    """

    def __init__(
        self,
        *,
        context_window: int | None = None,
        soft_frac: float = 0.65,
        hard_frac: float = 0.80,
        max_tokens: int | None = None,
        hard_max_tokens: int | None = None,
        keep_head: int = 1,
        keep_recent: int = 6,
        min_forget: int = 2,
        budget_tokens: int = 24_000,
        hard_budget_tokens: int = 32_000,
    ) -> None:
        # C9 — honor the LIVE context window, not a fixed ~24k cap.
        #
        # Derive trigger thresholds from the ROUTED model's context window
        # (soft=65%, hard=80%). When `window × frac` already fits UNDER the working
        # budget, use it unchanged (small-window path: 16k→10.4k, 32k→20.8k, etc.).
        # When the window-fraction EXCEEDS the budget, grow above the budget UP TO a
        # sane ceiling (4× working budget = 96k soft / 128k hard by default) — a
        # 128k-window model now condenses at 83k (its 65% point), a 200k model caps
        # at 96k, a 1M model still caps at 96k. Cost scales with input tokens PER
        # CALL so we still cap growth — but the cap is no longer so tight that a
        # big-window model never gets to use its room. The budget is the FLOOR
        # (anything below it stays at the window-fraction) and the ceiling is the
        # CEILING (a 1M model can't bloat to 681k and re-send 60k+ every action).
        #
        # Unknown window → the working budget IS the bound (unchanged). Explicit
        # max_tokens / hard_max_tokens still override (tests). The small-window
        # branch is byte-identical to the previous formula for any model whose
        # window × frac ≤ budget (preserves H1 contract for that range).
        soft_ceiling = budget_tokens * 4  # 96_000 by default — well above 24k, well below 1M
        hard_ceiling = hard_budget_tokens * 4  # 128_000 by default
        if context_window is not None:
            window_soft = int(context_window * soft_frac)
            window_hard = int(context_window * hard_frac)
            if window_soft <= budget_tokens:
                derived_soft = window_soft  # small-window path: unchanged from before
            else:
                derived_soft = min(window_soft, soft_ceiling)  # grow above budget, bounded
            if window_hard <= hard_budget_tokens:
                derived_hard = window_hard
            else:
                derived_hard = min(window_hard, hard_ceiling)
        else:
            # No window known → the working budget IS the bound.
            derived_soft, derived_hard = budget_tokens, hard_budget_tokens
        self._max = max_tokens if max_tokens is not None else derived_soft
        self._hard = hard_max_tokens if hard_max_tokens is not None else derived_hard
        self._keep_head = keep_head
        self._keep_recent = keep_recent
        self._min_forget = min_forget

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        if token_count is None:
            return None
        if token_count >= self._hard:
            return CondensationRequest(soft=False, reason="tokens")  # must condense now
        if token_count >= self._max:
            return CondensationRequest(soft=True, reason="tokens")  # maintain the bound
        return None

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer: Summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ) -> CondensationEvent | None:
        forgotten = [
            (e.forgotten_start_seq, e.forgotten_end_seq)
            for e in events
            if isinstance(e, CondensationEvent)
        ]

        def is_forgotten(seq: int | None) -> bool:
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

        # The still-live, LLM-visible events in seq order (already-forgotten dropped).
        live: list[Event] = [
            e
            for e in events
            if isinstance(e, LLMConvertible) and e.seq is not None and not is_forgotten(e.seq)
        ]

        # C10 — `keep_recent` counts TOOL-TURNS, not raw events.
        #
        # A "turn" is a maximal contiguous group of events that all belong to one
        # tool call: an ActionEvent paired with its observation/agent_error (the
        # normal 2-event case), a free-standing observation/agent_error whose
        # action is not in `live` (action was already forgotten — single-event
        # turn), or another LLMConvertible event (user/agent message, knowledge,
        # plan, … — also single-event turn). Slicing the keep boundary at a turn
        # start means the boundary NEVER falls between an action and its
        # observation: retaining `keep_recent=k` keeps k COMPLETE turns, never
        # half a turn. (`keep_head` semantics are unchanged — head is still
        # exactly `keep_head` events from the start of `live`.)
        last_action: ActionEvent | None = None
        turn_starts: list[int] = [0]
        for i in range(1, len(live)):
            e = live[i]
            if isinstance(e, ActionEvent):
                turn_starts.append(i)
                last_action = e
            elif isinstance(e, (ObservationEvent, AgentErrorEvent)):
                if last_action is not None and e.action_id == last_action.id:
                    continue  # observation/agent_error pairs with the current turn's action
                turn_starts.append(i)  # orphan — its action isn't in `live`
                last_action = None
            else:
                # MessageEvent / KnowledgeEvent / PlanEvent / … — standalone turn
                turn_starts.append(i)
                last_action = None

        # Need a non-empty head, at least K complete turns in the recent tail, and
        # at least `min_forget` events in the middle — else there's nothing worth
        # forgetting yet (the minimum-progress guard).
        n_turns = len(turn_starts)
        if len(live) < self._keep_head + self._min_forget:
            return None
        if n_turns <= self._keep_recent:
            return None  # the K tail turns would consume every turn — no middle
        # The recent tail starts at the K-th-from-last turn. The middle is
        # everything between the head and that turn, sliced at a turn boundary.
        tail_start = turn_starts[n_turns - self._keep_recent]
        if tail_start <= self._keep_head:
            return None  # the head reaches (or overlaps) the tail — no middle
        span = live[self._keep_head : tail_start]
        if len(span) < self._min_forget:
            return None

        start_seq, end_seq = span[0].seq, span[-1].seq
        if start_seq is None or end_seq is None:  # filtered above; assertion for the type
            return None

        # C16 — `hard_reset` is a POINTER-ONLY flush, not a prose recap.
        #
        # The soft path (reason="tokens", the default) is byte-unchanged: it
        # calls the summarizer and produces a prose summary. The hard_reset
        # path is the loop's context-overflow escape hatch — the span is
        # already too big to re-ingest verbatim, so a lossy prose recap is
        # WORSE than pointing the model at the bytes on disk. The engine has
        # already verified every path in `artifact_paths` exists before
        # passing them in, so the manifest is honest (every pointer resolves).
        # Without artifacts there's nothing to point to → no progress → None
        # (same shape as an empty prose summary on the soft path).
        if reason == "hard_reset":
            if not artifact_paths:
                return None  # no recoverable artifacts — the soft path's empty-summary behavior
            return CondensationEvent(
                forgotten_start_seq=start_seq,
                forgotten_end_seq=end_seq,
                summary=_build_pointer_manifest(span, list(artifact_paths)),
                summary_role="user",
                reason="hard_reset",
            )

        # HS-02: hand the FULL `view.messages` to the summarizer (not just the
        # slice) so the prior anchored summary — which View.of already placed
        # at the chronological position of the forgotten span — is visible to
        # it. The summarizer scans for the `GOAL:` marker and selects
        # UPDATE-in-place vs CREATE-fresh accordingly; passing the slice would
        # strip the prior summary and force a fresh recap on every condensation.
        # The instructions themselves tell the model to leave the recent tail
        # out of the structured summary, so the extra context is a feature, not
        # noise. Pinned plans / knowledge / datasources (GAP D / Cluster 7)
        # remain in `view.messages` because View.of exempts them from forgetting
        # — the summarizer can read them and decide what to surface in PROGRESS.
        summary = await summarizer.summarize(view.messages)
        if not summary.strip():
            return None  # an empty summary would forget context for nothing

        rejection = summary_rejection_reason(summary)
        if rejection is not None:
            # ONE bounded repair. The span is about to be forgotten either way,
            # so a single corrective re-ask is worth it -- but only one: retrying
            # a summarizer that emits protocol markup tends to emit it again, and
            # an unbounded loop here burns the context budget the condensation
            # exists to protect.
            _LOG.warning("summarizer output rejected (%s); requesting one repair", rejection)
            repaired = await summarizer.summarize(
                [*view.messages, LLMMessage(role="user", content=_SUMMARY_REPAIR_INSTRUCTION)]
            )
            if repaired.strip() and summary_rejection_reason(repaired) is None:
                summary = repaired
            else:
                # Both attempts unusable. Persist a TRUTHFUL host-generated
                # fallback rather than protocol markup: it asserts only what the
                # host itself knows (the seq range) and says plainly that the
                # progress in that span was not summarized. Claiming a summary we
                # do not have would be worse than admitting the gap.
                _LOG.warning("summarizer repair also rejected; using truthful fallback")
                summary = _fallback_summary(start_seq, end_seq)
        return CondensationEvent(
            forgotten_start_seq=start_seq,
            forgotten_end_seq=end_seq,
            summary=summary,
            summary_role="user",
            reason="tokens",
        )
