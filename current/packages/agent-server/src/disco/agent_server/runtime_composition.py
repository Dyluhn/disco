"""Wiring-only construction for ``ConversationRuntime``."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from disco.core import SkillStore
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    OperatingMode,
    SecretStore,
)
from disco.core.llm.config import RouterConfig
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import WorkflowReadiness
from disco.tools import SandboxService, SandboxSpec

from . import lifecycle as lifecycle_module
from ._runtime_foundation import _wire_foundation
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
from .build_platform_runtime import BuildPlatformRuntime
from .build_retrieval import BuildRetrievalCapabilities
from .connection_tracker import ConnectionTracker, LifecycleSuspender
from .control_ops import ControlOps
from .conversation_control_service import ConversationControlService
from .deep_research_provider import DeepResearchProvider
from .deep_research_service import DeepResearchService
from .deep_research_state import DeepResearchLiveState, DeepResearchState
from .lifecycle import GateReaper, LifecycleManager, OrphanReconciler, Rehydration
from .lifecycle_command_service import (
    LifecycleCommandService,
    WorkspaceLifecycleTerminalEffects,
)
from .lifecycle_idle_sweep import LifecycleIdleSweeper
from .lifecycle_ports import (
    LifecycleConnections,
    LifecycleKernelPins,
    LifecyclePersistenceNotifier,
    LifecyclePersistenceOwner,
    LifecycleRunState,
    LifecycleSandboxAccess,
    LifecycleStoreAccess,
    LifecycleUploads,
)
from .live_session_directory import LiveSessionDirectory
from .persistence_notifier import PersistenceNotifier
from .preview_service import PreviewService
from .resume_ports import (
    PinnedRunStart,
    ResumeEnvironmentProbe,
    ResumeRunState,
    ResumeStoreAccess,
    ResumeSurfaceSettings,
    ResumeWorkspaceFence,
)
from .resume_service import ResumeService
from .run_completion import DeferredRunCompletion
from .run_controller import RunController
from .run_kill_service import RunKillService
from .run_lifecycle_service import RunLifecycleService
from .run_registry import (
    KernelPinRegistry,
)
from .run_stranded_sweep import RunStrandedSweep
from .run_supervision_ports import (
    DeepResearchRun,
    DiscoKernelSelector,
    RunObservability,
    RunPersistence,
    RunPreflight,
    RunReentry,
    RunSurfaceSettings,
)
from .run_supervisor import RunFinalizer, RunPersistenceSupervisor, RunSupervisor
from .runtime_wiring_schema import _RuntimeWiringSchema
from .sandbox_resource_reconciler import SandboxResourceReconciler
from .schedule_service import ScheduleService
from .sessions_service import SessionsService
from .share_service import ShareService
from .space_service import ConfiguredProjectRoot, SpaceService
from .workflow_invocation import WorkflowInvocationPreparation
from .workflow_run_service import WorkflowRunService
from .workflow_runtime_ports import (
    RecurringScheduleControlAdapter,
    WorkflowConversationSettingsAdapter,
    WorkflowLoopFactoryAdapter,
    WorkflowModelAccessAdapter,
    WorkflowProjectAccessAdapter,
    WorkflowRunControlAdapter,
)
from .workflow_surface import _compiled_surface_payload, _digest_payload, _surface_environment
from .workspace_ownership import (
    ConversationContextDisposal,
    ConversationRegistryDisposal,
    RunTaskDisposal,
    WorkspaceOwnership,
    WorkspaceRestoreSession,
)
from .workspace_persistence import WorkspacePersistence
from .workspace_service import WorkspaceCoordinator

_LOG = logging.getLogger(__name__)




def _wire_lifecycle(rt: _RuntimeWiringSchema) -> None:
    executors = BuildExecutorAccess(rt._run_resources)
    rt.share = ShareService(
        rt._store,
        rt.settings._surface_of,
        rt.projects.current_project_store,
    )
    rt._persistence_notifier = PersistenceNotifier(rt._store)
    rt.contract = BuildContractService(rt._store, rt.settings, executors)
    rt._artifact_manifest_shadow = ArtifactManifestShadow(
        rt._store,
        rt._run_resources,
    )
    persistence = WorkspacePersistence(
        rt._store,
        rt._run_resources,
        rt._workspace_fence,
        rt.projects,
        rt._artifact_manifest_shadow,
        rt._persistence_notifier,
        rt.run_registry,
    )
    persistence_owner = LifecyclePersistenceOwner(persistence)
    rt._lifecycle_commands = LifecycleCommandService(
        store=rt._store,
        fence=rt._workspace_fence,
        terminal_effects=WorkspaceLifecycleTerminalEffects(
            persistence,
            snapshot_provider=lambda: lifecycle_module.snapshot_workspace,
        ),
    )
    lifecycle_store = LifecycleStoreAccess(rt._store)
    lifecycle_runs = LifecycleRunState(
        rt.run_registry,
        rt._run_resources,
        rt._loop_registry,
    )
    lifecycle_connections = LifecycleConnections(
        rt._connection_state,
        rt._preview_capture_ownership,
    )
    lifecycle_sandbox = LifecycleSandboxAccess(rt.sandbox, rt.projects)
    rt.lifecycle = LifecycleManager(
        persistence_owner,
        GateReaper(
            lifecycle_store,
            lifecycle_runs,
            lifecycle_connections,
            rt._lifecycle_commands,
            LifecycleKernelPins(rt._kernel_pin_store, rt.run_registry),
        ),
        OrphanReconciler(
            lifecycle_store,
            lifecycle_sandbox,
            rt._lifecycle_commands,
            persistence_owner,
        ),
        Rehydration(
            lifecycle_sandbox,
            lifecycle_runs,
            LifecyclePersistenceNotifier(rt._persistence_notifier),
            LifecycleUploads(rt.uploads),
        ),
        lifecycle_store,
        lifecycle_runs,
        lifecycle_connections,
        lifecycle_sandbox,
        rt._lifecycle_idle,
        rt._workspace_fence,
    )
    rt.connections = ConnectionTracker(
        LifecycleSuspender(rt.lifecycle),
        state=rt._connection_state,
        preview_capture_ownership=rt._preview_capture_ownership,
    )
    rt._build_platform = BuildPlatformRuntime(
        store=rt._store,
        workspace=rt._workspace_fence,
        surfaces=BuildSurfacePolicy(rt.settings),
        rollback=executors,
        ejections=rt._appkit_ejections,
    )


def _wire_research(
    rt: _RuntimeWiringSchema,
    *,
    research_providers: dict[str, Any] | None,
) -> None:
    rt._research_state = DeepResearchState()
    rt._research_live_state = DeepResearchLiveState()
    research_provider = DeepResearchProvider(
        rt._config_store,
        rt._secret_store,
        rt.mcp,
        injected_providers=research_providers,
    )
    rt.spaces = SpaceService(
        ConfiguredProjectRoot(rt._config_store),
        research_provider,
    )
    rt.deep_research = DeepResearchService(
        store=rt._store,
        lifecycle_commands=rt._lifecycle_commands,
        drivers=rt.drivers,
        settings=rt.settings,
        preflight=rt._driver_preflight,
        spaces=rt.spaces,
        cancellations=rt._cancellations,
        provider=research_provider,
        state=rt._research_state,
        live_state=rt._research_live_state,
    )


def _wire_workspace(rt: _RuntimeWiringSchema) -> None:
    executors = BuildExecutorAccess(rt._run_resources)
    rt._workspace_ownership = WorkspaceOwnership(
        RunTaskDisposal(
            rt._kernel_pin_store,
            rt.run_registry,
            rt._run_authorities,
            rt._run_resources,
            rt.sandbox,
        ),
        ConversationRegistryDisposal(
            rt._run_ingress,
            rt._loop_registry,
            rt._run_recovery,
            rt._cancellations,
            rt.settings,
            rt.contract,
            rt.deep_research,
            rt.mcp,
            rt.spaces,
        ),
        ConversationContextDisposal(
            rt.connections,
            rt._driver_contexts,
            rt._driver_preflight,
        ),
    )
    rt.workspace = WorkspaceCoordinator(
        rt._store,
        rt.settings,
        rt.projects,
        rt._lifecycle_commands,
        rt.lifecycle,
        rt._build_platform,
        rt._workspace_ownership,
        rt._workspace_fence,
    )
    rt._appkit_ejection = AppKitEjectionService(
        event_store=rt._store,
        workspace=rt.workspace,
        revision_capture=WorkspaceRevisionCapture(rt.lifecycle),
        project_stores=rt.projects,
        sandboxes=executors,
        transitions=rt._build_platform,
    )
    rt._build_retrieval = BuildRetrievalCapabilities(rt.deep_research, rt.mcp)
    rt._build_shadows = BuildShadowLedger()


def _wire_domains(
    rt: _RuntimeWiringSchema,
    *,
    research_providers: dict[str, Any] | None,
) -> None:
    _wire_lifecycle(rt)
    _wire_research(rt, research_providers=research_providers)
    _wire_workspace(rt)


def _wire_loops(rt: _RuntimeWiringSchema, *, mode: OperatingMode) -> None:
    sessions = BuildSessionFactory(
        rt.settings,
        rt.sandbox,
        rt.lifecycle,
        rt._run_resources,
    )
    executors = BuildExecutorFactory(
        rt.settings,
        rt.workspace,
        rt.projects,
        rt.contract,
        rt._loop_registry,
        rt._store,
        reference_packs=rt.reference_packs,
        workflow_surface_digest=lambda instance: _workflow_surface_digest(rt, instance),
    )
    assembler = BuildLoopAssembler(
        rt.settings,
        rt.workspace,
        rt._store,
        rt.contract,
        rt._build_platform,
        rt._build_shadows,
    )
    composer = BuildLoopComposer(
        sessions,
        LoopModeResolver(rt.settings, rt.drivers, rt.contract),
        BuildCapabilityBroker(rt._build_retrieval),
        executors,
        rt.mcp,
        McpLoopConfigurator(rt._store, rt._appkit_ejection),
        rt._run_resources,
        assembler,
    )
    rt._loop_factory = BuildLoopFactory(
        rt.settings,
        rt.drivers,
        rt.workspace,
        rt._store,
        rt.deep_research,
        composer,
        rt._loop_registry,
        rt._run_resources,
        rt._driver_contexts,
        mode,
    )
    rt.live_sessions = LiveSessionDirectory(rt._run_resources, rt._store)
    rt.preview = PreviewService(
        rt.live_sessions,
        rt._store,
        rt._config_store,
        rt.settings,
        rt.connections,
        rt.projects,
        rt.lifecycle,
        rt._loop_factory,
        rt.workspace,
    )
    rt.sessions = SessionsService(
        rt._run_resources,
        rt.sandbox,
        rt.settings,
        rt.mcp,
        rt.lifecycle,
        rt.connections,
        rt.live_sessions,
    )
    rt._workspace_ownership.bind_restore_session(
        WorkspaceRestoreSession(
            rt._run_resources,
            rt.preview,
            rt._loop_factory,
        )
    )


def _workflow_surface_digest(rt: _RuntimeWiringSchema, instance: Any) -> str:
    """Use the same compiled surface authority as workflow review/approval.

    Kept as a runtime-composition callback so the loop factory remains unaware
    of HTTP routes while Agent readiness still detects MCP/skill/tool drift.
    """
    env = _surface_environment(cast(Any, rt))
    return _digest_payload(_compiled_surface_payload(instance, env))


def _wire_run_control(rt: _RuntimeWiringSchema) -> DeferredRunCompletion:
    rt.sandbox_resources = SandboxResourceReconciler(
        rt.sandbox,
        rt.run_registry,
        rt._loop_registry,
        rt._run_resources,
        rt._run_recovery,
        rt._store,
        rt.lifecycle,
    )
    completion = DeferredRunCompletion()
    rt._run_supervisor = RunSupervisor(
        rt.run_registry,
        rt._run_authorities,
        rt._run_ingress,
        rt._run_resources,
        rt.workspace,
        completion,
    )
    rt.run_controller = RunController(
        rt.run_registry,
        rt._run_supervisor,
        rt.titles,
        rt.sandbox_resources,
        rt._build_platform,
        rt.drivers,
        rt._loop_factory,
    )
    rt._run_kills = RunKillService(
        rt._store,
        rt.workspace,
        rt._lifecycle_commands,
        rt.run_registry,
        rt._loop_registry,
        rt._run_resources,
    )
    rt._control = ControlOps(
        rt._store,
        rt.workspace,
        rt._loop_registry,
        rt._cancellations,
        rt.run_controller,
        rt._run_kills,
    )
    rt._disco_kernel = DiscoKernel._from_owners(
        rt._store,
        rt.workspace,
        rt._lifecycle_commands,
        rt.run_controller,
        rt._control,
        rt._loop_factory,
        rt.run_registry,
        rt._kernel_pin_store,
    )
    rt._kernel_pins = KernelPinRegistry(
        DiscoKernelSelector(rt._disco_kernel),
        store=rt._kernel_pin_store,
    )
    rt._resume = ResumeService(
        ResumeStoreAccess(rt._store),
        ResumeRunState(
            rt.run_registry,
            rt._run_resources,
            rt._loop_registry,
            rt._cancellations,
        ),
        ResumeWorkspaceFence(rt.workspace),
        ResumeSurfaceSettings(rt.settings),
        rt._lifecycle_commands,
        ResumeEnvironmentProbe(rt.projects, rt.uploads, rt.sessions),
        PinnedRunStart(rt._kernel_pins),
    )
    rt._disco_kernel._bind_resume(rt._resume)
    rt.conversation_control = ConversationControlService(
        rt.contract,
        rt._kernel_pins,
        rt._control,
        rt._resume,
    )
    rt._run_lifecycle = RunLifecycleService(
        rt.contract,
        rt._kernel_pins,
        rt.run_registry,
        rt._cancellations,
        rt.run_controller,
        rt._control,
    )
    return completion


def _wire_run_completion(
    rt: _RuntimeWiringSchema,
    completion: DeferredRunCompletion,
) -> None:
    run_policy = RunSurfaceSettings(rt.settings)
    observability = RunObservability(rt.contract, rt._store)
    rt._run_finalizer = RunFinalizer(
        rt.run_registry,
        rt._run_ingress,
        rt._run_recovery,
        rt._kernel_pins,
        rt._store,
        rt.workspace,
        rt._lifecycle_commands,
        RunReentry(rt.run_controller, rt._resume),
        run_policy,
        observability,
        workflow_handoff=_workflow_handoff(rt),
    )
    completion.bind(rt._run_finalizer)
    rt.run_sweep = RunStrandedSweep(
        rt.run_registry,
        rt._loop_registry,
        rt._store,
        rt._run_finalizer,
    )
    rt._idle_sweeper = LifecycleIdleSweeper(rt.lifecycle, rt.run_sweep)
    rt._run_execution = RunPersistenceSupervisor(
        rt.run_registry,
        rt._run_authorities,
        rt._store,
        rt._lifecycle_commands,
        run_policy,
        observability,
        RunPreflight(rt._driver_preflight, rt.sandbox),
        RunPersistence(rt.workspace, rt.lifecycle),
        DeepResearchRun(rt.deep_research),
    )
    rt.workspace.bind_run_collaborators(
        rt.run_controller,
        rt._run_execution,
    )


def _workflow_handoff(rt: _RuntimeWiringSchema):
    async def handoff(conversation_id: str) -> None:
        # The broad Agent sandbox is no longer trusted for a sealed workflow.
        # LifecycleManager detaches and destroys it before kick composes the
        # workflow-scoped executor in the same conversation.
        await rt.lifecycle._teardown_sandbox(conversation_id)
        rt.run_controller.kick(conversation_id)

    return handoff


def _wire_workflows(rt: _RuntimeWiringSchema) -> None:
    def prepare_workflow(
        instance_id: str, params: Mapping[str, Any], owner_id: str
    ) -> object:
        service = rt._loop_factory.workflow_invocation_service_for_owner(owner_id)
        if service is None:
            return WorkflowInvocationPreparation(
                accepted=False,
                instance_id=instance_id,
                readiness=WorkflowReadiness(
                    ready=False,
                    reasons=("workflow_runtime_unavailable",),
                ),
                reason="workflow_runtime_unavailable",
            )
        return service.prepare(instance_id, params=params, owner_id=owner_id)

    workflow_runs = WorkflowRunService(
        rt._store,
        lifecycle_commands=rt._lifecycle_commands,
        workspace=rt.workspace,
        project_access=WorkflowProjectAccessAdapter(rt.projects),
        settings=WorkflowConversationSettingsAdapter(rt.settings),
        model_access=WorkflowModelAccessAdapter(rt.drivers),
        loop_factory=WorkflowLoopFactoryAdapter(rt._loop_factory),
        run_control=WorkflowRunControlAdapter(
            rt.run_registry,
            rt._run_authorities,
            rt._run_ingress,
            rt._loop_registry,
            rt._run_resources,
            rt._driver_contexts,
            rt.workspace,
            rt._run_supervisor,
            rt._run_finalizer,
        ),
        prepare_workflow=prepare_workflow,
    )
    rt.schedules = ScheduleService(
        rt._store,
        workflow_runs=workflow_runs,
        project_access=WorkflowProjectAccessAdapter(rt.projects),
        recurring_control=RecurringScheduleControlAdapter(
            rt.settings,
            rt.conversation_control,
            rt.deep_research,
        ),
    )


def _wire_runs(rt: _RuntimeWiringSchema) -> None:
    completion = _wire_run_control(rt)
    _wire_run_completion(rt, completion)
    _wire_workflows(rt)


def wire_runtime(
    rt: _RuntimeWiringSchema,
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
