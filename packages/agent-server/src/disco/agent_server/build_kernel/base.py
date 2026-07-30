"""The `BuildKernel` seam — the abstraction the Build loop runs through.

Build kernel protocol. The point of this module is a thin,
behavior-preserving seam: today the agent-server drives ONE Build loop (Disco's
`AgentLoop`, scheduled by `ConversationRuntime.kick`, gated by `ControlOps`,
streamed through the event store).

Design note (per the codex SEAM EVAL of the real code): the current architecture
is an **append-only event store + task supervision**, NOT a loop that yields
events. The runtime schedules `AgentLoop.run()` as a background task; the loop
appends events to the store; the UI/WS subscribe to the store. So `BuildKernel`
is an adapter over the EXISTING entry points —

  * start / send_user_turn          → `ConversationRuntime.kick` (+ store append)
  * approve_plan / reject_plan /
    confirm / reject / request_plan  → `ControlOps` (the plan/action gate)
  * pick_alternative / pause /
    cancel / resume / kill           → the existing control surface

— it does NOT turn `AgentLoop.run()` into an `AsyncIterator[KernelEvent]`.
`KernelEvent` is therefore just a reuse of the existing `disco.core.Event`
spine (the store's element type), not a new parallel event type.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Protocol, runtime_checkable

from disco.core import Event, MessageEvent
from disco.core.appkit import BuildBrief
from disco.core.verification import VerificationRequirementsDirective

__all__ = [
    "BuildKernel",
    "BuildKernelKind",
    "KernelEvent",
]

# A KernelEvent is exactly an event in the append-only store. The whole loop —
# Disco's today, Pi's later — communicates by appending these and the store
# fans them out to subscribers, so there is no separate kernel event type to
# translate. Aliased (not re-declared) so a kernel implementation and the rest
# of the agent-server share one event spine.
KernelEvent = Event

# The only active build kernel. The vestigial persisted config field lives in
# core for legacy-load compatibility, but runtime selection always resolves here.
BuildKernelKind = Literal["disco"]


@runtime_checkable
class BuildKernel(Protocol):
    """The inner Build agent behind a stable, runtime-shaped seam.

    Every method maps 1:1 to an existing agent-server entry point so the current
    `DiscoKernel` is a faithful pass-through (ZERO behavior change).

    The campaign's "core" surface is start / send_user_turn / approve_plan /
    reject_plan / cancel / resume; the remaining methods (confirm / reject /
    request_plan / pick_alternative / pause / kill) round out the real Disco
    control surface the WS and REST routes already drive, so the seam is
    implementable end-to-end today.
    """

    #: Stable identifier of the kernel implementation (matches `BuildKernelKind`).
    name: ClassVar[str]

    # -- lifecycle ------------------------------------------------------------
    def start(self, conversation_id: str) -> None:
        """Schedule the conversation's build to (continue to) run. Idempotent —
        a no-op if a run is already in flight (the next step picks up new input)."""
        ...

    async def send_user_turn(
        self,
        conversation_id: str,
        text: str,
        *,
        context: str | None = None,
        build_brief: BuildBrief | None = None,
        verification_requirements: VerificationRequirementsDirective | None = None,
        steer: bool = False,
    ) -> MessageEvent:
        """Append a user turn (optionally a context block, optionally a mid-run
        steer) and start/continue the run. Returns the stored USER message so a
        caller can report its id/seq (the REST send/followup routes do)."""
        ...

    # -- plan gate ------------------------------------------------------------
    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan and begin executing it (full tools)."""
        ...

    async def reject_plan(self, conversation_id: str, reason: str = "") -> None:
        """Reject the pending plan and re-enter planning with the user's revision
        (Disco models plan rejection as a re-plan request)."""
        ...

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        """(Re-)enter plan mode with the user's instruction — first plan or replan."""
        ...

    # -- action gate ----------------------------------------------------------
    async def confirm(self, conversation_id: str) -> None:
        """Approve the pending (blast-radius-gated) action and resume."""
        ...

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        """Deny the pending action (no execution) and resume."""
        ...

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        """Resume an AWAITING_USER_DECISION gate by selecting a proposed option."""
        ...

    # -- stop / resume --------------------------------------------------------
    async def pause(self, conversation_id: str) -> None:
        """Cooperatively pause at the next step boundary (resume re-starts)."""
        ...

    async def cancel(self, conversation_id: str) -> None:
        """Cooperative stop — the run winds down to a terminal status."""
        ...

    async def resume(self, conversation_id: str) -> None:
        """Continue a stopped/incomplete run (mode-agnostic resume)."""
        ...

    async def kill(self, conversation_id: str) -> None:
        """The hard kill switch — halt the run, revoke caps, tear down the box."""
        ...
