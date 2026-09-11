"""Narrow dependency protocols for run supervision."""

from __future__ import annotations

import asyncio
from typing import Protocol

from disco.core import ConversationStatus


class RunReentryPort(Protocol):
    def kick(self, conversation_id: str, *, claimed_user_seq: int | None = None) -> None: ...

    async def resume_conversation(self, conversation_id: str) -> dict[str, object]: ...


class RunSurfacePolicy(Protocol):
    def surface(self, conversation_id: str) -> str: ...

    def is_build_surface(self, surface: str) -> bool: ...

    def autonomous(self, conversation_id: str) -> bool: ...


class RunObservabilityPort(Protocol):
    def emit_audit(self, conversation_id: str, status: ConversationStatus) -> None: ...

    async def emit_reminder(self, conversation_id: str, message: str) -> None: ...


class RunPreflightPort(Protocol):
    async def driver_failure(self, conversation_id: str) -> str | None: ...

    async def sandbox_failure(self, conversation_id: str) -> str | None: ...


class RunPersistencePort(Protocol):
    def workspace_lock(self, conversation_id: str) -> asyncio.Lock: ...

    async def rehydrate(self, conversation_id: str) -> None: ...

    async def rematerialize_uploads(self, conversation_id: str) -> None: ...

    async def rematerialize_reference_packs(self, conversation_id: str) -> None: ...

    async def snapshot(self, conversation_id: str, *, trigger: str) -> None: ...


class DeepResearchRunPort(Protocol):
    async def run_deep_research(self, conversation_id: str) -> None: ...
