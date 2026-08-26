"""Ingress action methods for :class:`ConversationRuntime`.

The facade delegates these operations to its named owners; keeping the action
seam in a mixin keeps the composition facade small without changing its API.
"""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from disco.core import MessageEvent
from disco.core.appkit import BuildBrief, classify_build_brief
from disco.core.verification import VerificationRequirementsDirective


class _RuntimeActionsMixin:
    def start(self, conversation_id: str) -> None:
        self.conversation_control.start(conversation_id)

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
        # The normal Build client sends only the advisory brief marker. Resolve
        # governed AppKit mode here, at the host boundary, before contract
        # activation or loop composition. This keeps direct sends and lazy
        # pre-created attachment/reference-pack conversations on one path.
        if (
            build_brief is None
            and self._surface_of(conversation_id) == "build"
            and await self.settings._conversation_is_pristine(conversation_id)
        ):
            build_brief = classify_build_brief(text)
        if build_brief is not None and self._surface_of(conversation_id) == "build":
            await self._promote_appkit_for_build(conversation_id, build_brief)

        # Build briefs are an explicit Build-contract activation signal, not a
        # generic task hint. Older clients sent the marker for Agent because the
        # two surfaces share a stream hook; fail neutral at the server boundary
        # so an ordinary Agent task can never acquire web-app completion gates.
        # Strict AppKit conversations retain their own classified contract.
        if (
            build_brief is not None
            and self._surface_of(conversation_id) == "agent"
            and not self.settings._effective_appkit_mode(conversation_id)
        ):
            build_brief = None
        return await self.conversation_control.send_user_turn(
            conversation_id,
            text,
            context=context,
            build_brief=build_brief,
            verification_requirements=verification_requirements,
            steer=steer,
        )

    async def confirm(self, conversation_id: str) -> None:
        await self.conversation_control.confirm(conversation_id)

    async def reject(
        self,
        conversation_id: str,
        reason: str = "rejected by user",
    ) -> None:
        await self.conversation_control.reject(conversation_id, reason)

    async def approve_plan(self, conversation_id: str) -> None:
        await self.conversation_control.approve_plan(conversation_id)

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        await self.conversation_control.request_plan(conversation_id, text)

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        await self.conversation_control.pick_alternative(conversation_id, option_id)

    async def pause(self, conversation_id: str) -> None:
        await self._run_lifecycle.pause(conversation_id)

    async def cancel(self, conversation_id: str) -> None:
        await self._run_lifecycle.cancel(conversation_id)

    async def resume(self, conversation_id: str) -> None:
        await self._run_lifecycle.resume(conversation_id)

    async def kill(self, conversation_id: str) -> None:
        await self._run_lifecycle.kill(conversation_id)

    async def aclose(self) -> None:
        await self._run_supervisor.cancel_runs()
        await self._driver_preflight.aclose()
        await self._run_supervisor.close_resources()
