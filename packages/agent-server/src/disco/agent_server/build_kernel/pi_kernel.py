"""`PiKernel` — the experimental Pi-SDK Build kernel (STUB).

PR A1/A2 ships only the selector wiring: when Settings selects
`pi_experimental` AND the experimental flag is on, the runtime resolves THIS
kernel instead of `DiscoKernel`. Every operation raises `NotImplementedError`
for now — later campaign epics fill in the Pi SDK sidecar, the Disco inference
gateway, the tool bridge, and the plan-pause surface behind this same
`BuildKernel` interface. Its mere presence proves the selector path end-to-end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, ClassVar

from disco.core import ConversationState

from .base import KernelEvent

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime

_MSG = (
    "The experimental Pi build kernel is not implemented yet "
    "(Disco Pi Build Kernel Campaign — later epics). Select the 'disco' kernel."
)


class PiKernel:
    """Stub `BuildKernel`. Constructs fine (so selector wiring + typing exist);
    every operation raises `NotImplementedError` until later epics land it."""

    name: ClassVar[str] = "pi_experimental"

    def __init__(self, runtime: ConversationRuntime | Any) -> None:
        self._rt = runtime

    def start(self, conversation_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def send_user_turn(
        self,
        conversation_id: str,
        text: str,
        *,
        context: str | None = None,
        steer: bool = False,
    ) -> None:
        raise NotImplementedError(_MSG)

    async def approve_plan(self, conversation_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def reject_plan(self, conversation_id: str, reason: str = "") -> None:
        raise NotImplementedError(_MSG)

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        raise NotImplementedError(_MSG)

    async def confirm(self, conversation_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        raise NotImplementedError(_MSG)

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def pause(self, conversation_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def cancel(self, conversation_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def resume(self, conversation_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def kill(self, conversation_id: str) -> None:
        raise NotImplementedError(_MSG)

    async def subscribe(
        self, conversation_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[KernelEvent]:
        raise NotImplementedError(_MSG)

    async def get_state(self, conversation_id: str) -> ConversationState:
        raise NotImplementedError(_MSG)
