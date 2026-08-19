"""Pinned-kernel ingress and conversation control coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from disco.core import MessageEvent
from disco.core.appkit import BuildBrief
from disco.core.verification import VerificationRequirementsDirective

from .build_contract_service import BuildContractService
from .build_kernel import BuildKernel
from .control_ops import ControlOps
from .resume_service import ResumeService
from .run_registry import KernelPinRegistry


class ConversationControlService:
    """Own the stable kernel pin and all run-control ingress policy.

    Live-run lifecycle (pause/cancel/resume/kill) lives on
    ``RunLifecycleService`` — those operations never take a kernel
    continuation, so they are not ingress policy.
    """

    def __init__(
        self,
        contract: BuildContractService,
        pins: KernelPinRegistry,
        controls: ControlOps,
        resume: ResumeService,
    ) -> None:
        self._contract = contract
        self._pins = pins
        self._controls = controls
        self._resume = resume

    def _ensure_kernel(self, conversation_id: str) -> BuildKernel:
        return self._pins.ensure(conversation_id)

    def _clear_new_pin(self, conversation_id: str, newly_pinned: bool) -> None:
        if newly_pinned:
            self._pins.clear(conversation_id)

    def start(self, conversation_id: str) -> None:
        newly_pinned = self._pins.current(conversation_id) is None
        kernel = self._ensure_kernel(conversation_id)
        try:
            kernel.start(conversation_id)
        except BaseException:
            self._clear_new_pin(conversation_id, newly_pinned)
            raise

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
        await self._contract._fold_contract_from_history(conversation_id)
        newly_declared = build_brief is not None and not self._contract.has_declared_contract(
            conversation_id
        )
        self._contract.activate_contract_for_brief(conversation_id, build_brief)
        newly_pinned = self._pins.current(conversation_id) is None
        kernel = self._ensure_kernel(conversation_id)
        try:
            return await kernel.send_user_turn(
                conversation_id,
                text,
                context=context,
                build_brief=build_brief,
                verification_requirements=verification_requirements,
                steer=steer,
            )
        except BaseException:
            self._clear_new_pin(conversation_id, newly_pinned)
            if newly_declared:
                self._contract.set_build_kind(conversation_id, None)
                self._contract.release_fold_attempt(conversation_id)
            raise

    async def _continue(
        self,
        conversation_id: str,
        call: Callable[[BuildKernel], Awaitable[Any]],
    ) -> Any:
        await self._contract._fold_contract_from_history(conversation_id)
        newly_pinned = self._pins.current(conversation_id) is None
        kernel = self._ensure_kernel(conversation_id)
        try:
            return await call(kernel)
        except BaseException:
            self._clear_new_pin(conversation_id, newly_pinned)
            raise

    async def confirm(self, conversation_id: str) -> None:
        await self._continue(conversation_id, lambda kernel: kernel.confirm(conversation_id))

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        await self._continue(
            conversation_id,
            lambda kernel: kernel.reject(conversation_id, reason),
        )

    async def approve_plan(self, conversation_id: str) -> None:
        await self._continue(
            conversation_id,
            lambda kernel: kernel.approve_plan(conversation_id),
        )

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        await self._continue(
            conversation_id,
            lambda kernel: kernel.request_plan(conversation_id, text),
        )

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        await self._continue(
            conversation_id,
            lambda kernel: kernel.pick_alternative(conversation_id, option_id),
        )

    async def accept_finished(self, conversation_id: str, text: str = "") -> None:
        """Explicit `accept_finished` frame: authoritative stop-intent on a
        FINISHED build. Delegates straight to ControlOps (like pause/cancel) —
        no kernel continuation, because accepting the result runs nothing."""
        await self._controls.accept_finished(conversation_id, text)

    async def resume_conversation(self, conversation_id: str) -> dict[str, object]:
        await self._contract._fold_contract_from_history(conversation_id)
        return dict(await self._resume.resume_conversation(conversation_id))
