"""Live-run lifecycle control: pause, cancel, resume, kill."""

from __future__ import annotations

from disco.core import ConversationStatus

from .build_contract_service import BuildContractService
from .control_ops import ControlOps
from .run_controller import RunController
from .run_registry import CancellationRegistry, KernelPinRegistry, RunRegistry


class RunLifecycleService:
    """Own winding a live run down and back up (pause/cancel/resume/kill).

    Extracted from ConversationControlService at the PKG-26 width boundary:
    that class owns kernel-pin ingress policy (every kernel continuation),
    while these four operations never touch a kernel continuation — they
    stop, restart or tear down the run that is already underway. Winding a
    run down also owns releasing its kernel pin (``kill``), which is why the
    pin registry rides here rather than a call back into the ingress owner.
    """

    def __init__(
        self,
        contract: BuildContractService,
        pins: KernelPinRegistry,
        runs: RunRegistry,
        cancellations: CancellationRegistry,
        controller: RunController,
        controls: ControlOps,
    ) -> None:
        self._contract = contract
        self._pins = pins
        self._runs = runs
        self._cancellations = cancellations
        self._controller = controller
        self._controls = controls

    async def pause(self, conversation_id: str) -> None:
        await self._controls.pause(conversation_id)

    async def cancel(self, conversation_id: str) -> None:
        await self._controls.cancel(conversation_id)

    async def resume(self, conversation_id: str) -> None:
        self._cancellations.clear(conversation_id)
        self._controller.kick(conversation_id)

    async def kill(self, conversation_id: str) -> None:
        generation = self._runs.generation(conversation_id)
        self._contract._emit_toolscope_audit_summary(
            conversation_id,
            ConversationStatus.IDLE,
        )
        self._pins.clear_if_current(conversation_id, generation, self._runs)
        await self._controls.kill(conversation_id, generation)
