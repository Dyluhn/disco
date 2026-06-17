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
        driver_has_vision = disco_env("DRIVER_VISION") == "1"
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
                    # latest_screenshot_seq is only set (above) for an observation
                    # whose structured payload carried a screenshot_b64, so the
                    # seq-matched event here provably has a non-None `structured`.
                    assert e.tool_result.structured is not None
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


def _build_pointer_manifest(
    span: list[Event], artifact_paths: list[str]
) -> str:
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
        return CondensationEvent(
            forgotten_start_seq=start_seq,
            forgotten_end_seq=end_seq,
            summary=summary,
            summary_role="user",
            reason="tokens",
        )
