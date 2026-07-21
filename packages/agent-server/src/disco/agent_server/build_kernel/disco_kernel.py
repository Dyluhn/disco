"""`DiscoKernel` — the current Build loop behind the `BuildKernel` seam.

PR A1: a pure, behavior-preserving wrapper. Every method delegates to the SAME
entry point the agent-server already calls, so routing a build through the
`DiscoKernel` is observationally identical to today —

  * start / send_user_turn → `ConversationRuntime.kick` (+ the same store appends
    the WS/REST send paths do via `_user_message` / `_context_message`)
  * confirm / reject / approve_plan / request_plan / pause / cancel / kill →
    the existing `ControlOps` collaborator (`runtime._control`)
  * reject_plan → `ControlOps.request_plan` (Disco models plan rejection as a
    re-plan request)
  * pick_alternative → the existing `_loop_for(...).pick_alternative` path
  * resume → the existing `ResumeOps.resume_conversation` (the mode-agnostic
    resume the WS `resume` frame + REST `/resume` already use)
  * subscribe / get_state → the existing event store

IMPORTANT (no recursion): this delegates to the runtime's COLLABORATORS
(`_control`, `_resume`) and inner resolvers (`_loop_for`, `kick`, `_store`),
NEVER to the runtime's public control methods — those public methods route
THROUGH the active kernel (the A1 seam), so calling them here would loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, ClassVar, cast

from disco.core import (
    ConversationState,
    ConversationStatus,
    MessageEvent,
    StatusEvent,
)
from disco.core.appkit import BuildBrief

from ..build_messages import _build_brief_message, _context_message, _user_message
from .base import KernelEvent

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime


class DiscoKernel:
    """The Disco `AgentLoop`-driven Build, as a `BuildKernel`. Thin facade over
    `ConversationRuntime`; holds only a back-reference (like `ControlOps`)."""

    name: ClassVar[str] = "disco"

    def __init__(self, runtime: ConversationRuntime | Any) -> None:
        self._rt = runtime

    # -- lifecycle ------------------------------------------------------------
    def start(self, conversation_id: str) -> None:
        self._rt.kick(conversation_id)

    async def send_user_turn(
        self,
        conversation_id: str,
        text: str,
        *,
        context: str | None = None,
        build_brief: BuildBrief | None = None,
        steer: bool = False,
    ) -> MessageEvent:
        """Append the (optional hidden context +) user message, then kick — the
        same store appends + `kick` the WS/REST send_message path performs.

        Returns the stored USER message (the REST send/followup routes report its
        id/seq) — byte-identical to the append the routes did inline before the seam."""
        # The user turn, its durable revision marker, and task registration are
        # one linearizable ingress operation relative to host-side mutations.
        # The newly registered task queues on this same non-reentrant fence and
        # cannot execute until this block publishes the complete turn.
        async with self._rt.workspace_lock(conversation_id):
            async with self._rt._workspace.interprocess_mutation_fence(conversation_id):
                pending = []
                if context:
                    pending.append(_context_message(context))
                if build_brief is not None:
                    pending.append(_build_brief_message(build_brief))
                user_event = _user_message(text, steer=steer)
                pending.append(user_event)
                # USER and run-intent share one SQLite transaction: cancellation
                # can leave neither or both, never a durable unmarked user turn.
                stored_events = await self._rt._workspace.append_run_ingress_locked(
                    conversation_id,
                    pending,
                    "user-turn",
                )
                stored = next(
                    event
                    for event in stored_events
                    if isinstance(event, MessageEvent) and event.id == user_event.id
                )
                # A live revision steer needs a sequence-stable marker that cannot be
                # masked by later in-flight Action/Observation events.
                if steer:
                    from disco.core.loop import signals as _signals

                    if _signals.is_revision_intent(text) and not (
                        _signals.current_blocked_question_landing(
                            await self._rt._store.get_events(conversation_id)
                        )
                    ):
                        await self._rt._store.append(
                            conversation_id,
                            StatusEvent(
                                status=ConversationStatus.RUNNING,
                                detail="revision_steer_pending",
                            ),
                        )
                self._rt.kick(conversation_id, claimed_user_seq=stored.seq)
                self._rt._workspace.claim_registered_run_locked(conversation_id)
        return cast("MessageEvent", stored)

    # -- plan gate ------------------------------------------------------------
    async def approve_plan(self, conversation_id: str) -> None:
        await self._rt._control.approve_plan(conversation_id)

    async def reject_plan(self, conversation_id: str, reason: str = "") -> None:
        # Disco has no distinct "reject plan" op — a rejected plan is a re-plan
        # request carrying the user's revision (empty reason → plain replan).
        await self._rt._control.request_plan(conversation_id, reason)

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        await self._rt._control.request_plan(conversation_id, text)

    # -- action gate ----------------------------------------------------------
    async def confirm(self, conversation_id: str) -> None:
        await self._rt._control.confirm(conversation_id)

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        await self._rt._control.reject(conversation_id, reason)

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        # The inner impl (runtime.pick_alternative does exactly this); replicated
        # here so the public runtime method can route through the kernel safely.
        loop = self._rt._loop_for(conversation_id)
        await loop.pick_alternative(option_id)

    # -- stop / resume --------------------------------------------------------
    async def pause(self, conversation_id: str) -> None:
        await self._rt._control.pause(conversation_id)

    async def cancel(self, conversation_id: str) -> None:
        await self._rt._control.cancel(conversation_id)

    async def resume(self, conversation_id: str) -> None:
        await self._rt._resume.resume_conversation(conversation_id)

    async def kill(self, conversation_id: str) -> None:
        # Generation-guarded kill (finding #4) — mirror `ConversationRuntime.kill`:
        # capture the run-generation, clear only THIS generation's pin, and thread the
        # generation to the control op so a newer run that starts during teardown is
        # neither terminalized nor torn down.
        generation = self._rt._run_generation.get(conversation_id)
        self._rt._unpin_if_current_generation(conversation_id, generation)
        await self._rt._control.kill(conversation_id, generation)

    # -- events / state -------------------------------------------------------
    async def subscribe(
        self, conversation_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[KernelEvent]:
        return await self._rt._store.subscribe(conversation_id, after_seq=after_seq)

    async def get_state(self, conversation_id: str) -> ConversationState:
        return await self._rt._store.get_state(conversation_id)
