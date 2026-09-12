"""View-rendering free functions for the agent loop.

Compatibility facade: the workspace snapshot implementation now lives in
:mod:`view_snapshot`, the single bounded owner. This module re-exports every
public/private name, signature, prompt byte, receipt, and omission rule so
consumers are unchanged. The remaining view-rendering projections (the plan
receipts, the F8 arg-shrink transform, the router overflow signal, and the
``ViewBuilder`` coordinator) stay here, split under the architecture limits.

Extracted from engine.py (the _LoopFacet god-class). These are render-time
projections — they carry no loop state: the snapshot takes the sandbox
explicitly, the others are pure over their inputs. Bodies are byte-identical to
the former _LoopFacet methods.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, cast

from ..context import (
    ArtifactMemoryStore,
    CompactionPolicy,
    ContextLedger,
    VerifierFailureRef,
    context_compact_if_needed,
)
from ..effects import ObservationReceipt
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    retarget_elided_arg_markers,
)
from ..inspect import inspect_enabled
from ..llm import Difficulty, OperatingMode, OverflowSignal
from ..obs import log_event, log_span
from ..view import View, effective_plan_progress, microcompact
from . import signals
from .context_budget import ContextCaps, derive_context_caps
from .context_builder import (
    build_context_pack,
    render_context_pack,
    render_plan_as_todo_markdown,
)
from .context_live import context_pack_enabled, protected_context_compaction_seqs
from .dedup import (
    collapse_superseded_reads,
)
from .file_state import (
    FileStateTracker,
    file_state_notice,
    reconcile_mutation_receipts,
)
from .messages import _workspace_paths_from_events
from .observe import _ground_read
from .view_f8 import f8_shrink_file_write_args  # noqa: F401 — re-exported for back-compat
from .view_snapshot import (  # noqa: F401 — re-exported for back-compat
    _BASELINE_CAPS,
    _WS_MAX_FILES,
    _WS_PER_FILE_CHARS,
    _WS_READ_TIMEOUT_S,
    _WS_TOTAL_CHARS,
    SnapshotCache,
    working_set_hashes,
    workspace_snapshot_message,
)
from .view_snapshot import (
    _SNAPSHOT_READ_HALT as _SNAPSHOT_READ_HALT,
)
from .view_snapshot import (
    _read_working_file as _read_working_file,
)
from .view_snapshot import (
    _SnapshotHalt as _SnapshotHalt,
)
from .view_snapshot import (
    _workspace_snapshot_preamble as _workspace_snapshot_preamble,
)

if TYPE_CHECKING:
    from .boundaries import Sandbox
    from .ports import (
        ContextGroundingPort,
        ConversationModePort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
    )

    class _LoopFacet(

        ContextGroundingPort,

        ConversationModePort,

        LoopEventPort,

        PlanLifecyclePort,

        ToolExecutionPort,

        Protocol,

    ):
        """The loop capability this module uses: context grounding, conversation mode, the event
        log, the plan lifecycle, tool execution.
        """

_LOG = logging.getLogger("disco.loop")

_PLAN_EXECUTION_RECEIPT_MARKER = "PLAN PHASE RECEIPT"
_PLAN_PLANNING_RECEIPT_MARKER = "PLANNING PHASE RECEIPT"
_INVALID_PLAN_FEEDBACK = "invalid_plan_done_conditions"
_PLAN_VERIFIER_FAILED = "plan_verifier_failed"
_PLAN_VERIFIER_REPLAN_REQUIRED = "plan_verifier_replan_required"
_RETIRED_PLAN_FEEDBACK_PREFIX = "[[DISCO-RETIRED-PLAN-FEEDBACK:"


def _plan_planning_receipt(events: list[Event], *, planning_active: bool) -> LLMMessage | None:
    """Project current revision-planning truth after stale historical context."""

    if not planning_active:
        return None
    repair = signals.verifier_repair_planning_active(events)
    approved = signals.latest_approved_plan(events)
    prior_revision = approved.revision if approved is not None else 0
    next_revision = signals.next_plan_revision(events)
    if repair:
        purpose = (
            "The current plan-owned verifier failed twice. The delivered workspace "
            "may already be correct; external acceptance is unchanged. Submit a "
            "corrected plan/verifier for approval and do not redo product edits while "
            "this repair is still in PLANNING."
        )
    elif approved is None:
        instruction = (signals.latest_user_text(events) or "the user's request").strip()
        if len(instruction) > 320:
            instruction = instruction[:317].rstrip() + "..."
        purpose = (
            f"No plan is approved yet. Revision {next_revision} is the initial plan for "
            f"the request {instruction!r}. Read only what you need, then call "
            "`submit_plan` for approval."
        )
    else:
        instruction = (
            signals.current_revision_instruction(events) or "the latest user change"
        ).strip()
        if len(instruction) > 320:
            instruction = instruction[:317].rstrip() + "..."
        purpose = (
            f"A new user instruction is awaiting revision {next_revision}: {instruction!r}. "
            "Read only what you need, then call `submit_plan` for that revision."
        )
    return LLMMessage(
        role="user",
        content=(
            "<system-reminder>\n"
            f"{_PLAN_PLANNING_RECEIPT_MARKER} (authoritative; derived from durable "
            "phase events):\n"
            f"CURRENT PHASE: PLANNING for revision {next_revision}. Revision "
            f"{prior_revision} is only the previously approved baseline; it is NOT "
            "approval for the pending revision. Workspace mutation and execution "
            f"tools remain unavailable until the new plan is approved. {purpose}\n"
            "</system-reminder>"
        ),
    )


def _latest_plan_approval(events: list[Event]) -> StatusEvent | None:
    """The most recent ``plan_approved`` StatusEvent, or None."""
    return next(
        (
            event
            for event in reversed(events)
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        None,
    )


def _has_post_approval_idempotent(events: list[Event], approval_seq: int) -> bool:
    """Whether a plan_revision_idempotent StatusEvent follows the approval.

    The paired idempotent-redirect MessageEvent is already the current
    execution handoff. Do not project a second copy from the older approval.
    """
    return any(
        isinstance(event, StatusEvent)
        and event.detail == "plan_revision_idempotent"
        and (event.seq or 0) > approval_seq
        for event in events
    )


def _has_post_approval_proposal(events: list[Event], approval_seq: int) -> bool:
    """Whether a newer PlanEvent proposal follows the approval.

    A newer proposal is awaiting its own decision; do not describe the older
    approval as the active handoff if a caller accidentally materializes a
    view while that gate is parked.
    """
    return any(
        isinstance(event, PlanEvent) and (event.seq or 0) > approval_seq for event in events
    )


def _has_post_approval_action(events: list[Event], approval_seq: int) -> bool:
    """Whether an execution ActionEvent follows the approval."""
    return any(
        isinstance(event, ActionEvent) and (event.seq or 0) > approval_seq for event in events
    )


def _execution_receipt_guidance(plan: PlanEvent, verifier_repair: bool) -> str:
    """The guidance text for the execution receipt, per repair mode."""
    next_step = plan.steps[0].title.strip() if plan.steps else "the approved work"
    if verifier_repair:
        return (
            "This approval is a VERIFIER-ONLY correction after successful workspace "
            "work. The prior delivered bytes remain valid; do not redo product edits "
            "unless fresh evidence shows they are wrong. Call `finish` now so Disco "
            "freshly re-runs external acceptance, the corrected plan predicates, and "
            "the normal host/browser gates. Read-only inspection is allowed if needed."
        )
    return (
        "Any earlier invalid-plan or correct-and-resubmit instruction for this "
        "revision is superseded. Do not call `submit_plan` or submit this approved "
        "plan again. Continue with execution tools now; the next step is: "
        f"{next_step}. Use `propose_plan_update` only if new user input or "
        "execution evidence shows that the approved plan itself must change."
    )


def _plan_execution_receipt(events: list[Event]) -> LLMMessage | None:
    """Project the durable approval edge into the first execution view.

    ``plan_approved`` is intentionally a non-LLM StatusEvent.  Without this
    projection, the model's last visible instruction can remain "your plan was
    NOT accepted" even after the user approved its corrected replacement.  The
    mode/tool router knows execution started, while the model still believes it
    is planning.  Deriving the receipt from the persisted approval transition
    keeps those two views of phase truth identical across restart and
    condensation without adding a second best-effort event.

    The receipt remains present until the first execution ActionEvent.  After
    that, the action/result pair itself proves that the phase handoff was
    consumed.  An out-of-phase ``submit_plan`` gets the same guidance from the
    dispatch guard, so that recovery does not depend on this projection
    lingering forever.
    """

    plan = signals.latest_approved_plan(events)
    if plan is None or signals.in_planning_for_revision(events):
        return None
    approval = _latest_plan_approval(events)
    if approval is None:
        return None
    approval_seq = approval.seq or 0
    if _has_post_approval_idempotent(events, approval_seq):
        return None
    if _has_post_approval_proposal(events, approval_seq):
        return None
    verifier_repair = signals.verifier_repair_execution_active(events)
    if not verifier_repair and _has_post_approval_action(events, approval_seq):
        return None
    guidance = _execution_receipt_guidance(plan, verifier_repair)
    return LLMMessage(
        role="user",
        content=(
            "<system-reminder>\n"
            f"{_PLAN_EXECUTION_RECEIPT_MARKER} (authoritative; derived from the "
            "durable approval record):\n"
            f"Plan revision {plan.revision} is APPROVED. You are now in EXECUTION "
            f"mode. {guidance}\n</system-reminder>"
        ),
    )


def _retire_superseded_invalid_plan_feedback(
    events: list[Event],
) -> tuple[list[Event], frozenset[str]]:
    """Retire the exact tagged rejection events a later approval resolved.

    This is render-only.  Audit history stays byte-for-byte intact, but obsolete
    blocking instructions cannot compete with the approved plan in a restarted
    or re-condensed model view.  Each exact tagged event is replaced by a unique
    render sentinel before ``View.of``; an ordinary user message with identical
    prose is therefore never removed by content coincidence.  The replacement
    retains the original id/seq/meta so condensation placement and view-authority
    projection remain unchanged.
    """

    if signals.latest_approved_plan(events) is None:
        return events, frozenset()
    superseded_failures = signals.superseded_plan_owned_failure(events)
    approval_seq = max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        default=0,
    )
    projected: list[Event] = []
    sentinels: set[str] = set()
    for event in events:
        blocking = event.meta.get("blocking") if isinstance(event, MessageEvent) else None
        retire_invalid = blocking == _INVALID_PLAN_FEEDBACK and (event.seq or 0) < approval_seq
        retire_plan_failure = (
            blocking in {_PLAN_VERIFIER_FAILED, _PLAN_VERIFIER_REPLAN_REQUIRED}
            and event.id in superseded_failures
        )
        if (
            isinstance(event, MessageEvent)
            and event.source is EventSource.ENVIRONMENT
            and (retire_invalid or retire_plan_failure)
        ):
            sentinel = f"{_RETIRED_PLAN_FEEDBACK_PREFIX}{event.id}]]"
            sentinels.add(sentinel)
            projected.append(
                event.model_copy(
                    update={"message": event.message.model_copy(update={"content": sentinel})}
                )
            )
            continue
        projected.append(event)
    return projected, frozenset(sentinels)


def overflow_signal(events: list[Event]) -> OverflowSignal:
    """Derive the router's overflow inputs from the log: trailing tool errors
    feed router rule 4 (stuck recovery) so a struggling step can auto-escalate
    to overflow *before* STUCK is declared (§6 note)."""
    consecutive = 0
    for e in reversed(events):
        if isinstance(e, AgentErrorEvent):
            consecutive += 1
        elif isinstance(e, ActionEvent | ObservationEvent | MessageEvent):
            break
    return OverflowSignal(difficulty=Difficulty.ROUTINE, consecutive_tool_errors=consecutive)


def _drop_context_pack_owned_history(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Drop raw PlanEvent renders when the ContextPack owns goal/version/todo."""
    return [
        msg
        for msg in messages
        if not (msg.role == "assistant" and msg.content.startswith("Plan (revision "))
    ]


def _worth_a_summarizer_call(condenser: object, events: list[Event]) -> bool:
    """Soft condensation batches: only when the condenser can say how much history has aged
    out AND that amount reaches its batch size. Condensers without the seam behave as before."""
    forgettable = getattr(condenser, "forgettable_tokens", None)
    batch = getattr(condenser, "min_batch_tokens", None)
    if not callable(forgettable) or not isinstance(batch, int):
        return True
    return int(cast(Callable[[list[Event]], int], forgettable)(events)) >= batch


class ViewBuilder:
    """Materialize the model-facing View each turn: microcompact, condense if
    triggered (§8), gate the C6 tail-recap, apply the F8 shrink (assist), and
    append the always-fresh workspace snapshot. Back-ref collaborator: body is
    byte-identical to the former _LoopFacet._materialize_view with self. →
    self._loop..

    W2 addition: holds a ``FileStateTracker`` instance across turns to power
    the stale-aware snapshot (full body only for stale/never-shown files,
    one-line pointer for unchanged known files) and the stale-change notice."""

    def __init__(self, loop: _LoopFacet) -> None:
        self._loop = loop
        # W2 — per-ViewBuilder file-state tracker (mutable, survives across
        # turns). Starts empty; populated by workspace_snapshot_message as
        # files are shown in full. No lock needed: build() runs under the
        # loop's conversation lock.
        self._file_tracker: FileStateTracker = FileStateTracker()
        # Bytes of the working set keyed by hash — one hash exec per turn decides
        # which files are read again (see SnapshotCache).
        self._snapshot_cache: SnapshotCache = SnapshotCache()
        # The current turn's working-set hash map (set by _compute_stale_paths, read by
        # _build_snapshot); None when the hash exec was unavailable this turn.
        self._turn_hashes: dict[str, str] | None = None

    def _resolve_caps(self) -> ContextCaps:
        """CW-2/CW-6 — resolve this turn's context caps from the capability gate
        (`assist`) and the LIVE driver context window. Reuses the SAME cached
        `_driver_context_window()` the condenser budgets against (runtime); does not
        re-probe. Best-effort: a loop without the runtime method (legacy/mock) →
        unknown window → the assist baseline (byte-identical to today)."""
        window: int | None = None
        probe = getattr(self._loop, "_driver_context_window", None)
        if callable(probe):
            try:
                result = probe()
                window = int(result) if isinstance(result, int) else None
            except Exception:  # noqa: BLE001 — never block view build on a probe miss
                window = None
        return derive_context_caps(assist=bool(self._loop._assist), context_window=window)

    def _context_compaction_policy(self) -> CompactionPolicy:
        policy = getattr(self._loop, "_context_compaction_policy", None)
        if isinstance(policy, CompactionPolicy):
            return policy
        return CompactionPolicy.default()

    async def _context_pack_message(self, events: list[Event]) -> LLMMessage:
        """Build the live ContextPack message, with durable store inputs best-effort."""
        base_ledger: ContextLedger | None = None
        todo_text: str | None = None
        design_direction: str | None = None
        failures: tuple[VerifierFailureRef, ...] | None = None
        sbx = getattr(getattr(self._loop, "executor", None), "sandbox", None)
        if sbx is not None:
            store = ArtifactMemoryStore(sbx)
            try:
                workspace_root = getattr(sbx, "workspace_root", None)
                reconstructed = await store.reconstruct(
                    str(getattr(self._loop, "conversation_id", "") or ""),
                    str(workspace_root) if workspace_root is not None else None,
                )
                base_ledger = reconstructed.ledger
                failures = reconstructed.ledger.latest_verifier_failures
            except Exception:  # noqa: BLE001 - context files are best-effort
                _LOG.warning(
                    "CXT live context-pack ledger read failed for %s",
                    getattr(self._loop, "conversation_id", ""),
                    exc_info=True,
                )
            try:
                todo_text = await store.read_todo()
            except Exception:  # noqa: BLE001 - absent/corrupt todo omits the section
                _LOG.warning(
                    "CXT live context-pack todo read failed for %s",
                    getattr(self._loop, "conversation_id", ""),
                    exc_info=True,
                )
            try:
                design_direction = await store.read_design_direction()
            except Exception:  # noqa: BLE001 - absent/corrupt design direction omits it
                _LOG.warning(
                    "CXT live context-pack design direction read failed for %s",
                    getattr(self._loop, "conversation_id", ""),
                    exc_info=True,
                )

        # todo.md remains a durable seed/human artifact.  Live execution
        # progress comes from the event log's single progress reducer, so a
        # stale unchecked seed can never contradict completed plan steps.
        plan, states = effective_plan_progress(events)
        if plan is not None:
            todo_text = render_plan_as_todo_markdown(
                plan,
                states,
                include_heading=False,
            )

        pack = build_context_pack(
            events,
            base_ledger=base_ledger,
            policy=self._context_compaction_policy(),
            todo_text=todo_text,
            design_direction=design_direction,
            failures=failures,
            allowed_next_actions=self._context_allowed_next_actions(events),
        )
        return LLMMessage(role="user", content=render_context_pack(pack))

    def _context_allowed_next_actions(self, events: list[Event]) -> tuple[str, ...]:
        """Expose the phase router's real planning scope in the durable pack."""

        if not self._planning_phase_active(events):
            return ()
        try:
            names = self._loop._driver.planning_allowed_tool_names()
        except Exception:  # noqa: BLE001 - view projection must remain available
            names = getattr(self._loop, "_planning_tools", frozenset())
        return tuple(sorted(str(name) for name in names if str(name)))

    def _planning_phase_active(self, events: list[Event]) -> bool:
        """Whether this plan-first loop is currently accepting planning actions."""

        if not getattr(self._loop, "_planning_tools", frozenset()):
            return False
        if self._loop._effective_mode(events) is not OperatingMode.PLANNING:
            return False
        return not signals.plan_submitted_since_current_planning(events)

    async def _project_view(self, events: list[Event], *, include_context_pack: bool) -> View:
        projected_events, retired = _retire_superseded_invalid_plan_feedback(events)
        view = View.of(projected_events)
        messages = [message for message in view.messages if message.content not in retired]
        if include_context_pack:
            msg = await self._context_pack_message(events)
            messages = _drop_context_pack_owned_history(messages)
            insert_at = 1 if messages else 0
            if inspect_enabled():
                try:
                    log_event(
                        "context_pack",
                        cid=str(getattr(self._loop, "conversation_id", "") or ""),
                        included=True,
                        message_index=insert_at,
                        chars=len(msg.content),
                    )
                except Exception:  # noqa: BLE001 - inspect must never affect prompts
                    pass
            messages = [*messages[:insert_at], msg, *messages[insert_at:]]
        planning_receipt = _plan_planning_receipt(
            events, planning_active=self._planning_phase_active(events)
        )
        execution_receipt = _plan_execution_receipt(events)
        if planning_receipt is not None:
            messages.append(planning_receipt)
        if execution_receipt is not None:
            messages.append(execution_receipt)
        return view.model_copy(update={"messages": messages})

    async def build(self, events: list[Event]) -> View:
        """The View alone, for callers that own their history and do not re-read it.

        Prefer :meth:`build_with_horizon` on any path that will go on to use "the
        events" afterwards -- see that method for why.
        """
        view, _horizon = await self.build_with_horizon(events)
        return view

    async def build_with_horizon(self, events: list[Event]) -> tuple[View, list[Event]]:
        """The View **and the exact event list it was built from**.

        ``_build`` can append durable events mid-build -- microcompact tombstones,
        context-compaction snips, and a condensation tombstone -- re-reading the log
        after each. The View therefore describes a LATER horizon than the list the
        caller passed in. A caller that renders this View but keeps its own
        pre-build list is holding two different horizons at once, and the span the
        condenser just replaced still looks live to it, so the SAME span can be
        condensed a second time.

        Returning both together makes the horizon impossible to lose: one response,
        one horizon.
        """
        # CW-2/CW-6 — derive the per-turn caps once and thread them into the
        # snapshot (pin breadth/size) + the observation snip. assist-ON → the
        # baseline caps + a None snip override → byte-identical to today.
        caps = self._resolve_caps()
        # Pass the derived snip cap unconditionally: events.py only HONORS it when
        # it strictly exceeds the 8k baseline, so the assist-ON / small-window caps
        # (obs_snip_chars == 8k) are a no-op there → byte-identical to today.
        from ..events import obs_snip_override

        snip_token = obs_snip_override.set(caps.obs_snip_chars)
        try:
            return await self._build(events, caps)
        finally:
            obs_snip_override.reset(snip_token)

    async def _build(self, events: list[Event], caps: ContextCaps) -> tuple[View, list[Event]]:
        # S3 Microcompact (GAP A): a cheap, no-model pass FIRST — tombstone no-op
        # turns (a failed call an identical later call superseded) so the lossy
        # model-summarization condenser fires on a smaller, denser residue (or not
        # at all). Idempotent: re-running won't re-tombstone an already-dropped span.
        micro = microcompact(events)
        if micro:
            for tomb in micro:
                await self._loop._emit(tomb)
            events = await self._loop._events()
        context_pack_active = context_pack_enabled()
        view = await self._project_view(events, include_context_pack=context_pack_active)
        sbx, stale = await self._compute_stale_paths(events)
        with log_span("loop.view.snapshot", cached=len(self._snapshot_cache)):
            snapshot, pinned_full, snapshot_receipts = await self._build_snapshot(
                sbx, events, stale, caps, hashes=self._turn_hashes
            )
        for _p in pinned_full:
            _ground_read(self._loop, _p)
        snap_tokens = len(snapshot.content) // 4 if snapshot is not None else 0
        est = signals.estimate_tokens(view) + snap_tokens
        # H3: log the prompt size per step so cost regressions are visible (the 60k
        # bloat was invisible because nothing measured it). DEBUG-level; cheap.
        _LOG.debug("driver view: ~%d input tokens, %d messages", est, len(view.messages))
        with log_span("loop.view.condense"):
            view, events = await self._maybe_condense(
                events, view, context_pack_active, snap_tokens, est
            )
        view = self._apply_history_transforms(view, events, pinned_full, snapshot_receipts, stale)
        view = self._position_snapshot(view, snapshot, stale)
        # `events` has been re-read after every durable append above, so it is the
        # exact horizon this View describes. Returning it with the View is what
        # keeps a caller from carrying a stale list forward.
        return view, events

    async def _compute_stale_paths(
        self, events: list[Event]
    ) -> tuple[Sandbox | None, list[str]]:
        """W2 — identify files whose disk SHA diverged from the snapshot's last view.

        One hash exec over the working set serves both this scan and the snapshot's
        read decisions (left in ``self._turn_hashes``); when it is unavailable both
        fall back to per-file reads.
        """
        sbx = getattr(getattr(self._loop, "executor", None), "sandbox", None)
        reconcile_mutation_receipts(self._file_tracker, events)
        mutated, read_only = _workspace_paths_from_events(events)
        working_set = mutated + read_only
        hashes: dict[str, str] | None = None
        stale: list[str] = []
        if sbx is not None and working_set:
            with log_span("loop.view.hashes", files=len(working_set)) as span:
                hashes = await working_set_hashes(sbx, working_set)
                span["hashed"] = len(hashes) if hashes is not None else -1
            stale = await self._file_tracker.stale_paths(sbx, working_set, hashes=hashes)
        self._turn_hashes = hashes
        return sbx, stale

    async def _build_snapshot(
        self,
        sbx: Sandbox | None,
        events: list[Event],
        stale: list[str],
        caps: ContextCaps,
        *,
        hashes: dict[str, str] | None = None,
    ) -> tuple[LLMMessage | None, set[str], list[ObservationReceipt]]:
        """A8 — build the live workspace snapshot up-front so its size is counted.

        The snapshot content is disk-derived and independent of condensation, so
        building it before the condense check and attaching it after is sound.
        Collect exact full-body paths for write grounding and typed revision/range
        receipts for safe historical-read compaction.
        """
        pinned_full: set[str] = set()
        snapshot_receipts: list[ObservationReceipt] = []
        snapshot = await workspace_snapshot_message(
            sbx,
            events,
            tracker=self._file_tracker,
            stale=frozenset(stale),
            caps=caps,
            pin_full=not self._loop._assist,
            out_pinned_full=pinned_full,
            out_prompt_receipts=snapshot_receipts,
            hashes=hashes,
            cache=self._snapshot_cache,
        )
        return snapshot, pinned_full, snapshot_receipts

    async def _maybe_condense(
        self,
        events: list[Event],
        view: View,
        context_pack_active: bool,
        snap_tokens: int,
        est: int,
    ) -> tuple[View, list[Event]]:
        """Run context-compaction then model condensation if triggered (§8).

        Returns the possibly-rebuilt view and the re-read event horizon.
        """
        req = self._loop.condenser.should_condense(view, token_count=est)
        if req is not None and context_pack_active:
            snips = context_compact_if_needed(
                events,
                self._context_compaction_policy(),
                protected_seqs=protected_context_compaction_seqs(events),
                pressure_chars=est * 4,
            )
            if snips:
                for snip in snips:
                    await self._loop._emit(snip)
                events = await self._loop._events()
                view = await self._project_view(events, include_context_pack=context_pack_active)
                est = signals.estimate_tokens(view) + snap_tokens
                req = self._loop.condenser.should_condense(view, token_count=est)
        if req is not None and req.soft and not _worth_a_summarizer_call(self._loop.condenser, events):
            req = None  # aged history too small to summarize yet; the hard trigger never waits
        if req is not None:
            tombstone = await self._loop.condenser.condense(
                events, view, summarizer=self._loop.summarizer
            )
            if tombstone is not None:
                await self._loop._emit(tombstone)
                events = await self._loop._events()
                view = await self._project_view(events, include_context_pack=context_pack_active)
            # Soft trigger with no tombstone this step: proceed uncondensed and
            # retry next iteration (§8). Non-fatal.
        return view, events

    def _apply_history_transforms(
        self,
        view: View,
        events: list[Event],
        pinned_full: set[str],
        snapshot_receipts: list[ObservationReceipt],
        stale: list[str],
    ) -> View:
        """Apply the C6 tail-recap gate, read-collapse, elided-arg retarget, and F8.

        Append the snapshot AFTER any condensation (so it is never rebuilt away by
        a re-projection) and outside View.of (so the render-time snip/mask never
        touch it). C6 gates the tail-recap BEFORE the snapshot append so the gate
        inspects a message list whose LAST element is the recap, not the snapshot.
        """
        view = self._loop._gate_recitation(view, events, context_pack_active=context_pack_enabled())
        # Pure render-time compaction: only exact same-revision/range receipts may
        # replace a historical read. Bare paths and prior-turn delivery confer no
        # authority; a later partial read cannot evict an earlier full read.
        _pinned_arg = None if self._loop._assist else frozenset(pinned_full)
        view = view.model_copy(
            update={
                "messages": collapse_superseded_reads(
                    view.messages,
                    events,
                    pinned_full_paths=_pinned_arg,
                    prompt_receipts=tuple(snapshot_receipts),
                )
            }
        )
        # CW P1-a (round-2) — assist-OFF: retarget every elided tool-call ARGUMENT marker
        # to the neutral metadata-only marker. An elided arg is a write/edit body — NOT
        # the file's current content — so it is not in the CURRENT WORKSPACE block even
        # when the path is pinned; the old per-pin "it's in the block" pointer was
        # therefore a dangling claim. `_snip_args` rendered the directional pre-CW-3
        # marker inside View.of (no tier context there); this pass neutralizes it for
        # the prefix-placed block. assist-ON keeps the directional marker (correct for
        # its tail-placed block + byte-identical to pre-CW-3).
        if not self._loop._assist:
            view = view.model_copy(update={"messages": retarget_elided_arg_markers(view.messages)})
        # F8 — GATED mid-turn arg truncation (assist-tier context-window reclaim).
        # Render-time only: the persisted event log is unchanged. Assist OFF → no-op;
        # the rendered messages are byte-identical to today. Runs AFTER the collapse so
        # the F8 marker is applied to history and after the snapshot append so the
        # snapshot (authoritative current state) is never shrunk.
        if self._loop._assist:
            view = view.model_copy(
                update={
                    "messages": self._loop._f8_shrink_file_write_args(view.messages, events),
                }
            )
        return view

    def _position_snapshot(
        self,
        view: View,
        snapshot: LLMMessage | None,
        stale: list[str],
    ) -> View:
        """CW-3 — position the pinned snapshot block and the volatile stale notice.

        assist OFF (capable): the block is byte-stable (pin_full) → move it to the
        PREFIX (front of view.messages, which lands immediately after the system
        prompt once routing prepends it, BEFORE the event history). The
        system+tools+workspace prefix then caches as a unit; an unchanged turn
        rebills it at ~10% instead of re-billing the whole ~60k block. The stale
        notice stays in the tail (volatile).

        assist ON (small): UNCHANGED — stale notice then snapshot in the TAIL,
        byte-identical to before this change (the W2 pointer block is volatile,
        so it belongs in the recent, high-attention window, not a cache prefix).
        """
        stale_msg = file_state_notice(stale)
        if snapshot is not None:
            _LOG.info(
                "A8 workspace snapshot injected: %d chars across the working set",
                len(snapshot.content),
            )
        if self._loop._assist:
            tail: list[LLMMessage] = []
            if stale_msg is not None:
                tail.append(stale_msg)
            if snapshot is not None:
                tail.append(snapshot)
            if tail:
                return view.model_copy(update={"messages": [*view.messages, *tail]})
        else:
            prefix = [snapshot] if snapshot is not None else []
            tail = [stale_msg] if stale_msg is not None else []
            if prefix or tail:
                return view.model_copy(update={"messages": [*prefix, *view.messages, *tail]})
        return view
