"""Narrow typed access to an already-live preview sandbox session."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from disco.tools import SandboxSession

type PreviewSession = SandboxSession
type PreviewSessionResolver = Callable[
    ..., PreviewSession | None | Awaitable[PreviewSession | None]
]
type AsyncPreviewSessionResolver = Callable[
    ..., Awaitable[PreviewSession | None]
]


class LiveSessionAccess(Protocol):
    def live_session(self, conversation_id: str) -> PreviewSession | None: ...


class PreviewSessionAccess(LiveSessionAccess, Protocol):
    def resolve_cid_prefix(self, cid8: str) -> str | None: ...

    async def resolve_owned_cid_prefix(self, cid8: str, owner_id: str) -> str | None: ...
