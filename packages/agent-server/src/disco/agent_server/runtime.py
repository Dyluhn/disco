"""Agent-server composition facade.

``ConversationRuntime`` wires named owners and retains only the narrow ingress
surface shared by transports. Domain policy and mutable collections live on
their owning services.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import os
from typing import TYPE_CHECKING, Any

from disco.core import (
    ConversationStatus,
    MessageEvent,
    PlanEvent,
    SkillStore,
    StatusEvent,
)
from disco.core.appkit import BuildBrief
from disco.core.env import disco_env
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    ModelRole,
    OperatingMode,
    SecretStore,
)
from disco.core.llm.config import RouterConfig
from disco.core.llm.secret_refs import resolve_provider_secret
from disco.core.loop import AgentLoop, RouterAgent
from disco.core.store.sqlite import SqliteEventStore
from disco.core.verification import VerificationRequirementsDirective
from disco.core.workflow import WorkflowRun
from disco.tools import SandboxService, SandboxSpec
from disco.tools.builtin.verify_appkit_app import (
    APPKIT_LIVE_PREVIEW_NAME as APPKIT_LIVE_PREVIEW_NAME,
)
from disco.tools.projects import ProjectStore

from .build_contract_service import (
    _AUDIT_KIND_TERMS,
)
from .build_contract_service import (
    host_verify_canary_enabled as host_verify_canary_enabled,
)
from .build_loop_components import (
    _apply_mcp_scope as _apply_mcp_scope,
)
from .build_loop_components import (
    _mcp_base_risk_by_tool as _mcp_base_risk_by_tool,
)
from .build_loop_components import (
    _MCPToolWrapper as _MCPToolWrapper,
)
from .build_loop_components import (
    _sync_appkit_live_preview as _sync_appkit_live_preview,
)
from .build_loop_components import (
    toolscope_audit_enabled as toolscope_audit_enabled,
)
from .build_loop_components import (
    workflow_router_enabled as workflow_router_enabled,
)
from .driver_context import ResolvedDriverContext
from .runtime_composition import wire_runtime
from .runtime_model_probe import (
    _LIVE_MODEL_PROBE_CACHE as _LIVE_MODEL_PROBE_CACHE,
)
from .runtime_model_probe import (
    _LIVE_MODEL_PROBE_INFLIGHT as _LIVE_MODEL_PROBE_INFLIGHT,
)
from .runtime_model_probe import (
    _MODELS_CTX_CACHE as _MODELS_CTX_CACHE,
)
from .runtime_model_probe import (
    _PROBE_TTL_S as _PROBE_TTL_S,
)
from .runtime_model_probe import _do_live_model_probe
from .runtime_model_probe import _model_label as _model_label
from .runtime_model_probe import _probe_live_model as _owned_probe_live_model
from .sandbox_runtime_service import (
    build_sandbox_service as build_sandbox_service,
)
from .sandbox_runtime_service import (
    effective_local_runtime as effective_local_runtime,
)
from .workspace_service import (
    WorkspaceRestoreConflict as WorkspaceRestoreConflict,
)
from .workspace_service import (
    WorkspaceRestoreStorageError as WorkspaceRestoreStorageError,
)
from .workspace_service import (
    WorkspaceVersionNotFound as WorkspaceVersionNotFound,
)

if TYPE_CHECKING:
    from .appkit_ejection import AppKitEjectionService
    from .artifact_manifest_shadow import ArtifactManifestShadow
    from .build_contract_service import BuildContractService
    from .build_kernel import DiscoKernel
    from .build_loop_assembler import BuildShadowLedger
    from .build_loop_factory import BuildLoopFactory
    from .build_platform_runtime import AppKitEjectionLedger, BuildPlatformRuntime
    from .build_retrieval import BuildRetrievalCapabilities
    from .connection_tracker import ConnectionState, ConnectionTracker
    from .control_ops import ControlOps
    from .conversation_control_service import ConversationControlService
    from .deep_research_service import DeepResearchService
    from .deep_research_state import DeepResearchLiveState, DeepResearchState
    from .driver_context_state import DriverContextState
    from .driver_runtime import DriverPreflight, DriverRuntime
    from .lifecycle import LifecycleManager
    from .lifecycle_command_service import LifecycleCommandService
    from .lifecycle_idle_sweep import LifecycleIdleSweeper
    from .lifecycle_ports import LifecycleIdleSweepDeps
    from .live_session_directory import LiveSessionDirectory
    from .mcp_manager import McpManager
    from .persistence_notifier import PersistenceNotifier
    from .preview_service import PreviewService
    from .project_runtime_service import ProjectRuntimeService
    from .resume_service import ResumeService
    from .run_controller import RunController
    from .run_kill_service import RunKillService
    from .run_registry import (
        CancellationRegistry,
        KernelPinRegistry,
        KernelPinStore,
        LoopRegistry,
        RunAuthorityLedger,
        RunIngressLedger,
        RunRecoveryLedger,
        RunRegistry,
        RunResourceRegistry,
    )
    from .run_stranded_sweep import RunStrandedSweep
    from .run_supervisor import (
        RunFinalizer,
        RunPersistenceSupervisor,
        RunSupervisor,
    )
    from .runtime_settings import RuntimeSettings
    from .sandbox_resource_reconciler import SandboxResourceReconciler
    from .sandbox_runtime_service import SandboxRuntimeService
    from .schedule_service import ScheduleService
    from .sessions_service import SessionsService
    from .share_service import ShareService
    from .space_service import SpaceService
    from .suggestion_service import SuggestionService
    from .title_service import TitleService
    from .upload_store import UploadStore
    from .workspace_fence import WorkspaceFenceService
    from .workspace_ownership import WorkspaceOwnership
    from .workspace_service import WorkspaceCoordinator


def _has_unfinished_plan(events: list[Any]) -> bool:
    has_plan = any(isinstance(event, PlanEvent) for event in events)
    has_finished = any(
        isinstance(event, StatusEvent) and event.status is ConversationStatus.FINISHED
        for event in events
    )
    return has_plan and not has_finished


def _probe_live_model(
    base_url: str | None,
    api_key: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility seam for existing imports and probe monkeypatches."""

    return _owned_probe_live_model(
        base_url,
        api_key,
        model_id,
        probe_body=_do_live_model_probe,
    )


def _release_process_memory() -> None:
    """Best-effort release of freed glibc arenas after memory-heavy runs."""

    gc.collect()
    if (os.environ.get("DISCO_DR_MALLOC_TRIM") or "1").strip().lower() in {
        "0",
        "off",
        "false",
        "none",
    }:
        return
    try:
        libc = ctypes.CDLL(
            ctypes.util.find_library("c") or "libc.so.6",
            use_errno=True,
        )
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except (OSError, AttributeError, ValueError):
        pass


def _default_in_catalog(
    default: str | None,
    models: list[dict[str, object]],
) -> str | None:
    return default if any(model["id"] == default for model in models) else None


class _RuntimeWiringSchema:
    """Static owner inventory for the composition root; stores no values."""

    _store: SqliteEventStore
    _skill_store: SkillStore
    _uploads: UploadStore
    _secret_store: SecretStore
    _config_store: ConfigStore
    _sandbox: SandboxRuntimeService
    _projects: ProjectRuntimeService
    _loop_registry: LoopRegistry
    _run_registry: RunRegistry
    _run_authorities: RunAuthorityLedger
    _run_ingress: RunIngressLedger
    _run_resources: RunResourceRegistry
    _run_recovery: RunRecoveryLedger
    _driver_contexts: DriverContextState
    _appkit_ejections: AppKitEjectionLedger
    _settings: RuntimeSettings
    _drivers: DriverRuntime
    _driver_preflight: DriverPreflight
    _cancellations: CancellationRegistry
    _mcp: McpManager
    _kernel_pin_store: KernelPinStore
    _connection_state: ConnectionState
    _lifecycle_idle: LifecycleIdleSweepDeps
    _idle_sweeper: LifecycleIdleSweeper
    _workspace_fence: WorkspaceFenceService
    _title_service: TitleService
    _suggestion_service: SuggestionService
    _share: ShareService
    _resume: ResumeService
    _workspace: WorkspaceCoordinator
    _workspace_ownership: WorkspaceOwnership
    _persistence_notifier: PersistenceNotifier
    _contract: BuildContractService
    _build_platform: BuildPlatformRuntime
    _lifecycle: LifecycleManager
    _lifecycle_commands: LifecycleCommandService
    _connections: ConnectionTracker
    _appkit_ejection: AppKitEjectionService
    _research_state: DeepResearchState
    _research_live_state: DeepResearchLiveState
    _dr: DeepResearchService
    _spaces: SpaceService
    _build_retrieval: BuildRetrievalCapabilities
    _build_shadows: BuildShadowLedger
    _artifact_manifest_shadow: ArtifactManifestShadow
    _loop_factory: BuildLoopFactory
    _schedule: ScheduleService
    _sessions: SessionsService
    _live_sessions: LiveSessionDirectory
    _preview: PreviewService
    _sandbox_resources: SandboxResourceReconciler
    _run_supervisor: RunSupervisor
    _run_controller: RunController
    _run_kills: RunKillService
    _control: ControlOps
    _disco_kernel: DiscoKernel
    _kernel_pins: KernelPinRegistry
    _conversation_control: ConversationControlService
    _run_finalizer: RunFinalizer
    _run_sweep: RunStrandedSweep
    _run_execution: RunPersistenceSupervisor


class ConversationRuntime(_RuntimeWiringSchema):
    """Wiring-only root plus the transport-stable conversation ingress facade."""

    _AUDIT_KIND_TERMS = _AUDIT_KIND_TERMS

    def __init__(
        self,
        store: SqliteEventStore,
        *,
        config: RouterConfig | None = None,
        config_store: ConfigStore | None = None,
        secret_store: SecretStore | None = None,
        router: DefaultLLMRouter | None = None,
        enable_thinking: bool = False,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
        research_providers: dict[str, Any] | None = None,
        sandbox_service: SandboxService | None = None,
        sandbox_spec: SandboxSpec | None = None,
        skill_store: SkillStore | None = None,
    ) -> None:
        db_path = disco_env("DB", "") or getattr(store, "db_path", "")
        wire_runtime(
            self,
            store,
            db_path=db_path,
            config=config,
            config_store=config_store,
            secret_store=secret_store,
            router=router,
            enable_thinking=enable_thinking,
            mode=mode,
            research_providers=research_providers,
            sandbox_service=sandbox_service,
            sandbox_spec=sandbox_spec,
            skill_store=skill_store,
        )

    def _resolve_secret(self, name: str | None) -> str | None:
        return resolve_provider_secret(name, self._secret_store)

    def _origin_approved(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
    ) -> bool:
        return self._config_store.origin_approved(
            url,
            purpose,
            secret_ref,
            secret_store=self._secret_store,
        )

    def _router_now(
        self,
        pick: str | None = None,
        *,
        enable_thinking: bool | None = None,
        surface: str | None = None,
        autonomous: bool = False,
        conversation_id: str | None = None,
        appkit_mode: bool = False,
    ) -> DefaultLLMRouter:
        return self._drivers.router(
            pick,
            enable_thinking=enable_thinking,
            surface=surface,
            autonomous=autonomous,
            conversation_id=conversation_id,
            appkit_mode=appkit_mode,
        )

    def _surface_of(self, conversation_id: str) -> str:
        return self._settings._surface_of(conversation_id)

    def _effective_autonomous(self, conversation_id: str) -> bool:
        return self._settings._effective_autonomous(conversation_id)

    def _sandbox_service_now(self) -> SandboxService:
        return self._sandbox._sandbox_service_now()

    def _build_sandbox_spec(
        self,
        *,
        surface: str = "build",
        mcp_egress_hosts: frozenset[str] | None = None,
    ) -> SandboxSpec:
        return self._sandbox._build_sandbox_spec(
            surface=surface,
            mcp_egress_hosts=mcp_egress_hosts,
        )

    async def _resolve_driver_context(
        self,
        conversation_id: str,
    ) -> ResolvedDriverContext:
        return await self._drivers.resolve_context(conversation_id)

    async def _preflight_driver(
        self,
        conversation_id: str | None,
        *,
        override: str | None = None,
        role: ModelRole = ModelRole.AGENT_DRIVER,
    ) -> str | None:
        return await self._driver_preflight.check(
            conversation_id,
            override=override,
            role=role,
        )

    def _project_store_now(self) -> ProjectStore:
        return self._projects.current_project_store()

    def _loop_for(
        self,
        conversation_id: str,
        *,
        driver_context_window: int | None = None,
    ) -> AgentLoop:
        return self._loop_factory.loop_for(
            conversation_id,
            driver_context_window=driver_context_window,
        )

    def _compose_build_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
        sealed_workflow_run: WorkflowRun | None = None,
        sealed_workflow_instance_id: str | None = None,
    ) -> AgentLoop:
        return self._loop_factory.compose_build_loop(
            conversation_id,
            router,
            agent,
            driver_context_window=driver_context_window,
            sealed_workflow_run=sealed_workflow_run,
            sealed_workflow_instance_id=sealed_workflow_instance_id,
        )

    async def _run_with_persistence(
        self,
        conversation_id: str,
        loop: AgentLoop,
    ) -> Any:
        return await self._run_execution.run(conversation_id, loop)

    async def _maybe_snapshot(
        self,
        conversation_id: str,
        *,
        trigger: str = "turn",
    ) -> None:
        await self._lifecycle._maybe_snapshot(conversation_id, trigger=trigger)

    async def _suspend(self, conversation_id: str) -> None:
        await self._lifecycle._suspend(conversation_id)

    async def _teardown_sandbox(self, conversation_id: str) -> None:
        await self._lifecycle._teardown_sandbox(conversation_id)

    def start(self, conversation_id: str) -> None:
        self._conversation_control.start(conversation_id)

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
        return await self._conversation_control.send_user_turn(
            conversation_id,
            text,
            context=context,
            build_brief=build_brief,
            verification_requirements=verification_requirements,
            steer=steer,
        )

    async def confirm(self, conversation_id: str) -> None:
        await self._conversation_control.confirm(conversation_id)

    async def reject(
        self,
        conversation_id: str,
        reason: str = "rejected by user",
    ) -> None:
        await self._conversation_control.reject(conversation_id, reason)

    async def approve_plan(self, conversation_id: str) -> None:
        await self._conversation_control.approve_plan(conversation_id)

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        await self._conversation_control.request_plan(conversation_id, text)

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        await self._conversation_control.pick_alternative(conversation_id, option_id)

    async def pause(self, conversation_id: str) -> None:
        await self._conversation_control.pause(conversation_id)

    async def cancel(self, conversation_id: str) -> None:
        await self._conversation_control.cancel(conversation_id)

    async def resume(self, conversation_id: str) -> None:
        await self._conversation_control.resume(conversation_id)

    async def kill(self, conversation_id: str) -> None:
        await self._conversation_control.kill(conversation_id)

    async def aclose(self) -> None:
        await self._run_supervisor.cancel_runs()
        await self._driver_preflight.aclose()
        await self._run_supervisor.close_resources()
