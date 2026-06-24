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

from disco.core import ConversationState, MessageEvent

from ..build_messages import _context_message, _user_message
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
        steer: bool = False,
    ) -> MessageEvent:
        """Append the (optional hidden context +) user message, then kick — the
        same store appends + `kick` the WS/REST send_message path performs.

        Returns the stored USER message (the REST send/followup routes report its
        id/seq) — byte-identical to the append the routes did inline before the seam."""
        if context:
            await self._rt._store.append(conversation_id, _context_message(context))
        stored = await self._rt._store.append(
            conversation_id, _user_message(text, steer=steer)
        )
        self._rt.kick(conversation_id)
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
        await self._rt._control.kill(conversation_id)

    # -- events / state -------------------------------------------------------
    async def subscribe(
        self, conversation_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[KernelEvent]:
        return await self._rt._store.subscribe(conversation_id, after_seq=after_seq)

    async def get_state(self, conversation_id: str) -> ConversationState:
        return await self._rt._store.get_state(conversation_id)
