"""Typed one-time binding for the run-supervision construction cycle."""

from __future__ import annotations

from typing import Protocol


class RunCompletionPort(Protocol):
    async def finalize_clean(
        self,
        conversation_id: str,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None: ...

    async def terminalize_crash(
        self,
        conversation_id: str,
        error: BaseException,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None: ...

    async def rekick_unadmitted(self, conversation_id: str) -> None: ...


class DeferredRunCompletion:
    """Fail closed until the composition root closes the supervision cycle."""

    def __init__(self) -> None:
        self._target: RunCompletionPort | None = None

    def bind(self, target: RunCompletionPort) -> None:
        if self._target is not None:
            raise RuntimeError("run completion target is already bound")
        self._target = target

    def _bound(self) -> RunCompletionPort:
        if self._target is None:
            raise RuntimeError("run completion target is not bound")
        return self._target

    async def finalize_clean(
        self,
        conversation_id: str,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None:
        await self._bound().finalize_clean(
            conversation_id,
            generation,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def terminalize_crash(
        self,
        conversation_id: str,
        error: BaseException,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None:
        await self._bound().terminalize_crash(
            conversation_id,
            error,
            generation,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def rekick_unadmitted(self, conversation_id: str) -> None:
        await self._bound().rekick_unadmitted(conversation_id)
