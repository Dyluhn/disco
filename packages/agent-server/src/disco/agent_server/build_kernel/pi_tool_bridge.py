"""`PiToolBridge` — route a Pi-kernel tool call through Disco's FULL safety wrapper.

Disco Pi Build Kernel Campaign, EPIC D (the Pi tool bridge).

THE PROBLEM this module exists to solve. The naive campaign sketch (PR D2) routes
a Pi custom tool call straight to ``DefaultToolExecutor.execute()``::

    Pi custom tool → POST … → DefaultToolExecutor.execute() → ToolResult

But ``DefaultToolExecutor.execute()`` is only the INNERMOST step of what Disco's
own agent loop does around every tool call. The loop (``AgentLoop._run_drive``)
applies a stack of pre- and post-execution SAFETY gates *around* the executor —
gates that block writes during PLANNING, hard-deny catastrophic commands, raise a
risk-confirmation gate, defend against copied-back elision markers (K1), enforce
read-before-write grounding, and pair every action with exactly one observation.
A Pi bridge that called only ``execute()`` would silently drop ALL of those. This
module REPLICATES (composes) that wrapper so a Pi-originated tool call is gated
EXACTLY as a Disco-originated one.

WHAT IS REPLICATED HERE (file:line of the Disco source each gate mirrors):

  * Planning execute-time gate — block writes/shell/finish/… during PLANNING.
      mirror: engine.py:945-969 (the allowlist-rejection branch of
      ``_gate_planning_mode``). Allowlist source: ``Driver.planning_allowed_tool_
      names`` (driver.py:172).  → ``PiToolBridge._gate_planning``
  * Hard-deny — catastrophic-command refusal (mkfs / rm -rf / fork bomb / raw
      disk write), refused before any approval can run it.
      mirror: engine.py:980-1001 (``_gate_hard_deny``) over the FREE function
      ``signals.hard_deny_reason`` (signals.py:99 → analyzers.py:104).
      → ``PiToolBridge._gate_hard_deny``
  * Risk assessment + confirmation gate — assess risk, stamp the audit into the
      action meta, then gate (HALT pending) per the confirmation policy.
      mirror: engine.py:1003-1057 (``_gate_risk_confirm``); policy decision via
      ``policy.should_confirm_action`` (policies.py:98).
      → ``PiToolBridge._gate_risk_confirm``
  * K1 elision guard + recovery — reject a copied-back ``_snip_args`` placeholder
      before execution (data-loss + read-loop defense); auto-recover a pure
      ``file_write.content`` marker from the log instead of dead-ending.
      mirror: observe.py:334-413 over FREE helpers ``find_elided_arg_markers`` /
      ``value_is_only_elision_marker`` (events.py:441/460) and
      ``_recover_elided_file_write_content`` (observe.py:60). The executor repeats
      a generic guard at executor.py:372.  → ``PiToolBridge._execute_and_observe``
  * F9 read-dedup (ASSIST-only, default OFF) — short-circuit an identical recent
      read-only call to a pointer + ground the read so a following write is not
      falsely blocked. mirror: observe.py:253-311 over ``_f9_dedupable_read``
      (dedup.py:452). Gated on ``assist`` exactly like Disco; capable models
      (assist OFF) bypass it byte-identically.  → ``PiToolBridge._maybe_f9_dedup``
  * Read-before-write / file-state grounding — comes for FREE by routing through
      the executor (the ``file_write`` tool's own gate, files.py:411), PLUS the
      explicit ``note_grounding_read`` calls the loop makes on K1-recovery and F9
      dedup paths (replicated here, mirror of observe.py:299/373's ``_ground_read``,
      observe.py:45).  → ``PiToolBridge._ground_read``
  * Executor validation / invalid-tool — unknown/out-of-scope tool and pydantic
      arg failures become structured ``ToolResult`` failures (executor.py:363-418),
      surfaced as a recoverable observation. Comes for FREE via the executor.
  * ActionEvent↔observation pairing — every executed action yields EXACTLY one
      observation (``ObservationEvent`` on success / ``AgentErrorEvent`` on
      failure), correlated by ``action.id`` + the tool ``call_id``.
      mirror: observe.py:226 (``Observer.execute_and_observe`` [CONTRACT]),
      emission at observe.py:431 (success) / 452-457 (failure) / 420-427 (raised).
      → ``PiToolBridge._execute_and_observe``

WHAT IS NOT REPLICATED HERE — and WHY (these are loop-intelligence / product, or
they require a forbidden shared seam; see the campaign EPIC-D constraints):

  * The confirmation CONTINUATION (confirm → execute / reject → deny). When the
      risk gate gates a call, this bridge HALTs it pending and returns
      ``CONFIRM_REQUIRED``; it does NOT execute it. Resuming a pending action is
      owned by the existing control surface (``AgentLoop.confirm`` engine.py:1499 /
      ``AgentLoop.reject`` engine.py:1515, driven by ``ControlOps``), which a
      PARALLEL campaign task owns and which already re-runs the pending action
      through the SAFE path (``_execute_and_observe``). The bridge therefore
      produces the pending state faithfully and stops — no duplication, no edit to
      ``control_ops.py``.  INTEGRATION DEPENDENCY: the Pi runtime must route a
      CONFIRM_REQUIRED outcome to that existing confirm/reject surface.
  * Virtual / meta tools (``finish`` / ``submit_plan`` / ``ask_user`` / ``clarify``
      / ``notify_user`` / ``remember`` / ``serve`` / ``delegate_explore`` /
      ``propose_plan_update``) are loop-handled, NOT executor tools (engine.py:1315,
      turn_control.py:909). The bridge does not execute them through the executor;
      a configured ``virtual_tool_names`` set short-circuits them to
      ``BridgeDecision.VIRTUAL`` so the caller dispatches them to the loop's virtual
      handlers.  INTEGRATION DEPENDENCY: wire the real virtual set + handlers from
      the runtime (parallel task).
  * Loop breakers, recitation/re-ground, execution-nudge, fan-out caps, F8 arg
      shrink, superseded-read collapse — pure LOOP-INTELLIGENCE the Pi kernel
      supplies itself; double-handling them here would CONFLICT. Left to Pi.

THE DANGLING / MISSING-OBSERVATION DECISION (codex flagged: Disco DETECTS a
dangling action after a crash but has no core auto-replay). See
``reconcile_dangling_action`` below for the explicit Pi decision.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationState,
    ConversationStatus,
    Event,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.events import (
    SecurityRisk,
    find_elided_arg_markers,
    value_is_only_elision_marker,
)
from disco.core.llm.types import OperatingMode
from disco.core.loop import signals
from disco.core.loop.dedup import _f9_dedupable_read
from disco.core.loop.observe import _recover_elided_file_write_content

if TYPE_CHECKING:
    from disco.core.security.assessment import RiskAssessment

_LOG = logging.getLogger(__name__)

__all__ = [
    "BridgeDecision",
    "BridgeHost",
    "BridgeOutcome",
    "PiToolBridge",
]


class BridgeDecision(str, Enum):
    """The outcome class of routing one Pi tool call through the wrapper.

    The caller (the Pi runtime) interprets this to decide what to hand back to
    the Pi inner loop:

      * ``EXECUTED`` — the tool ran and SUCCEEDED; a paired ``ObservationEvent``
        is in the log. Return its result to Pi.
      * ``FAILED`` — the tool ran but FAILED (or raised, or was invalid/unknown);
        a paired ``AgentErrorEvent`` is in the log. Return the error to Pi — it is
        RECOVERABLE (Pi reasons about it and adapts), exactly like Disco surfaces
        a tool failure to its model.
      * ``REFUSED`` — a PRE-execution gate refused the call (planning gate /
        hard-deny / unrecoverable K1 elision marker); the tool was NEVER executed.
        A paired ``AgentErrorEvent`` is in the log. Return the refusal to Pi.
      * ``CONFIRM_REQUIRED`` — the risk gate HALTed the call pending user
        confirmation. The proposed ``ActionEvent`` + a
        ``WAITING_FOR_CONFIRMATION`` status are in the log; ``pending_action_id``
        identifies it. The caller pauses Pi and routes to the existing
        confirm/reject control surface (NOT this bridge).
      * ``VIRTUAL`` — the tool is a loop-handled virtual/meta tool (finish,
        submit_plan, ask_user, …); it was NOT executed. The caller dispatches it
        to the loop's virtual handler.
      * ``HALTED`` — the conversation is NOT in a drivable state (it is awaiting
        confirmation / plan approval / a user answer, is paused, or is terminal —
        finished/stuck/error), OR its state was indeterminate. The call was NOT
        executed and NOTHING was emitted (there is no action to pair). The caller
        must route to the appropriate control surface (e.g. confirm/reject) or wait
        for the user — it must NOT retry the tool. ``pending_action_id`` carries the
        action awaiting confirmation when the halt is WAITING_FOR_CONFIRMATION. This
        is the FAIL-CLOSED state gate (mirror the _run_drive checkpoint
        engine.py:1216 + run() entry engine.py:1107).
    """

    EXECUTED = "executed"
    FAILED = "failed"
    REFUSED = "refused"
    CONFIRM_REQUIRED = "confirm_required"
    VIRTUAL = "virtual"
    HALTED = "halted"


@dataclass(frozen=True)
class BridgeOutcome:
    """The structured result of one ``PiToolBridge.route`` call.

    Exactly one of ``observation`` / ``error`` is set for the terminal decisions
    (``EXECUTED`` → ``observation``; ``FAILED`` / ``REFUSED`` → ``error``).
    ``CONFIRM_REQUIRED`` sets ``pending_action_id`` and leaves both ``None``.
    ``action`` is the proposed/executed ``ActionEvent`` for every decision except
    ``VIRTUAL`` (a virtual tool never becomes an executor action).
    """

    decision: BridgeDecision
    action: ActionEvent | None = None
    observation: ObservationEvent | None = None
    error: AgentErrorEvent | None = None
    pending_action_id: str | None = None


@runtime_checkable
class BridgeHost(Protocol):
    """The minimal seam the bridge composes — every dependency a Disco loop
    already exposes, injected explicitly so the bridge is testable with fakes and
    so it never reaches into ``runtime.py`` / ``engine.py`` directly.

    A live ``AgentLoop`` satisfies this shape via :meth:`PiToolBridge.from_loop`.
    """

    # Declared as read-only properties so an implementer may satisfy them with
    # either a plain attribute (the test ``FakeHost``) OR a live property (the
    # ``_LoopBridgeHost`` adapter, which reads through to the loop per access).

    @property
    def executor(self) -> Any:
        """WHERE tools execute. Duck-typed against ``DefaultToolExecutor``: must
        provide ``async execute(call) -> ToolResult`` and SHOULD provide
        ``tool_scope(name) -> str``, ``note_grounding_read(path)``,
        ``readonly_tool_names() -> frozenset[str]`` and a ``sandbox`` attribute."""
        ...

    @property
    def analyzer(self) -> Any:
        """Risk analyzer — ``assess(action) -> SecurityRisk`` and optionally the
        richer ``assess_detailed(action) -> RiskAssessment``."""
        ...

    @property
    def policy(self) -> Any:
        """Confirmation policy — ``should_confirm(risk)`` and optionally
        ``should_confirm_action(risk, *, scope, tool_name)``."""
        ...

    @property
    def mode(self) -> OperatingMode:
        """The LIVE operating mode (PLANNING vs an execution mode). Read per call."""
        ...

    @property
    def plan_tool(self) -> str:
        """The structured-plan tool name (intercepted by the loop, never executed)."""
        ...

    def planning_allowed_tool_names(self) -> frozenset[str]:
        """The allowlist the PLANNING gate enforces (read-only-capability ∩ name
        allowlist + the plan tool + ask/clarify), kept in lockstep with tool
        visibility (mirror ``Driver.planning_allowed_tool_names`` driver.py:172)."""
        ...

    async def emit(self, event: Event) -> None:
        """Append an event to the conversation's append-only store (mirror
        ``AgentLoop._emit``)."""
        ...

    async def events(self) -> list[Event]:
        """The current event log (mirror ``AgentLoop._events``)."""
        ...

    async def state(self) -> ConversationState:
        """The reconstructed conversation state (mirror ``AgentLoop.get_state``).
        REQUIRED so the bridge's terminal/halted-state gate can never silently fall
        OPEN: a host MUST be able to report whether the conversation is drivable. If
        this raises, the gate fails CLOSED (REFUSE)."""
        ...

    # OPTIONAL, duck-typed seams (discovered via ``getattr`` so a fake/legacy host
    # need not implement them — consistent with the executor's optional methods):
    #   * ``async reenter_planning_for_followup(events) -> bool`` — the loop's REAL
    #     stale-plan / change-steer replan gate (mutates mode→PLANNING + emits the
    #     planning marker when a CHANGE follow-up is pending). The live
    #     ``_LoopBridgeHost`` wires it to ``AgentLoop._maybe_reenter_planning_for_
    #     followup`` so the bridge INHERITS the real gate (no drift from a
    #     re-implemented ``signals.is_revision_intent``). Absent → no replan.
    #   * ``lock`` — the loop's ``asyncio.Lock``; the bridge holds it across the
    #     pre-execution critical section (state→replan→planning→risk→action append)
    #     exactly as ``_run_drive`` does, releasing it before the long-running
    #     execute. Absent → an uncontended ``nullcontext`` (single-threaded fakes).


@dataclass
class _LoopBridgeHost:
    """Adapter that presents a live ``AgentLoop`` as a :class:`BridgeHost`.

    Reaches the loop's PUBLIC executor/analyzer/policy/mode and its ``_emit`` /
    ``_events`` / ``get_state`` coroutines, plus the driver's
    ``planning_allowed_tool_names`` and the loop's stale-plan replan gate and lock
    (the accessors not re-exposed on the loop today — see the integration note in
    :meth:`PiToolBridge.from_loop`).

    It READS the loop's state/events/lock and DELEGATES the replan decision to the
    loop's own gate (``_maybe_reenter_planning_for_followup``) — the ONE place it
    drives a mutation, and only the SAME mutation ``_run_drive`` performs (re-enter
    PLANNING on a CHANGE follow-up), under the SAME lock the bridge holds. So it
    cannot collide with the runtime-lifecycle task: that task serializes on the
    loop lock too, and the bridge's pre-execution critical section is held under it.
    """

    _loop: Any

    @property
    def executor(self) -> Any:
        return self._loop.executor

    @property
    def analyzer(self) -> Any:
        return self._loop.analyzer

    @property
    def policy(self) -> Any:
        return self._loop.policy

    @property
    def mode(self) -> OperatingMode:
        # LIVE: the loop flips mode on plan approval; read it per call.
        return self._loop.mode

    @property
    def plan_tool(self) -> str:
        return self._loop._plan_tool

    def planning_allowed_tool_names(self) -> frozenset[str]:
        return self._loop._driver.planning_allowed_tool_names()

    async def emit(self, event: Event) -> None:
        await self._loop._emit(event)

    async def events(self) -> list[Event]:
        return await self._loop._events()

    async def state(self) -> ConversationState:
        # Pure store read (AgentLoop.get_state delegates to the store; it does NOT
        # take self._lock), so it is safe to call while the bridge holds the lock.
        return await self._loop.get_state()

    async def reenter_planning_for_followup(self, events: list[Event]) -> bool:
        """Inherit the loop's REAL stale-plan replan gate (mirror the apply-time
        ``_gate_midstep_steer_replan`` → ``_maybe_reenter_planning_for_followup``,
        engine.py:1818/1775). Re-enters PLANNING (mutating ``loop.mode`` + emitting
        the ``planning`` marker) iff a CHANGE/REVISION follow-up is pending on an
        approved build; the bridge's planning gate then REJECTS the write. Lock-free
        — the caller (the bridge) holds the loop lock, exactly as ``_run_drive``
        does when it calls this gate. Returns True iff it re-entered planning."""
        return await self._loop._maybe_reenter_planning_for_followup(events)

    @property
    def lock(self) -> Any:
        """The loop's ``asyncio.Lock`` — the bridge holds it across the
        pre-execution critical section so the state→replan→planning→risk→action-
        append sequence is atomic w.r.t. the runtime-lifecycle task, mirroring
        ``_run_drive``'s per-step critical section."""
        return self._loop._lock


# The statuses at which a Pi tool call must be REFUSED without executing — the
# conversation is parked at a gate or terminal and is NOT being driven, so a new
# tool call cannot land. Mirror of ``engine._TERMINAL_FOR_NOW`` MINUS ``IDLE``:
# IDLE means "ready to run" (run() entry engine.py:1107 treats IDLE as drivable,
# every OTHER terminal-for-now status as a refuse). Kept as an explicit literal
# (not imported) to avoid a heavy import of the engine module from agent_server;
# the set is small + stable and is asserted parity-equivalent in the tests.
_HALTED_STATUSES: frozenset[ConversationStatus] = frozenset(
    {
        ConversationStatus.PAUSED,
        ConversationStatus.FINISHED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.AWAITING_USER_QUESTION,
    }
)

# Mirror of the planning-gate refusal text (engine.py:957-963), kept verbatim so
# a Pi-originated planning violation reads identically to a Disco one.
_PLANNING_REFUSAL = (
    "<system-reminder>\n"
    "REFUSED: `{tool}` is not available in PLANNING mode. No workspace mutation "
    "or execution is allowed before plan approval. Call `submit_plan`, use a safe "
    "read tool (file_read/file_list/search/extract), or ask/clarify if details "
    "are missing.\n"
    "</system-reminder>"
)

# Mirror of the hard-deny refusal text (engine.py:990-994).
_HARD_DENY_REFUSAL = (
    "<system-reminder>\n"
    "REFUSED: that command is hard-denied ({reason}). It will never be executed "
    "regardless of approval. Choose a different, safe approach.\n"
    "</system-reminder>"
)

# Mirror of the K1 elision-guard refusal text (observe.py:399-408). The
# assist-OFF recovery wording (file_read is authoritative) is used because the Pi
# bridge does not render the assist-tier CURRENT WORKSPACE block.
_K1_REFUSAL = (
    "Argument(s) {bad} contain an internal elision placeholder (e.g. text inside "
    "angle brackets saying the content was elided / to re-issue the call or "
    "file_read the path for the full content), not real content. That marker is a "
    "context-saving stand-in for content you ALREADY wrote — it is NOT the content "
    "itself, and it was NOT executed. Do not copy the placeholder into a tool "
    "call. Read the actual current content — call file_read on the path for the "
    "authoritative content — and resend the FULL argument."
)


class PiToolBridge:
    """Compose Disco's tool-execution safety wrapper for a Pi-originated call.

    One public entry point — :meth:`route` — takes a single Pi tool call and
    drives it through the SAME ordered gates ``AgentLoop._run_drive`` applies,
    appending the SAME events (ActionEvent / ObservationEvent / AgentErrorEvent /
    StatusEvent) so the conversation log is indistinguishable from a Disco-driven
    one. It NEVER calls ``executor.execute`` without the wrapper.
    """

    def __init__(
        self,
        host: BridgeHost,
        *,
        assist: bool = False,
        virtual_tool_names: frozenset[str] = frozenset(),
    ) -> None:
        """``assist`` gates the F9 read-dedup exactly like Disco (default OFF:
        capable Pi models bypass it byte-identically). ``virtual_tool_names`` are
        loop-handled tools the bridge must NOT route to the executor."""
        self._host = host
        self._assist = assist
        self._virtual = virtual_tool_names

    @classmethod
    def from_loop(
        cls,
        loop: Any,
        *,
        assist: bool = False,
        virtual_tool_names: frozenset[str] = frozenset(),
    ) -> PiToolBridge:
        """Build a bridge over a live ``AgentLoop``.

        INTEGRATION NOTE: the adapter reaches ``loop._driver.planning_allowed_tool_
        names()`` because the loop re-exposes ``readonly_tool_names`` (engine.py:741)
        but NOT ``planning_allowed_tool_names``. That is a READ of a private
        accessor, not a source edit — so it stays additive and does not touch the
        forbidden shared files. If/when the loop grows a public passthrough, swap
        the adapter to it. # TODO(integration): prefer a public AgentLoop.
        planning_allowed_tool_names() passthrough (engine.py) over loop._driver.
        """
        return cls(
            _LoopBridgeHost(loop),
            assist=assist,
            virtual_tool_names=virtual_tool_names,
        )

    # -- public entry ---------------------------------------------------------
    def _critical_section(self) -> Any:
        """The loop lock guarding the pre-execution critical section — held across
        state→replan→planning→risk→action-append so the sequence is atomic w.r.t.
        the runtime-lifecycle task, exactly as ``_run_drive`` holds ``self._lock``
        per step (engine.py:1198) and RELEASES it before the long-running execute
        (engine.py:1466). A host without a lock (single-threaded fake) → an
        uncontended ``nullcontext``."""
        lock = getattr(self._host, "lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    async def route(
        self,
        tool_call: ToolCall,
        *,
        thought: str = "",
        self_assessed_risk: SecurityRisk | None = None,
        llm_response_id: str | None = None,
    ) -> BridgeOutcome:
        """Route ONE Pi tool call through the full wrapper.

        Gate order is PARITY-EQUIVALENT to ``AgentLoop._run_drive`` (and in the
        SAME ORDER):

          dangling reconcile (resume invariant) → TERMINAL/HALTED-state gate →
          stale-plan/replan gate → PLANNING phase gate → virtual short-circuit →
          action creation → hard-deny → risk/confirm → action append →
          execute+observe (K1 + pairing).

        Three ordering invariants vs ``_run_drive`` are LOAD-BEARING (the prior
        sketch drifted on each):

          * the HALTED-state gate runs FIRST so a call while the conversation is
            awaiting confirm/plan/user (or paused/terminal) is REFUSED without
            executing — fail CLOSED (mirror the _run_drive checkpoint
            engine.py:1216 + run() entry engine.py:1107);
          * the PLANNING gate runs BEFORE the virtual short-circuit (engine.py:1309
            runs ``_gate_planning_mode`` BEFORE the meta/virtual handlers at
            engine.py:1325) — so a non-allowlisted virtual (finish/serve/remember/
            notify_user/delegate_explore) is REJECTED in PLANNING, NOT virtual-
            handled; only the allowlisted virtuals (submit_plan/ask_user/clarify)
            survive to the short-circuit;
          * the stale-plan replan gate runs BEFORE the PLANNING gate so a CHANGE
            follow-up re-enters PLANNING and the PLANNING gate then rejects the
            write — the SAME net effect as the apply-time ``_gate_midstep_steer_
            replan`` (engine.py:1818), which re-enters planning then rejects a
            mutating tool / lets a read proceed.

        The state→replan→planning→hard-deny→risk→action-append sequence runs under
        the loop lock (the critical section); the long-running execute runs OUTSIDE
        it, mirroring ``_run_drive`` (engine.py:1466).
        """
        # G0 — dangling reconciliation (P2/#6): BEFORE accepting this new call,
        # pair any action interrupted by a crash between its emit and its
        # observation. Idempotent: a healthy log is a no-op (returns None). This is
        # the always-on enforcement of the resume invariant — a new call never
        # lands while a prior action dangles.
        async with self._critical_section():
            await self._reconcile_dangling()

            # G-STATE — terminal/halted-state gate (P0/#2). Fail CLOSED.
            halted = await self._gate_conversation_state()
            if halted is not None:
                return halted

            # G-REPLAN — stale-plan / change-steer replan gate (P0/#3). Inherit the
            # loop's REAL gate; a pending CHANGE follow-up re-enters PLANNING so the
            # PLANNING gate below rejects the write (no land on the stale plan).
            await self._maybe_reenter_planning_for_followup()

            # G1 — PLANNING execute-time gate (P0/#1), BEFORE the virtual short-
            # circuit (mirror engine.py:945-969). In PLANNING a non-allowlisted tool
            # (incl. finish/serve/remember/notify_user/delegate_explore) is REFUSED;
            # reads fall through to execution; the allowlisted virtuals (submit_plan/
            # ask_user/clarify) fall through to the short-circuit below.
            if self._host.mode == OperatingMode.PLANNING:
                refused = await self._gate_planning(
                    tool_call, thought, self_assessed_risk, llm_response_id
                )
                if refused is not None:
                    return refused

            # Virtual / meta tools are loop-handled, never executed here. Reached
            # ONLY by tools that SURVIVED the planning gate: in PLANNING that is the
            # allowlisted virtuals (submit_plan/ask_user/clarify); in execution mode
            # (gate is a no-op) every virtual.
            if tool_call.tool_name in self._virtual:
                return BridgeOutcome(decision=BridgeDecision.VIRTUAL)

            action = ActionEvent(
                thought=thought,
                tool_call=tool_call,
                self_assessed_risk=self_assessed_risk or SecurityRisk.UNKNOWN,
                llm_response_id=llm_response_id,
            )

            # G2 — HARD DENY (mirror engine.py:980-1001).
            refused = await self._gate_hard_deny(action)
            if refused is not None:
                return refused

            # G3 — RISK + CONFIRMATION gate (mirror engine.py:1003-1057).
            action, gated = await self._gate_risk_confirm(action)
            if gated:
                return BridgeOutcome(
                    decision=BridgeDecision.CONFIRM_REQUIRED,
                    action=action,
                    pending_action_id=action.id,
                )

            # Append the action under the lock (mirror engine.py:1467), then
            # release before the long-running execute.
            await self._host.emit(action)

        # G4 — execute with the K1 guard + pairing OUTSIDE the lock (mirror
        # engine.py:1466-1468: "EXECUTE outside the lock").
        return await self._execute_and_observe(action)

    # -- confirm/reject continuation ------------------------------------------
    async def resume_confirmed(self, action: ActionEvent) -> BridgeOutcome:
        """Execute a previously risk-gated action AFTER the user CONFIRMED it.

        The proposed ``ActionEvent`` was already appended by the risk gate (followed
        by a ``WAITING_FOR_CONFIRMATION`` status whose ``detail`` is the action id),
        and ``route`` returned ``CONFIRM_REQUIRED`` without executing. When the user
        confirms (via the control surface), the caller resumes the SAME action
        through the SAME safe execute/observe core (K1 guard + pairing) — mirror of
        ``AgentLoop.confirm`` (engine.py:1499) re-running the pending action through
        ``_execute_and_observe``. The action is NOT re-emitted (it is already in the
        log); only the observation/error pairing is appended. Used by the Pi runtime
        to wire ``CONFIRM_REQUIRED`` to a REAL confirm continuation (not a no-op)."""
        return await self._execute_and_observe(action)

    async def reject_pending(
        self, action: ActionEvent, reason: str = "rejected by user"
    ) -> BridgeOutcome:
        """Deny a previously risk-gated action AFTER the user REJECTED it.

        Pair the already-emitted proposed action with an ``AgentErrorEvent`` (so it
        is never left dangling) WITHOUT executing the tool — mirror of
        ``AgentLoop.reject`` (engine.py:1515). Returns a ``REFUSED`` outcome whose
        ``error`` carries the denial."""
        tc = action.tool_call
        error = AgentErrorEvent(
            error=f"The action was REJECTED by the user ({reason}) and was NOT executed.",
            action_id=action.id,
            tool_call_id=tc.call_id if tc else None,
        )
        await self._host.emit(error)
        return BridgeOutcome(
            decision=BridgeDecision.REFUSED, action=action, error=error
        )

    # -- G0: dangling reconciliation ------------------------------------------
    async def _reconcile_dangling(self) -> None:
        """Pair any crash-interrupted action BEFORE accepting a new call (P2/#6).

        Idempotent by construction: ``reconcile_dangling_action`` returns None once
        the dangling action is paired, so repeated calls are no-ops. Within a normal
        ``route`` the previous call already paired its action, so this is a no-op
        except on the FIRST call after a crash/resume."""
        recovered = self.reconcile_dangling_action(await self._host.events())
        if recovered is not None:
            await self._host.emit(recovered)

    # -- G-STATE: terminal / halted-state gate --------------------------------
    async def _gate_conversation_state(self) -> BridgeOutcome | None:
        """Terminal/halted-state gate (P0/#2). REFUSE a Pi tool call while the
        conversation is NOT in a drivable state — mirror the ``_run_drive``
        checkpoint (engine.py:1216) + ``run()`` entry (engine.py:1107): a
        conversation awaiting confirmation / plan approval / a user answer, paused,
        or terminal (finished/stuck/error) is not being driven, so a new tool call
        must NOT execute. Returns a ``HALTED`` outcome (nothing emitted — there is
        no action to pair) or ``None`` (drivable → proceed).

        FAIL CLOSED: if the state cannot be determined (the host raises), REFUSE."""
        try:
            state = await self._host.state()
        except Exception:  # noqa: BLE001 — indeterminate state must fail closed
            _LOG.warning(
                "Pi tool call REFUSED: conversation state indeterminate (fail-closed)",
                exc_info=True,
            )
            return BridgeOutcome(decision=BridgeDecision.HALTED)
        if state.execution_status in _HALTED_STATUSES:
            _LOG.info(
                "Pi tool call REFUSED: conversation is %s (not drivable) — routing "
                "to the control surface, not executing",
                state.execution_status,
            )
            return BridgeOutcome(
                decision=BridgeDecision.HALTED,
                pending_action_id=state.pending_action_id,
            )
        return None

    # -- G-REPLAN: stale-plan / change-steer replan gate ----------------------
    async def _maybe_reenter_planning_for_followup(self) -> None:
        """Stale-plan / change-steer replan gate (P0/#3). Delegate to the loop's
        REAL replan gate so a pending CHANGE follow-up on an approved build re-enters
        PLANNING — the PLANNING gate below then REJECTS the write, exactly as Disco's
        apply-time ``_gate_midstep_steer_replan`` does (engine.py:1818). Inheriting
        the real gate avoids re-implementing (and drifting from) ``signals.is_
        revision_intent``. Best-effort: a host that doesn't expose the gate (legacy/
        fake) simply skips it."""
        reenter = getattr(self._host, "reenter_planning_for_followup", None)
        if callable(reenter):
            await cast("Any", reenter(await self._host.events()))

    # -- G1: planning gate ----------------------------------------------------
    async def _gate_planning(
        self,
        tool_call: ToolCall,
        thought: str,
        self_assessed_risk: SecurityRisk | None,
        llm_response_id: str | None,
    ) -> BridgeOutcome | None:
        """Mirror of the allowlist-rejection branch of ``_gate_planning_mode``
        (engine.py:945-969). Returns a REFUSED outcome when the tool is not in the
        planning allowlist; ``None`` (fall through) when it is allowed.

        The submit_plan interception and tool-less-prose nudge (engine.py:864-916)
        are loop/virtual concerns, not executor-tool safety — the plan tool is in
        ``virtual_tool_names`` and short-circuits in :meth:`route` before reaching
        here.
        """
        allowed = self._host.planning_allowed_tool_names()
        if tool_call.tool_name in allowed:
            return None
        action = ActionEvent(
            thought=thought,
            tool_call=tool_call,
            self_assessed_risk=self_assessed_risk or SecurityRisk.UNKNOWN,
            llm_response_id=llm_response_id,
        )
        await self._host.emit(action)
        error = AgentErrorEvent(
            error=_PLANNING_REFUSAL.format(tool=tool_call.tool_name),
            action_id=action.id,
            tool_call_id=tool_call.call_id,
        )
        await self._host.emit(error)
        return BridgeOutcome(
            decision=BridgeDecision.REFUSED, action=action, error=error
        )

    # -- G2: hard deny --------------------------------------------------------
    async def _gate_hard_deny(self, action: ActionEvent) -> BridgeOutcome | None:
        """Mirror of ``_gate_hard_deny`` (engine.py:980-1001) over the FREE
        function ``signals.hard_deny_reason`` (signals.py:99). Refuses catastrophic
        commands BEFORE the confirm gate — no approval can run them."""
        deny_reason = signals.hard_deny_reason(action)
        if deny_reason is None:
            return None
        await self._host.emit(action)  # record the proposed action for audit
        error = AgentErrorEvent(
            error=_HARD_DENY_REFUSAL.format(reason=deny_reason),
            action_id=action.id,
            tool_call_id=action.tool_call.call_id if action.tool_call else None,
        )
        await self._host.emit(error)
        return BridgeOutcome(
            decision=BridgeDecision.REFUSED, action=action, error=error
        )

    # -- G3: risk + confirmation ----------------------------------------------
    async def _gate_risk_confirm(
        self, action: ActionEvent
    ) -> tuple[ActionEvent, bool]:
        """Mirror of ``_gate_risk_confirm`` (engine.py:1003-1057).

        Assess risk (stamping the detailed audit into the action meta when the
        analyzer supports it), resolve the tool scope, and consult the
        confirmation policy. Returns ``(action, gated)``; when ``gated`` the
        proposed action + a ``WAITING_FOR_CONFIRMATION`` status are emitted and the
        caller HALTs pending (the existing confirm/reject surface resumes it).
        """
        detailed = getattr(self._host.analyzer, "assess_detailed", None)
        if callable(detailed):
            assessment = cast("RiskAssessment", detailed(action))
            risk = assessment.risk
            action = action.model_copy(
                update={
                    "meta": {
                        **action.meta,
                        "risk_assessment": assessment.model_dump(mode="json"),
                    }
                }
            )
        else:
            risk = self._host.analyzer.assess(action)

        # WHERE the tool executes (duck-typed; absent → "unknown"), mirror 1024-1031.
        scope_fn = getattr(self._host.executor, "tool_scope", None)
        tool_scope = "unknown"
        if callable(scope_fn):
            try:
                tool_scope = scope_fn(action.tool_call.tool_name)
            except Exception:  # noqa: BLE001 — scope is best-effort
                tool_scope = "unknown"

        # should_confirm_action when available; fall back to should_confirm.
        sca = getattr(self._host.policy, "should_confirm_action", None)
        if callable(sca):
            gated = bool(sca(risk, scope=tool_scope, tool_name=action.tool_call.tool_name))
        else:
            gated = bool(self._host.policy.should_confirm(risk))

        # Journal the scope-based exemption (mirror 1043-1046).
        if not gated and self._host.policy.should_confirm(risk):
            action = action.model_copy(
                update={"meta": {**action.meta, "auto_approved": "sandboxed"}}
            )

        if gated:
            await self._host.emit(action)  # record the PROPOSED action
            await self._host.emit(
                StatusEvent(
                    status=ConversationStatus.WAITING_FOR_CONFIRMATION,
                    detail=action.id,
                )
            )
        return action, gated

    # -- G4: K1 guard/recovery + execute + pairing ----------------------------
    async def _execute_and_observe(self, action: ActionEvent) -> BridgeOutcome:
        """Mirror of the SAFETY core of ``Observer.execute_and_observe``
        (observe.py:226-459): optional F9 dedup, the K1 elision guard + recovery,
        the executor call, and the [CONTRACT] one-observation-per-action pairing.

        F8 arg-shrink, W-39 shell-verify reminder, sandbox-restart notice, and the
        plan-step done-condition note are LOOP-INTELLIGENCE / product surface left
        to the Pi loop (see the module header). Executor validation, the generic
        K1 boundary guard, and the file-write read-before-write/binary/syntax gates
        all come FOR FREE inside ``executor.execute`` (executor.py:363-418,
        files.py:212/411).
        """
        assert action.tool_call is not None  # route() never builds a toolless action
        tc = action.tool_call

        # F9 — assist-only read-dedup short-circuit (default OFF). Mirror 253-311.
        deduped = await self._maybe_f9_dedup(action)
        if deduped is not None:
            return deduped

        # K1 — elision-marker execution guard + recovery. Mirror 334-413.
        bad = find_elided_arg_markers(tc.arguments)
        if (
            bad == ["content"]
            and tc.tool_name == "file_write"
            and value_is_only_elision_marker(tc.arguments.get("content"))
        ):
            path = tc.arguments.get("path")
            if isinstance(path, str) and path:
                original = _recover_elided_file_write_content(
                    await self._host.events(), path, before_id=action.id
                )
                if original is not None:
                    _LOG.info(
                        "K1 recovery: re-expanded elided file_write content for %s "
                        "(%d chars, call_id=%s)",
                        path,
                        len(original),
                        tc.call_id,
                    )
                    tc.arguments["content"] = original
                    self._ground_read(path)  # engine supplied content → not a blind rewrite
                    bad = []  # recovered → fall through to normal execution
        if bad:
            _LOG.info(
                "K1 guard: rejected %s — arg(s) %s carry an elision placeholder "
                "(call_id=%s)",
                tc.tool_name,
                bad,
                tc.call_id,
            )
            error = AgentErrorEvent(
                error=_K1_REFUSAL.format(bad=bad),
                action_id=action.id,
                tool_call_id=tc.call_id,
            )
            await self._host.emit(error)
            return BridgeOutcome(
                decision=BridgeDecision.REFUSED, action=action, error=error
            )

        # Execute — never raises (mirror 416-429): exceptions become paired errors.
        try:
            result = await self._host.executor.execute(tc)
        except Exception as e:  # noqa: BLE001 — any failure is observable, never dangling
            error = AgentErrorEvent(
                error=str(e), action_id=action.id, tool_call_id=tc.call_id
            )
            await self._host.emit(error)
            return BridgeOutcome(
                decision=BridgeDecision.FAILED, action=action, error=error
            )

        # Pairing — exactly one observation per action (mirror 430-458).
        if result.success:
            observation = ObservationEvent(tool_result=result, action_id=action.id)
            await self._host.emit(observation)
            return BridgeOutcome(
                decision=BridgeDecision.EXECUTED,
                action=action,
                observation=observation,
            )
        error = AgentErrorEvent(
            error=result.error or "tool failed",
            action_id=action.id,
            tool_call_id=tc.call_id,
        )
        await self._host.emit(error)
        return BridgeOutcome(decision=BridgeDecision.FAILED, action=action, error=error)

    async def _maybe_f9_dedup(self, action: ActionEvent) -> BridgeOutcome | None:
        """ASSIST-only F9 read-dedup (default OFF). Mirror of observe.py:253-311.

        When ``assist`` is on and this read-only call exactly repeats a recent one
        within the window (and nothing invalidated it), emit a pointer observation
        instead of re-executing — AND ground the read so a following same-path write
        is not falsely blocked (the read-before-write loop the loop's ``_ground_read``
        closes, observe.py:296-299).
        """
        if not self._assist or action.tool_call is None:
            return None
        # P1/#5 — mirror Driver.readonly_tool_names (driver.py:159-170) EXACTLY:
        # absent OR raising → None (NOT an empty frozenset). None lets
        # _f9_dedupable_read fall back to the narrow _WORKSPACE_READ_TOOLS, the same
        # backstop the loop uses; an empty frozenset would instead WRONGLY mark every
        # tool non-read-only. Critically, the unguarded call previously raised
        # AFTER route() emitted the ActionEvent (and BEFORE any observation),
        # leaving a DANGLING action — the executor try/except below never covered
        # it. Catch here so a flaky readonly() degrades to "no dedup", never dangles.
        readonly = getattr(self._host.executor, "readonly_tool_names", None)
        readonly_names: frozenset[str] | None
        if readonly is None:
            readonly_names = None
        else:
            try:
                readonly_names = frozenset(readonly())
            except Exception:  # noqa: BLE001 — never let tool-listing dangle the action
                _LOG.debug("readonly_tool_names() raised; F9 falls back to None", exc_info=True)
                readonly_names = None
        deduped, prior_id, pointer = _f9_dedupable_read(
            action.tool_call.tool_name,
            action.tool_call.arguments,
            await self._host.events(),
            readonly_names=readonly_names,
        )
        if not deduped:
            return None
        _LOG.info(
            "F9 read-dedup: short-circuited %s (call_id=%s, prior_action_id=%s)",
            action.tool_call.tool_name,
            action.tool_call.call_id,
            prior_id,
        )
        path = action.tool_call.arguments.get("path")
        if isinstance(path, str) and path:
            self._ground_read(path)
        observation = ObservationEvent(
            tool_result=ToolResult(
                call_id=action.tool_call.call_id,
                tool_name=action.tool_call.tool_name,
                success=True,
                content=pointer,
            ),
            action_id=action.id,
        )
        await self._host.emit(observation)
        return BridgeOutcome(
            decision=BridgeDecision.EXECUTED, action=action, observation=observation
        )

    def _ground_read(self, path: str) -> None:
        """Satisfy the read-before-write gate for ``path`` via the executor when
        the bridge supplied the file's content through a non-tool channel (K1
        recovery / F9 pointer). Mirror of ``_ground_read`` (observe.py:45):
        best-effort, a fake executor without ``note_grounding_read`` is a no-op."""
        note = getattr(self._host.executor, "note_grounding_read", None)
        if callable(note) and isinstance(path, str) and path:
            try:
                note(path)
            except Exception:  # noqa: BLE001 — grounding is best-effort
                _LOG.debug("note_grounding_read failed for %s", path, exc_info=True)

    # -- dangling / missing-observation reconciliation ------------------------
    @staticmethod
    def reconcile_dangling_action(events: list[Event]) -> AgentErrorEvent | None:
        """THE DANGLING-OBSERVATION DECISION (codex EPIC-D flag).

        Disco's normal path guarantees exactly one observation per executed action
        (observe.py:226 [CONTRACT]); ``route`` upholds the same invariant by
        wrapping execution in try/except so even a raising tool yields a paired
        ``AgentErrorEvent`` — within a single live call there is NEVER a dangling
        action. The open case is a CRASH (process death / kill) BETWEEN appending
        the ``ActionEvent`` and appending its observation. Codex confirmed Disco
        DETECTS such a dangling action (test_loop_step.py:81) but core has NO
        auto-replay wrapper.

        DECISION for the Pi bridge: DETECT-AND-NEUTRALIZE, do NOT auto-replay.
        On resume, if the log's last tool action has no paired observation/error,
        emit a synthetic ``AgentErrorEvent`` (an "interrupted" observation) to
        RESTORE the action↔observation pairing — but NEVER re-execute the tool. A
        replay would risk DOUBLE side effects (a ``file_write`` / ``shell_exec`` may
        have partially or fully applied before the crash — re-running could corrupt
        or duplicate). Pairing the dangling action keeps the provider tool-call log
        valid (KV stability) and hands the model a clean, recoverable signal to
        re-issue the call itself if it still needs it — matching Disco's
        "detectable but not auto-replayed" posture.

        EXCLUSION: a confirmation-pending action is NOT dangling — the risk gate
        emits it FOLLOWED by a ``WAITING_FOR_CONFIRMATION`` status whose ``detail``
        is the action id (engine.py:1049-1054), and it is intentionally awaiting the
        confirm/reject surface, which executes it. Reconciling that would race the
        confirm path, so any action referenced by such a status is left alone.

        Returns the synthetic ``AgentErrorEvent`` to append (the caller appends it
        via the store), or ``None`` if the log is already consistent.
        """
        order: list[ActionEvent] = []
        paired_ids: set[str] = set()
        pending_ids: set[str] = set()
        for event in events:
            if isinstance(event, ActionEvent) and event.tool_call is not None:
                order.append(event)
            elif isinstance(event, ObservationEvent | AgentErrorEvent):
                if event.action_id:
                    paired_ids.add(event.action_id)
            elif (
                isinstance(event, StatusEvent)
                and event.status == ConversationStatus.WAITING_FOR_CONFIRMATION
                and event.detail
            ):
                pending_ids.add(event.detail)
        # The crash point is the MOST-RECENT action with no observation/error and
        # not gated pending confirmation. A healthy log pairs every action.
        last_action: ActionEvent | None = None
        for action in reversed(order):
            if action.id in paired_ids or action.id in pending_ids:
                continue
            last_action = action
            break
        if last_action is None:
            return None
        tc = last_action.tool_call
        _LOG.warning(
            "dangling action reconciled (no replay): %s action_id=%s — emitting "
            "interrupted observation",
            tc.tool_name if tc else "?",
            last_action.id,
        )
        return AgentErrorEvent(
            error=(
                "The previous tool call was INTERRUPTED before its result was "
                "recorded (the run was stopped or the process was killed mid-call). "
                "It was NOT re-executed automatically — a partially-applied side "
                "effect must not be blindly repeated. Re-issue the call if you "
                "still need it, after reading the current workspace state."
            ),
            action_id=last_action.id,
            tool_call_id=tc.call_id if tc else None,
        )
