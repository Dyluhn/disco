"""The `BuildKernel` seam — the abstraction the Build loop runs through.

Disco Pi Build Kernel Campaign, PR A1. The point of this module is a thin,
behavior-preserving seam: today the agent-server drives ONE Build loop (Disco's
`AgentLoop`, scheduled by `ConversationRuntime.kick`, gated by `ControlOps`,
streamed through the event store). A future `PiKernel` should be selectable in
its place WITHOUT the rest of the agent-server caring which inner agent ran.

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
  * subscribe / get_state           → the existing event store

— it does NOT turn `AgentLoop.run()` into an `AsyncIterator[KernelEvent]`.
`KernelEvent` is therefore just a reuse of the existing `disco.core.Event`
spine (the store's element type), not a new parallel event type.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol, runtime_checkable

from disco.core import ConversationState, Event

# The experimental gate lives in the shared core layer (one source of truth for
# both sibling server packages) and is re-exported here so the build_kernel API
# surface stays self-contained. `EXPERIMENTAL_ENV` is the env var that unlocks the
# experimental Pi kernel; `experimental_kernels_enabled()` reads it live. Default
# OFF: selecting `pi_experimental` with it unset transparently downgrades to the
# Disco kernel, so a stale persisted setting can never route a real build through
# the stub.
from disco.core.llm.config import (
    BUILD_KERNEL_EXPERIMENTAL_ENV as EXPERIMENTAL_ENV,
)
from disco.core.llm.config import (
    build_kernel_experimental_enabled as experimental_kernels_enabled,
)

__all__ = [
    "EXPERIMENTAL_ENV",
    "BuildKernel",
    "BuildKernelKind",
    "BuildKernelPolicy",
    "KernelEvent",
    "experimental_kernels_enabled",
]

# A KernelEvent is exactly an event in the append-only store. The whole loop —
# Disco's today, Pi's later — communicates by appending these and the store
# fans them out to subscribers, so there is no separate kernel event type to
# translate. Aliased (not re-declared) so a kernel implementation and the rest
# of the agent-server share one event spine.
KernelEvent = Event

# The kernel choices exposed in Settings (A2). `disco` is the only production
# kernel today; `pi_experimental` is gated behind the experimental flag and
# currently wires to a stub.
BuildKernelKind = Literal["disco", "pi_experimental"]


@dataclass(frozen=True)
class BuildKernelPolicy:
    """Resolves the persisted kernel setting against the experimental gate.

    `selected` is the user's Settings value; `experimental_enabled` is whether
    the experimental flag is on. `effective_kind` is what the runtime ACTUALLY
    routes to — `pi_experimental` only survives when the flag is on, otherwise it
    falls back to `disco` (never silently routing a build through the stub)."""

    selected: BuildKernelKind = "disco"
    experimental_enabled: bool = False

    @classmethod
    def resolve(cls, selected: str | None) -> BuildKernelPolicy:
        """Build a policy from a persisted setting (`None`/unknown → `disco`),
        reading the experimental gate from the environment."""
        kind: BuildKernelKind = "pi_experimental" if selected == "pi_experimental" else "disco"
        return cls(selected=kind, experimental_enabled=experimental_kernels_enabled())

    @property
    def effective_kind(self) -> BuildKernelKind:
        """The kernel the runtime resolves to. `pi_experimental` is honored only
        when the experimental flag is on; otherwise it downgrades to `disco`."""
        if self.selected == "pi_experimental" and self.experimental_enabled:
            return "pi_experimental"
        return "disco"


@runtime_checkable
class BuildKernel(Protocol):
    """The inner Build agent behind a stable, runtime-shaped seam.

    Every method maps 1:1 to an existing agent-server entry point so the current
    `DiscoKernel` is a faithful pass-through (ZERO behavior change). A future
    `PiKernel` implements the same surface to host the Pi SDK in the same slot.

    The campaign's "core" surface is start / send_user_turn / approve_plan /
    reject_plan / cancel / resume + the event subscription accessor; the
    remaining methods (confirm / reject / request_plan / pick_alternative /
    pause / kill / get_state) round out the real Disco control surface the WS and
    REST routes already drive, so the seam is implementable end-to-end today.
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
        steer: bool = False,
    ) -> None:
        """Append a user turn (optionally a context block, optionally a mid-run
        steer) and start/continue the run."""
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

    # -- events / state -------------------------------------------------------
    async def subscribe(
        self, conversation_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[KernelEvent]:
        """The conversation's event stream: history after `after_seq`, then live
        appends. Awaited to obtain the async iterator (mirrors the store)."""
        ...

    async def get_state(self, conversation_id: str) -> ConversationState:
        """The reconstructed conversation state (status + pending gate ids)."""
        ...
