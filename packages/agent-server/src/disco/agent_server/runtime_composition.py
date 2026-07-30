"""Wiring-only construction for ``ConversationRuntime``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from disco.core import SkillStore
from disco.core.inspect import inspect_enabled
from disco.core.inspect import install as install_inspect
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    OperatingMode,
    SecretStore,
)
from disco.core.llm.config import RouterConfig
from disco.core.store.sqlite import SqliteEventStore
from disco.tools import SandboxService, SandboxSpec

from .appkit_ejection import AppKitEjectionService
from .artifact_manifest_shadow import ArtifactManifestShadow
from .build_composition_ports import (
    BuildExecutorAccess,
    BuildSurfacePolicy,
    WorkspaceRevisionCapture,
)
from .build_contract_service import BuildContractService
from .build_kernel import DiscoKernel
from .build_loop_assembler import BuildLoopAssembler, BuildShadowLedger
from .build_loop_components import LoopModeResolver
from .build_loop_factory import (
    BuildCapabilityBroker,
    BuildExecutorFactory,
    BuildLoopComposer,
    BuildLoopFactory,
    BuildSessionFactory,
    McpLoopConfigurator,
)
from .build_platform_runtime import AppKitEjectionLedger, BuildPlatformRuntime
from .build_retrieval import BuildRetrievalCapabilities
from .connection_tracker import ConnectionTracker, LifecycleSuspender
from .control_ops import ControlOps
from .conversation_control_service import ConversationControlService
from .deep_research_service import DeepResearchService
from .deep_research_state import DeepResearchLiveState, DeepResearchState
from .driver_context_state import DriverContextState
from .driver_runtime import DriverPreflight, DriverRuntime
from .driver_runtime_ports import (
    RuntimeDriverProbeSeams,
    RuntimeDriverPromptState,
    RuntimeDriverSelections,
)
from .lifecycle_command_service import compose_lifecycle_service
from .mcp_manager import McpManager
from .persistence_notifier import PersistenceNotifier
from .preview_service import PreviewService
from .project_runtime_service import ProjectRuntimeService
from .resume_service import ResumeService
from .run_completion import DeferredRunCompletion
from .run_controller import RunController
from .run_kill_service import RunKillService
from .run_registry import (
    CancellationRegistry,
    KernelPinRegistry,
    LoopRegistry,
    RunAuthorityLedger,
    RunIngressLedger,
    RunRecoveryLedger,
    RunRegistry,
    RunResourceRegistry,
)
from .run_stranded_sweep import RunStrandedSweep
from .run_supervision_ports import (
    DeepResearchRun,
    DiscoKernelSelector,
    RunObservability,
    RunPersistence,
    RunPreflight,
    RunSurfaceSettings,
)
from .run_supervisor import RunFinalizer, RunPersistenceSupervisor, RunSupervisor
from .runtime_settings import (
    RuntimeSettings,
    _RuntimeModelBindings,
    _RuntimeSettingsRouting,
)
from .sandbox_resource_reconciler import SandboxResourceReconciler
from .sandbox_runtime_service import SandboxRuntimeService
from .schedule_service import (
    RecurringScheduleControl,
    ScheduleService,
    WorkflowConversationSettings,
    WorkflowLoopFactory,
    WorkflowModelAccess,
    WorkflowProjectAccess,
    WorkflowRunControl,
)
from .sessions_service import SessionsService
from .share_service import ShareService
from .space_service import ConfiguredProjectRoot, SpaceService
from .suggestion_service import SuggestionService
from .title_service import TitleService
from .upload_store import UploadStore
from .workflow_run_service import WorkflowRunService
from .workspace_service import WorkspaceCoordinator

if TYPE_CHECKING:
    from .runtime import ConversationRuntime

_LOG = logging.getLogger(__name__)


def _config_store(
    config: RouterConfig | None,
    config_store: ConfigStore | None,
) -> ConfigStore:
    if config_store is not None:
        return config_store
    if config is None:
        return ConfigStore()
    result = ConfigStore(base_factory=lambda: config)
    if result.path.exists():
        _LOG.warning(
            "ConversationRuntime(config=...) is shadowed by %s; pass "
            "config_store= or point DISCO_CONFIG at the intended file",
            result.path,
        )
    return result


def _wire_foundation(
    rt: ConversationRuntime,
    store: SqliteEventStore,
    *,
    db_path: str,
    config: RouterConfig | None,
    config_store: ConfigStore | None,
    secret_store: SecretStore | None,
    router: DefaultLLMRouter | None,
    enable_thinking: bool,
    mode: OperatingMode,
    sandbox_service: SandboxService | None,
    sandbox_spec: SandboxSpec | None,
    skill_store: SkillStore | None,
) -> None:
    rt._store = store
    rt._skill_store = skill_store or SkillStore()
    rt._uploads = UploadStore(f"{db_path}.uploads" if db_path else "")
    rt._secret_store = secret_store or SecretStore()
    rt._config_store = _config_store(config, config_store)
    rt._sandbox = SandboxRuntimeService(
        rt._config_store,
        sandbox_service,
        sandbox_spec or SandboxSpec(),
    )
    rt._projects = ProjectRuntimeService(rt._config_store, store)
    if inspect_enabled():
        install_inspect()

    rt._loop_registry = LoopRegistry()
    rt._run_registry = RunRegistry()
    rt._run_authorities = RunAuthorityLedger()
    rt._run_ingress = RunIngressLedger()
    rt._run_resources = RunResourceRegistry()
    rt._run_recovery = RunRecoveryLedger()
    rt._driver_contexts = DriverContextState(timeout_s=5.0)
    rt._appkit_ejections = AppKitEjectionLedger()
    executors = BuildExecutorAccess(rt._run_resources)
    rt._settings = RuntimeSettings(
        db_path=db_path,
        store=store,
        config_store=rt._config_store,
        routing=_RuntimeSettingsRouting(rt._config_store, rt._secret_store, router),
        model_bindings=_RuntimeModelBindings(
            loops=rt._loop_registry,
            runs=rt._run_registry,
            contexts=rt._driver_contexts,
            resources=rt._run_resources,
        ),
        appkit_ejections=rt._appkit_ejections,
    )
    rt._drivers = DriverRuntime(
        config_store=rt._config_store,
        secret_store=rt._secret_store,
        skill_store=rt._skill_store,
        prompt_state=RuntimeDriverPromptState(executors),
        selections=RuntimeDriverSelections(rt._settings),
        probes=RuntimeDriverProbeSeams(),
        contexts=rt._driver_contexts,
        injected_router=router,
        enable_thinking=enable_thinking,
    )
    rt._driver_preflight = DriverPreflight(rt._drivers)
    rt._cancellations = CancellationRegistry()
    rt._mcp = McpManager(rt._config_store, rt._secret_store, store)
    rt._title_service = TitleService(store, rt._router_now)
    rt._suggestion_service = SuggestionService(
        rt._router_now,
        lambda: rt._projects.current_project_store().root or "",
    )


def _wire_domains(
    rt: ConversationRuntime,
    *,
    research_providers: dict[str, Any] | None,
) -> None:
    executors = BuildExecutorAccess(rt._run_resources)
    rt._share = ShareService(
        rt._store,
        rt._settings._surface_of,
        rt._projects.current_project_store,
    )
    rt._resume = ResumeService(rt)
    rt._workspace = WorkspaceCoordinator(rt)
    rt._persistence_notifier = PersistenceNotifier(rt._store)
    rt._contract = BuildContractService(rt._store, rt._settings, executors)
    rt._build_platform = BuildPlatformRuntime(
        store=rt._store,
        workspace=rt._workspace,
        surfaces=BuildSurfacePolicy(rt._settings),
        rollback=executors,
        ejections=rt._appkit_ejections,
    )
    rt._lifecycle, rt._lifecycle_commands = compose_lifecycle_service(rt)
    rt._connections = ConnectionTracker(LifecycleSuspender(rt._lifecycle))
    rt._appkit_ejection = AppKitEjectionService(
        event_store=rt._store,
        workspace=rt._workspace,
        revision_capture=WorkspaceRevisionCapture(rt._lifecycle),
        project_stores=rt._projects,
        sandboxes=executors,
        transitions=rt._build_platform,
    )
    rt._research_state = DeepResearchState()
    rt._research_live_state = DeepResearchLiveState()
    rt._dr = DeepResearchService(
        rt,
        state=rt._research_state,
        live_state=rt._research_live_state,
        injected_providers=research_providers,
    )
    rt._spaces = SpaceService(ConfiguredProjectRoot(rt._config_store), rt._dr)
    rt._build_retrieval = BuildRetrievalCapabilities(rt._dr, rt._mcp)
    rt._build_shadows = BuildShadowLedger()
    rt._artifact_manifest_shadow = ArtifactManifestShadow(
        rt._store,
        rt._run_resources,
    )


def _wire_loops(rt: ConversationRuntime, *, mode: OperatingMode) -> None:
    sessions = BuildSessionFactory(
        rt._settings,
        rt._sandbox,
        rt._lifecycle,
        rt._run_resources,
    )
    executors = BuildExecutorFactory(
        rt._settings,
        rt._workspace,
        rt._projects,
        rt._contract,
        rt._loop_registry,
        rt._store,
    )
    assembler = BuildLoopAssembler(
        rt._settings,
        rt._workspace,
        rt._store,
        rt._contract,
        rt._build_platform,
        rt._build_shadows,
    )
    composer = BuildLoopComposer(
        sessions,
        LoopModeResolver(rt._settings, rt._drivers, rt._contract),
        BuildCapabilityBroker(rt._build_retrieval),
        executors,
        rt._mcp,
        McpLoopConfigurator(rt._store, rt._appkit_ejection),
        rt._run_resources,
        assembler,
    )
    rt._loop_factory = BuildLoopFactory(
        rt._settings,
        rt._drivers,
        rt._workspace,
        rt._store,
        rt._dr,
        composer,
        rt._loop_registry,
        rt._run_resources,
        rt._driver_contexts,
        mode,
    )
    workflow_runs = WorkflowRunService(
        rt._store,
        lifecycle_commands=rt._lifecycle_commands,
        workspace=rt._workspace,
        project_access=cast(WorkflowProjectAccess, rt),
        settings=cast(WorkflowConversationSettings, rt),
        model_access=cast(WorkflowModelAccess, rt),
        loop_factory=cast(WorkflowLoopFactory, rt),
        run_control=cast(WorkflowRunControl, rt),
    )
    rt._schedule = ScheduleService(
        rt._store,
        workflow_runs=workflow_runs,
        project_access=cast(WorkflowProjectAccess, rt),
        recurring_control=cast(RecurringScheduleControl, rt),
    )
    rt._sessions = SessionsService(rt)
    rt._preview = PreviewService(rt)


def _wire_runs(rt: ConversationRuntime) -> None:
    rt._sandbox_resources = SandboxResourceReconciler(
        rt._sandbox,
        rt._run_registry,
        rt._loop_registry,
        rt._run_resources,
        rt._run_recovery,
        rt._store,
        rt._lifecycle,
    )
    completion = DeferredRunCompletion()
    rt._run_supervisor = RunSupervisor(
        rt._run_registry,
        rt._run_authorities,
        rt._run_ingress,
        rt._run_resources,
        rt._workspace,
        completion,
    )
    rt._run_controller = RunController(
        rt._run_registry,
        rt._run_supervisor,
        rt._title_service,
        rt._sandbox_resources,
        rt._build_platform,
        rt._drivers,
        rt._loop_factory,
        rt._resume,
    )
    rt._run_kills = RunKillService(
        rt._store,
        rt._workspace,
        rt._lifecycle_commands,
        rt._run_registry,
        rt._loop_registry,
        rt._run_resources,
    )
    rt._control = ControlOps(
        rt._store,
        rt._workspace,
        rt._loop_registry,
        rt._cancellations,
        rt._run_controller,
        rt._run_kills,
    )
    rt._disco_kernel = DiscoKernel(
        rt._store,
        rt._workspace,
        rt._lifecycle_commands,
        rt._run_controller,
        rt._control,
        rt._loop_factory,
        rt._resume,
        rt._run_registry,
    )
    rt._kernel_pins = KernelPinRegistry(DiscoKernelSelector(rt._disco_kernel))
    rt._disco_kernel._bind_pins(rt._kernel_pins)
    rt._conversation_control = ConversationControlService(
        rt._contract,
        rt._kernel_pins,
        rt._run_registry,
        rt._cancellations,
        rt._run_controller,
        rt._control,
        rt._resume,
    )
    run_policy = RunSurfaceSettings(rt._settings)
    observability = RunObservability(rt._contract, rt._store)
    rt._run_finalizer = RunFinalizer(
        rt._run_registry,
        rt._run_ingress,
        rt._run_recovery,
        rt._kernel_pins,
        rt._store,
        rt._workspace,
        rt._lifecycle_commands,
        rt._run_controller,
        run_policy,
        observability,
    )
    completion.bind(rt._run_finalizer)
    rt._run_sweep = RunStrandedSweep(
        rt._run_registry,
        rt._loop_registry,
        rt._store,
        rt._run_finalizer,
    )
    rt._run_execution = RunPersistenceSupervisor(
        rt._run_registry,
        rt._run_authorities,
        rt._store,
        rt._lifecycle_commands,
        run_policy,
        observability,
        RunPreflight(rt._driver_preflight, rt._sandbox),
        RunPersistence(rt._workspace, rt._lifecycle),
        DeepResearchRun(rt._dr),
    )


def wire_runtime(
    rt: ConversationRuntime,
    store: SqliteEventStore,
    *,
    db_path: str,
    config: RouterConfig | None,
    config_store: ConfigStore | None,
    secret_store: SecretStore | None,
    router: DefaultLLMRouter | None,
    enable_thinking: bool,
    mode: OperatingMode,
    research_providers: dict[str, Any] | None,
    sandbox_service: SandboxService | None,
    sandbox_spec: SandboxSpec | None,
    skill_store: SkillStore | None,
) -> None:
    """Assemble named owners in dependency order; perform no domain policy."""

    _wire_foundation(
        rt,
        store,
        db_path=db_path,
        config=config,
        config_store=config_store,
        secret_store=secret_store,
        router=router,
        enable_thinking=enable_thinking,
        mode=mode,
        sandbox_service=sandbox_service,
        sandbox_spec=sandbox_spec,
        skill_store=skill_store,
    )
    _wire_domains(rt, research_providers=research_providers)
    _wire_loops(rt, mode=mode)
    _wire_runs(rt)
