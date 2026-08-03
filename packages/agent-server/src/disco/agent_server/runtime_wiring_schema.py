"""Static typed inventory for the runtime composition boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from disco.core import SkillStore
    from disco.core.llm import ConfigStore, DefaultLLMRouter, SecretStore
    from disco.core.store.sqlite import SqliteEventStore

    from .appkit_ejection import AppKitEjectionService
    from .artifact_manifest_shadow import ArtifactManifestShadow
    from .build_contract_service import BuildContractService
    from .build_kernel import DiscoKernel
    from .build_loop_assembler import BuildShadowLedger
    from .build_loop_factory import (
        BuildLoopFactory,
    )
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
    from .lifecycle_command_service import (
        LifecycleCommandService,
    )
    from .lifecycle_idle_sweep import LifecycleIdleSweeper
    from .lifecycle_ports import (
        LifecycleIdleSweepDeps,
    )
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
    from .run_supervisor import RunFinalizer, RunPersistenceSupervisor, RunSupervisor
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
    from .workspace_ownership import (
        WorkspaceOwnership,
    )
    from .workspace_service import WorkspaceCoordinator


class _RuntimeWiringSchema:
    """Names every owner without retaining an aggregate instance."""

    _store: SqliteEventStore
    _skill_store: SkillStore
    uploads: UploadStore
    _secret_store: SecretStore
    _config_store: ConfigStore
    sandbox: SandboxRuntimeService
    projects: ProjectRuntimeService
    _loop_registry: LoopRegistry
    _run_registry: RunRegistry
    _run_authorities: RunAuthorityLedger
    _run_ingress: RunIngressLedger
    _run_resources: RunResourceRegistry
    _run_recovery: RunRecoveryLedger
    _driver_contexts: DriverContextState
    _appkit_ejections: AppKitEjectionLedger
    _settings: RuntimeSettings
    drivers: DriverRuntime
    _driver_preflight: DriverPreflight
    _cancellations: CancellationRegistry
    mcp: McpManager
    _kernel_pin_store: KernelPinStore
    _connection_state: ConnectionState
    _lifecycle_idle: LifecycleIdleSweepDeps
    _idle_sweeper: LifecycleIdleSweeper
    _workspace_fence: WorkspaceFenceService
    titles: TitleService
    suggestions: SuggestionService
    share: ShareService
    _resume: ResumeService
    workspace: WorkspaceCoordinator
    _workspace_ownership: WorkspaceOwnership
    _persistence_notifier: PersistenceNotifier
    _contract: BuildContractService
    _build_platform: BuildPlatformRuntime
    _lifecycle: LifecycleManager
    _lifecycle_commands: LifecycleCommandService
    connections: ConnectionTracker
    _appkit_ejection: AppKitEjectionService
    _research_state: DeepResearchState
    _research_live_state: DeepResearchLiveState
    deep_research: DeepResearchService
    spaces: SpaceService
    _build_retrieval: BuildRetrievalCapabilities
    _build_shadows: BuildShadowLedger
    _artifact_manifest_shadow: ArtifactManifestShadow
    _loop_factory: BuildLoopFactory
    schedules: ScheduleService
    sessions: SessionsService
    live_sessions: LiveSessionDirectory
    preview: PreviewService
    sandbox_resources: SandboxResourceReconciler
    _run_supervisor: RunSupervisor
    run_controller: RunController
    _run_kills: RunKillService
    _control: ControlOps
    _disco_kernel: DiscoKernel
    _kernel_pins: KernelPinRegistry
    conversation_control: ConversationControlService
    _run_finalizer: RunFinalizer
    run_sweep: RunStrandedSweep
    _run_execution: RunPersistenceSupervisor
    _router_now: Callable[..., DefaultLLMRouter]
