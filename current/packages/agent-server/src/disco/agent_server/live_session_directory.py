"""Read-only ownership boundary for already-live sandbox sessions."""

from __future__ import annotations

from typing import cast

from disco.core.store.sqlite import SqliteEventStore
from disco.tools import SandboxSession

from .run_registry import RunResourceRegistry


class LiveSessionDirectory:
    """Resolve existing sessions and conversation prefixes without creating work."""

    def __init__(
        self,
        run_resources: RunResourceRegistry,
        store: SqliteEventStore,
    ) -> None:
        self._run_resources = run_resources
        self._store = store

    def live_session(self, conversation_id: str) -> SandboxSession | None:
        executor = self._run_resources.executor(conversation_id)
        session = executor.sandbox if executor is not None else None
        return cast(SandboxSession | None, session)

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        matches = [
            cid
            for cid in self._run_resources.conversation_ids(executors_only=True)
            if cid.removeprefix("conv_").startswith(cid8)
        ]
        return matches[0] if len(matches) == 1 else None

    async def resolve_owned_cid_prefix(self, cid8: str, owner_id: str) -> str | None:
        matches: list[str] = []
        for cid in self._run_resources.conversation_ids(executors_only=True):
            matches_prefix = cid.removeprefix("conv_").startswith(cid8)
            if matches_prefix and await self._store.conversation_owned_by(cid, owner_id):
                matches.append(cid)
        return matches[0] if len(matches) == 1 else None
