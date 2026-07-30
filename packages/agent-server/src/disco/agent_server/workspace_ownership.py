"""Conversation disposal, resource teardown, and restore-session acquisition.

Cohesive ownership-only helpers split from :class:`WorkspaceCoordinator` so the
coordinator constructor stays at ten or fewer direct collaborators.  Each
helper here receives its actual collaborators directly — no runtime back-ref,
no ``rt: Any``, no multi-domain locator.

Behavior is preserved exactly: the lock → interprocess fence → store/project
mutation order, run-generation authority, session/executor pairing, persisted
bytes, and owner checks are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from disco.tools.sandbox._container import PREVIEW_PORT

if TYPE_CHECKING:
    from disco.tools import SandboxSession

    from .build_contract_service import BuildContractService
    from .build_loop_factory import BuildLoopFactory
    from .connection_tracker import ConnectionTracker
    from .deep_research_service import DeepResearchService
    from .driver_context_state import DriverContextState
    from .driver_runtime import DriverPreflight
    from .mcp_manager import McpManager
    from .preview_service import PreviewService
    from .run_registry import (
        CancellationRegistry,
        KernelPinStore,
        LoopRegistry,
        RunAuthorityLedger,
        RunIngressLedger,
        RunRecoveryLedger,
        RunRegistry,
        RunResourceRegistry,
    )
    from .runtime_settings import RuntimeSettings
    from .sandbox_runtime_service import SandboxRuntimeService
    from .space_service import SpaceService

_LOG = logging.getLogger(__name__)


class RunTaskDisposal:
    """Cancel a conversation's registered run task and dispose its resources.

    Owns the kernel-pin clear, task cancel/await, authority discard, and the
    executor/pending-session/sandbox teardown performed during ``forget``.
    """

    def __init__(
        self,
        kernel_pins: KernelPinStore,
        run_registry: RunRegistry,
        run_authorities: RunAuthorityLedger,
        run_resources: RunResourceRegistry,
        sandbox: SandboxRuntimeService,
    ) -> None:
        self._kernel_pins = kernel_pins
        self._run_registry = run_registry
        self._run_authorities = run_authorities
        self._run_resources = run_resources
        self._sandbox = sandbox

    def active_task(self, conversation_id: str) -> asyncio.Task[object] | None:
        return self._run_registry.active_task(conversation_id)

    def current_lifecycle_task_authority(
        self,
        conversation_id: str,
    ) -> tuple[str | None, str | None]:
        """Return only the exact registered task's captured durable target."""

        task = asyncio.current_task()
        if task is None or not self._run_registry.owns_task(conversation_id, task):
            return None, None
        return self._run_authorities.get(task)

    async def cancel_registered_task(self, conversation_id: str) -> None:
        self._kernel_pins.clear(conversation_id)
        task = self._run_registry.task(conversation_id)
        self._run_registry.detach_task_if_owned(conversation_id, task)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if task is not None:
            self._run_authorities.discard(task)

    async def dispose_resources(self, conversation_id: str) -> None:
        executor = self._run_resources.pop_executor(conversation_id)
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()
        session = self._run_resources.pop_pending_session(conversation_id)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.destroy()
        with contextlib.suppress(Exception):
            await self._sandbox._sandbox_service_now().destroy_by_conversation(
                conversation_id
            )

    def forget_registry(self, conversation_id: str) -> None:
        self._run_registry.forget(conversation_id)


class ConversationRegistryDisposal:
    """Clear conversation-scoped domain registries during disposal."""

    def __init__(
        self,
        run_ingress: RunIngressLedger,
        loop_registry: LoopRegistry,
        run_recovery: RunRecoveryLedger,
        cancellations: CancellationRegistry,
        settings: RuntimeSettings,
        contract: BuildContractService,
        dr: DeepResearchService,
        mcp: McpManager,
        spaces: SpaceService,
    ) -> None:
        self._run_ingress = run_ingress
        self._loop_registry = loop_registry
        self._run_recovery = run_recovery
        self._cancellations = cancellations
        self._settings = settings
        self._contract = contract
        self._dr = dr
        self._mcp = mcp
        self._spaces = spaces

    def clear(self, conversation_id: str) -> None:
        self._run_ingress.forget(conversation_id)
        self._loop_registry.forget(conversation_id)
        self._run_recovery.forget(conversation_id)
        self._cancellations.clear(conversation_id)
        self._settings._forget(conversation_id)
        self._contract.forget(conversation_id)
        self._dr.forget(conversation_id)
        self._mcp._forget(conversation_id)
        self._spaces.forget(conversation_id)


class ConversationContextDisposal:
    """Clear connection and driver context for a conversation during disposal."""

    def __init__(
        self,
        connections: ConnectionTracker,
        driver_contexts: DriverContextState,
        driver_preflight: DriverPreflight,
    ) -> None:
        self._connections = connections
        self._driver_contexts = driver_contexts
        self._driver_preflight = driver_preflight

    def clear(self, conversation_id: str) -> None:
        self._connections.clear_conversation(conversation_id)
        self._driver_contexts.discard(conversation_id)
        self._driver_preflight.discard_conversation(conversation_id)


class WorkspaceRestoreSession:
    """Acquire a sandbox session for workspace restore.

    ``exclude`` marks a session that just failed (F-28) — the registry may
    still return that dying object, and handing it back would retry a dead
    container as if it were fresh.
    """

    def __init__(
        self,
        run_resources: RunResourceRegistry,
        preview: PreviewService,
        loop_factory: BuildLoopFactory,
    ) -> None:
        self._run_resources = run_resources
        self._preview = preview
        self._loop_factory = loop_factory

    def _live_session(self, conversation_id: str) -> SandboxSession | None:
        executor = self._run_resources.executor(conversation_id)
        return getattr(executor, "_sandbox", None) if executor is not None else None

    async def acquire(
        self,
        conversation_id: str,
        *,
        exclude: SandboxSession | None = None,
    ) -> SandboxSession:
        from .workspace_service import WorkspaceRestoreStorageError

        def _usable(candidate: SandboxSession | None) -> SandboxSession | None:
            return None if candidate is None or candidate is exclude else candidate

        session = _usable(self._live_session(conversation_id))
        if session is None:
            cid8 = conversation_id.removeprefix("conv_")[:8]
            with contextlib.suppress(Exception):
                await self._preview.wake_for_preview(cid8, PREVIEW_PORT)
            session = _usable(self._live_session(conversation_id))
        if session is None:
            try:
                self._loop_factory.loop_for(conversation_id)
            except Exception as exc:  # noqa: BLE001 — mapped to a named storage error
                _LOG.error(
                    "could not create sandbox for restore of %s: %s", conversation_id, exc
                )
                raise WorkspaceRestoreStorageError(
                    f"could not create sandbox for restore: {exc}"
                ) from exc
            session = _usable(self._live_session(conversation_id))
        if session is None:
            _LOG.error("sandbox unavailable for restore of %s", conversation_id)
            raise WorkspaceRestoreStorageError("sandbox unavailable for restore")
        return session


class WorkspaceOwnership:
    """Own conversation disposal, resource teardown, and restore-session acquisition.

    Single entry point composed from the cohesive disposal helpers so
    :class:`WorkspaceCoordinator` stays a state-free compatibility delegate.

    ``restore_session`` is bound after construction (see
    :meth:`bind_restore_session`) because :class:`WorkspaceRestoreSession`
    depends on :class:`PreviewService` and :class:`BuildLoopFactory`, which are
    wired after the coordinator in the composition order.
    """

    def __init__(
        self,
        task_disposal: RunTaskDisposal,
        registry_disposal: ConversationRegistryDisposal,
        context_disposal: ConversationContextDisposal,
    ) -> None:
        self._task_disposal = task_disposal
        self._registry_disposal = registry_disposal
        self._context_disposal = context_disposal
        self._restore_session: WorkspaceRestoreSession | None = None

    def bind_restore_session(self, restore_session: WorkspaceRestoreSession) -> None:
        """Bind the restore-session acquirer after preview/loop_factory are wired.

        Called once from the composition root after :class:`PreviewService` and
        :class:`BuildLoopFactory` exist.  This is an explicit typed binding —
        not a runtime back-ref, not a dynamic lookup, not a bag.
        """
        self._restore_session = restore_session

    def active_task(self, conversation_id: str) -> asyncio.Task[object] | None:
        return self._task_disposal.active_task(conversation_id)

    def current_lifecycle_task_authority(
        self,
        conversation_id: str,
    ) -> tuple[str | None, str | None]:
        return self._task_disposal.current_lifecycle_task_authority(conversation_id)

    async def cancel_registered_task(self, conversation_id: str) -> None:
        await self._task_disposal.cancel_registered_task(conversation_id)

    async def dispose(self, conversation_id: str) -> None:
        """Dispose resources, then clear registries and context in order."""

        await self._task_disposal.dispose_resources(conversation_id)
        self._task_disposal.forget_registry(conversation_id)
        self._registry_disposal.clear(conversation_id)
        self._context_disposal.clear(conversation_id)

    async def restore_session(
        self,
        conversation_id: str,
        *,
        exclude: SandboxSession | None = None,
    ) -> SandboxSession:
        assert self._restore_session is not None, "restore session not yet bound"
        return await self._restore_session.acquire(conversation_id, exclude=exclude)
