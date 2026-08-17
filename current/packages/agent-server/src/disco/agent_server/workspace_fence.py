"""Permanent workspace locks, process fences, and run-claim state."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from disco.core.store.sqlite import SqliteEventStore

from .project_runtime_service import ProjectRuntimeService
from .runtime_settings import _BUILD_LIKE_SURFACES, RuntimeSettings
from .workspace_process_fence import (
    workspace_process_fence,
    workspace_process_fence_held,
)

_OWNED_WORKSPACE_FENCES: ContextVar[frozenset[tuple[str, asyncio.Task[object]]]] = ContextVar(
    "disco_owned_workspace_fences", default=frozenset()
)


class WorkspaceFenceService:
    """Own the permanent serialization domain shared by workspace services."""

    def __init__(
        self,
        store: SqliteEventStore,
        projects: ProjectRuntimeService,
        settings: RuntimeSettings,
    ) -> None:
        self._store = store
        self._projects = projects
        self._settings = settings
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_runs: set[str] = set()
        self._admitted_runs: set[str] = set()

    def lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def _workspace_path(self, conversation_id: str) -> Path:
        store = self._projects.current_project_store()
        if store is not None:
            return store.path_for(conversation_id)
        store_key = hashlib.sha256(f"{id(self._store)}:{conversation_id}".encode()).hexdigest()
        return Path(tempfile.gettempdir()) / "disco-ephemeral-workspaces" / store_key

    @asynccontextmanager
    async def interprocess_mutation_fence(
        self,
        conversation_id: str,
        *,
        wait: bool = True,
    ) -> AsyncIterator[None]:
        if not self.lock(conversation_id).locked():
            raise RuntimeError("process workspace fence requires the conversation lock")
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("process workspace fence requires an asyncio task")
        token = _OWNED_WORKSPACE_FENCES.set(
            _OWNED_WORKSPACE_FENCES.get() | {(conversation_id, owner)}
        )
        try:
            async with workspace_process_fence(
                self._workspace_path(conversation_id),
                wait=wait,
            ):
                yield
        finally:
            _OWNED_WORKSPACE_FENCES.reset(token)

    @staticmethod
    def fence_owned_by_current_task(conversation_id: str) -> bool:
        owner = asyncio.current_task()
        return owner is not None and (conversation_id, owner) in _OWNED_WORKSPACE_FENCES.get()

    def require_process_fence_locked(self, conversation_id: str) -> None:
        if not workspace_process_fence_held(self._workspace_path(conversation_id)):
            raise RuntimeError("durable run intent requires the workspace process fence")

    def mark_registered_run_locked(
        self,
        conversation_id: str,
        *,
        active: bool,
    ) -> None:
        if not self.lock(conversation_id).locked():
            raise RuntimeError("run claim requires the workspace fence")
        if active and conversation_id not in self._admitted_runs:
            self._pending_runs.add(conversation_id)

    def mark_run_admitted(self, conversation_id: str) -> None:
        self._pending_runs.discard(conversation_id)
        self._admitted_runs.add(conversation_id)

    def clear_run_claim(self, conversation_id: str) -> None:
        self._pending_runs.discard(conversation_id)
        self._admitted_runs.discard(conversation_id)

    def has_admitted_run(self, conversation_id: str) -> bool:
        return conversation_id in self._admitted_runs

    def has_run_claim(self, conversation_id: str) -> bool:
        return conversation_id in self._pending_runs or self.has_admitted_run(conversation_id)

    @asynccontextmanager
    async def _lifecycle_fence(self, conversation_id: str) -> AsyncIterator[None]:
        if self._settings._surface_of(conversation_id) not in _BUILD_LIKE_SURFACES:
            yield
            return
        if self.fence_owned_by_current_task(conversation_id):
            yield
            return
        async with self.lock(conversation_id):
            async with self.interprocess_mutation_fence(conversation_id):
                yield

    @asynccontextmanager
    async def _lifecycle_locked_fence(
        self,
        conversation_id: str,
    ) -> AsyncIterator[None]:
        if not self.lock(conversation_id).locked():
            raise RuntimeError("locked lifecycle transition requires the workspace fence")
        if self._settings._surface_of(conversation_id) not in _BUILD_LIKE_SURFACES:
            yield
            return
        if self.fence_owned_by_current_task(conversation_id):
            yield
            return
        async with self.interprocess_mutation_fence(conversation_id):
            yield

    def _require_lifecycle_fence(self, conversation_id: str) -> None:
        if self._settings._surface_of(conversation_id) not in _BUILD_LIKE_SURFACES:
            return
        if not self.lock(conversation_id).locked():
            raise RuntimeError("locked status append requires the workspace fence")
        self.require_process_fence_locked(conversation_id)
