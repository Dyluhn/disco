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
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Literal, Protocol

from pydantic import BaseModel

from .events import (
    ActionEvent,
    CondensationEvent,
    DatasourceEvent,
    Event,
    KnowledgeEvent,
    LLMConvertible,
    LLMMessage,
    ObservationEvent,
    PlanEvent,
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
    return a.tool_call.tool_name + "\x00" + json.dumps(
        a.tool_call.arguments or {}, sort_keys=True, default=str
    )


def _latest_plan(events: list[Event]) -> PlanEvent | None:
    """The maximal-revision PlanEvent — the plan the agent is currently executing.
    A re-plan supersedes prior ones."""
    plan: PlanEvent | None = None
    for e in events:
        if isinstance(e, PlanEvent) and (plan is None or e.revision >= plan.revision):
            plan = e
    return plan


def _pinned_seqs(events: list[Event]) -> set[int]:
    """Seqs that condensation must NEVER forget: the latest PlanEvent (GAP D) plus
    every KnowledgeEvent and DatasourceEvent (Cluster 7 — standing guidance and
    durable API contracts must survive verbatim across a long run instead of
    dissolving into a lossy summary). The head user message is already protected
    by `keep_head`."""
    pinned: set[int] = set()
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
    return pinned


def _recitation_message(events: list[Event]) -> LLMMessage | None:
    """GAP D recency recitation: an EPHEMERAL objective + step checklist appended
    at the View TAIL so the goal stays in the high-attention recent window even
    after dozens of tool calls drift the plan toward the forgettable middle.
    Built purely from the log (latest plan + plan_step actions) — no model call,
    regenerated every materialization, never stored (preserves append-only)."""
    plan = _latest_plan(events)
    if plan is None or not plan.steps:
        return None
    # Only count plan_step marks made AFTER the current plan was proposed. A re-plan
    # (new PlanEvent) starts a fresh checklist — counting the PRIOR plan's "done"
    # marks would show every step done on the new plan (observed live: after a
    # re-plan the model saw "all 5 done", got confused, looped → STUCK).
    plan_seq = plan.seq or 0
    done: set[int] = set()
    active: set[int] = set()
    for e in events:
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        if e.tool_call.tool_name != "plan_step":
            continue
        if (e.seq or 0) < plan_seq:
            continue  # belongs to a superseded plan
        try:
            idx = int(e.tool_call.arguments.get("index"))  # type: ignore[arg-type]
            state = str(e.tool_call.arguments.get("state"))
        except (TypeError, ValueError):
            continue
        if state == "done":
            done.add(idx)
        elif state == "active":
            active.add(idx)
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
    """

    messages: list[LLMMessage]
    # seqs of events currently visible (post-condensation), for diagnostics.
    visible_seqs: list[int]
    total_events: int
    forgotten_count: int

    @classmethod
    def of(cls, events: list[Event]) -> View:
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

        def is_forgotten(seq: int | None) -> bool:
            if seq is not None and seq in pinned:
                return False
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

        # BP-00: when the driver has VISION, only the LATEST browser screenshot
        # renders as an image; older ones render as text only to save context.
        # Computed on the post-condensation sequence.
        driver_has_vision = os.environ.get("PMX_DRIVER_VISION") == "1"
        latest_screenshot_seq = None
        if driver_has_vision:
            for e in reversed(events):
                if (
                    isinstance(e, ObservationEvent)
                    and e.seq is not None
                    and not is_forgotten(e.seq)
                    and e.tool_result.tool_name == "browser"
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
            if isinstance(e, ObservationEvent)
            and e.seq is not None
            and not is_forgotten(e.seq)
        ]
        recent_obs_seqs = set(visible_obs[-_MASK_KEEP_RECENT:])

        # 2. Walk events in seq order. When we reach the start of a forgotten
        #    span, emit its summary (once) in place; drop forgotten and
        #    non-LLMConvertible events; keep the rest.
        msgs: list[LLMMessage] = []
        visible: list[int] = []
        emitted_summary_for: set[int] = set()
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
            if is_forgotten(e.seq):
                continue  # forgotten by a tombstone

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
            else:
                msg = e.to_llm_message()
                # BP-00: attach image ONLY to the latest browser screenshot.
                if (
                    isinstance(e, ObservationEvent)
                    and e.seq == latest_screenshot_seq
                    and latest_screenshot_seq is not None
                ):
                    b64 = e.tool_result.structured.get("screenshot_b64")
                    msg = msg.model_copy(update={"images": [f"data:image/png;base64,{b64}"]})

                msgs.append(msg)
                if e.seq is not None:
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
                        f = variants[e.seq % len(variants)]
                        msgs[-1] = msgs[-1].model_copy(update={"content": f(msgs[-1].content)})
                    elif isinstance(e, ObservationEvent):
                        variants = [
                            lambda c: c,
                            lambda c: f"Observation: {c}",
                            lambda c: f"Output: {c}",
                        ]
                        f = variants[e.seq % len(variants)]
                        msgs[-1] = msgs[-1].model_copy(update={"content": f(msgs[-1].content)})

                    visible.append(e.seq)

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


# ---- Condenser / Summarizer seams (§5.2) ------------------------------------


class CondensationRequest(BaseModel):
    soft: bool  # soft = maintain a bound; hard = must condense now
    reason: Literal["tokens", "events", "request", "hard_reset"]


class Summarizer(Protocol):
    """Produces summary text for a span of messages. [CONTRACT at the LLM
    boundary] — implemented by the LLM router using a CHEAP model, separate
    from the agent model (BoD §7.3, §15)."""

    async def summarize(self, messages: list[LLMMessage]) -> str: ...


class Condenser(Protocol):
    """Decides whether/how to condense. The loop calls should_condense() (sync,
    pure over the View) then awaits condense() (async); the strategy is
    [INTERIOR] and swappable. condense() returns at most one CondensationEvent to
    append, or None (no-op). No event is ever deleted.

    Async rationale (event-state-contract v1.2 §5.2): condense() must be async
    because the only correct summarizer makes an async router call, invoked from
    the loop's running asyncio loop. should_condense stays sync (pure)."""

    def should_condense(
        self, view: View, *, token_count: int | None
    ) -> CondensationRequest | None: ...

    async def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
    ) -> CondensationEvent | None: ...


class NoOpCondenser:
    """Phase 0 placeholder satisfying the `Condenser` protocol: never condenses.

    This lets the View/loop wire up against the real interface now; the
    `LLMSummarizingCondenser` is the real strategy used on the Agent surface.
    """

    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None:
        return None

    async def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
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
        # Derive trigger thresholds from the ROUTED model's context window (soft=65%,
        # hard=80%) BUT CAP them at a fixed WORKING BUDGET (H1). Cost scales with input
        # tokens PER CALL — a huge window (e.g. DeepSeek 1M → 65% = 681k) is capacity,
        # NOT a license to re-send 60k+ every action. So `soft = min(0.65×window,
        # budget)`: a big-window model still condenses at ~24k, keeping the driver's
        # context rich-but-bounded (recent + plan + pins + a running summary), not the
        # full growing transcript. Explicit max_tokens/hard_max_tokens still override
        # (tests). The budget is the binding constraint on any model ≥ ~37k window.
        if context_window is not None:
            derived_soft = min(int(context_window * soft_frac), budget_tokens)
            derived_hard = min(int(context_window * hard_frac), hard_budget_tokens)
        else:
            # No window known → the working budget IS the bound.
            derived_soft, derived_hard = budget_tokens, hard_budget_tokens
        self._max = max_tokens if max_tokens is not None else derived_soft
        self._hard = hard_max_tokens if hard_max_tokens is not None else derived_hard
        self._keep_head = keep_head
        self._keep_recent = keep_recent
        self._min_forget = min_forget

    def should_condense(
        self, view: View, *, token_count: int | None
    ) -> CondensationRequest | None:
        if token_count is None:
            return None
        if token_count >= self._hard:
            return CondensationRequest(soft=False, reason="tokens")  # must condense now
        if token_count >= self._max:
            return CondensationRequest(soft=True, reason="tokens")  # maintain the bound
        return None

    async def condense(
        self, events: list[Event], view: View, *, summarizer: Summarizer
    ) -> CondensationEvent | None:
        forgotten = [
            (e.forgotten_start_seq, e.forgotten_end_seq)
            for e in events
            if isinstance(e, CondensationEvent)
        ]

        def is_forgotten(seq: int | None) -> bool:
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

        # The still-live, LLM-visible events in seq order (already-forgotten dropped).
        live = [
            e
            for e in events
            if isinstance(e, LLMConvertible) and e.seq is not None and not is_forgotten(e.seq)
        ]
        # Need an anchoring head + a recent tail AND at least `min_forget` in between —
        # else there's nothing worth forgetting yet (the minimum-progress guard).
        if len(live) < self._keep_head + self._keep_recent + self._min_forget:
            return None
        span = live[self._keep_head : len(live) - self._keep_recent]
        if len(span) < self._min_forget:
            return None

        start_seq, end_seq = span[0].seq, span[-1].seq
        if start_seq is None or end_seq is None:  # filtered above; assertion for the type
            return None
        # GAP D: don't feed the pinned plan into the summarizer — it stays
        # rendered verbatim (View.of exempts it), so summarizing it is waste.
        pinned = _pinned_seqs(events)
        span_for_summary = [e for e in span if e.seq not in pinned]
        summary = await summarizer.summarize(
            [e.to_llm_message() for e in span_for_summary]
        )
        if not summary.strip():
            return None  # an empty summary would forget context for nothing
        return CondensationEvent(
            forgotten_start_seq=start_seq,
            forgotten_end_seq=end_seq,
            summary=summary,
            summary_role="user",
            reason="tokens",
        )
