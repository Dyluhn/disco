"""Concrete narrow ports for run supervision."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
)
from disco.core.store.sqlite import SqliteEventStore

from .build_contract_service import BuildContractService
from .build_kernel import BuildKernel, DiscoKernel
from .deep_research_service import DeepResearchService
from .driver_runtime import DriverPreflight
from .lifecycle import LifecycleManager
from .runtime_settings import RuntimeSettings
from .sandbox_runtime_service import SandboxRuntimeService
from .workspace_service import WorkspaceCoordinator

if TYPE_CHECKING:
    from .resume_service import ResumeService
    from .run_controller import RunController


class DeferredKernelSelector:
    """Typed one-time kernel binding used to break the pin/start cycle."""

    def __init__(self) -> None:
        self._kernel: BuildKernel | None = None

    def bind(self, kernel: BuildKernel) -> None:
        if self._kernel is not None:
            raise RuntimeError("kernel selector is already bound")
        self._kernel = kernel

    def select_kernel(self, conversation_id: str) -> BuildKernel:
        del conversation_id
        if self._kernel is None:
            raise RuntimeError("kernel selector is not bound")
        return self._kernel


class RunReentry:
    """Join run kick and resume without restoring a runtime-shaped owner."""

    def __init__(
        self,
        controller: RunController,
        resume: ResumeService,
    ) -> None:
        self._controller = controller
        self._resume = resume

    def kick(
        self,
        conversation_id: str,
        *,
        claimed_user_seq: int | None = None,
    ) -> None:
        self._controller.kick(
            conversation_id,
            claimed_user_seq=claimed_user_seq,
        )

    async def resume_conversation(self, conversation_id: str) -> dict[str, object]:
        return dict(await self._resume.resume_conversation(conversation_id))


class RunSurfaceSettings:
    """Expose only the surface decisions supervision needs."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def surface(self, conversation_id: str) -> str:
        return self._settings._surface_of(conversation_id)

    @staticmethod
    def is_build_surface(surface: str) -> bool:
        return surface in {"build", "agent"}

    def autonomous(self, conversation_id: str) -> bool:
        return self._settings._effective_autonomous(conversation_id)


class RunObservability:
    """Emit audit summaries and ambient persistence reminders."""

    def __init__(
        self,
        contract: BuildContractService,
        store: SqliteEventStore,
    ) -> None:
        self._contract = contract
        self._store = store

    def emit_audit(
        self,
        conversation_id: str,
        status: ConversationStatus,
    ) -> None:
        self._contract._emit_toolscope_audit_summary(conversation_id, status)

    async def emit_reminder(self, conversation_id: str, message: str) -> None:
        await self._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"Project persistence note: {message}\n"
                        "</system-reminder>"
                    ),
                ),
            ),
        )


class RunPreflight:
    """Combine the two independent first-use readiness checks."""

    def __init__(
        self,
        driver: DriverPreflight,
        sandbox: SandboxRuntimeService,
    ) -> None:
        self._driver = driver
        self._sandbox = sandbox

    async def driver_failure(self, conversation_id: str) -> str | None:
        return await self._driver.check(conversation_id)

    async def sandbox_failure(self, conversation_id: str) -> str | None:
        return await self._sandbox.preflight_failure()


class RunPersistence:
    """Delegate workspace persistence to its two existing owners."""

    def __init__(
        self,
        workspace: WorkspaceCoordinator,
        lifecycle: LifecycleManager,
    ) -> None:
        self._workspace = workspace
        self._lifecycle = lifecycle

    def workspace_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._workspace.lock(conversation_id)

    async def rehydrate(self, conversation_id: str) -> None:
        await self._lifecycle._maybe_rehydrate(conversation_id)

    async def rematerialize_uploads(self, conversation_id: str) -> None:
        await self._lifecycle._rematerialize_uploads(conversation_id)

    async def rematerialize_reference_packs(self, conversation_id: str) -> None:
        await self._lifecycle._rematerialize_reference_packs(conversation_id)

    async def snapshot(self, conversation_id: str, *, trigger: str) -> None:
        await self._lifecycle._maybe_snapshot(conversation_id, trigger=trigger)


class DeepResearchRun:
    """Narrow the Deep Research surface to its run entry point."""

    def __init__(self, deep_research: DeepResearchService) -> None:
        self._deep_research = deep_research

    async def run_deep_research(self, conversation_id: str) -> None:
        await self._deep_research._maybe_run_deep_research(conversation_id)


class DiscoKernelSelector:
    """The current single-kernel selection policy."""

    def __init__(self, disco: DiscoKernel) -> None:
        self._disco = disco

    def select_kernel(self, conversation_id: str) -> BuildKernel:
        _ = conversation_id
        return self._disco
