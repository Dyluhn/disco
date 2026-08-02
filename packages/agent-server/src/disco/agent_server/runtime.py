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
from typing import TYPE_CHECKING, Any, cast

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
from .runtime_compatibility import install_runtime_compatibility
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
from .runtime_wiring_schema import _RuntimeWiringSchema
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
    import asyncio
    import contextlib
    from collections.abc import AsyncIterator

    from disco.core import DEFAULT_OWNER_ID, ToolCall, ToolResult, WorkspaceMutationEvent
    from disco.core.llm import SandboxSettings
    from disco.tools import SandboxSession
    from disco.tools.projects import VersionRecord
    from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

    from .build_contract_service import BuildContractService
    from .build_loop_factory import BuildLoopFactory
    from .conversation_control_service import ConversationControlService
    from .deep_research_service import DeepResearchService
    from .driver_runtime import DriverPreflight, DriverRuntime
    from .lifecycle import LifecycleManager
    from .lifecycle_idle_sweep import LifecycleIdleSweeper
    from .live_session_directory import LiveSessionDirectory
    from .mcp_manager import McpManager
    from .project_runtime_service import ProjectRuntimeService
    from .resume_service import ResumeService
    from .run_registry import RunRegistry
    from .run_stranded_sweep import RunStrandedSweep
    from .run_supervisor import (
        RunPersistenceSupervisor,
        RunSupervisor,
    )
    from .runtime_settings import RuntimeSettings
    from .sandbox_runtime_service import SandboxRuntimeService
    from .schedule_service import ScheduleService
    from .share_service import ShareService
    from .space_service import SpaceService
    from .suggestion_service import SuggestionService
    from .title_service import TitleService
    from .upload_store import UploadStore
    from .workspace_commit import CommittedWorkspaceView
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


class ConversationRuntime:
    """Wiring-only root plus the transport-stable conversation ingress facade."""

    if TYPE_CHECKING:
        # fmt: off
        # The declared public collaborator seam (13-B1). Consumers reach
        # the owning service by name instead of through a delegate.
        run_sweep: RunStrandedSweep
        schedules: ScheduleService
        share: ShareService
        spaces: SpaceService
        suggestions: SuggestionService
        titles: TitleService
        uploads: UploadStore

        _config_store: ConfigStore
        _contract: BuildContractService
        _conversation_control: ConversationControlService
        _dr: DeepResearchService
        _driver_preflight: DriverPreflight
        _drivers: DriverRuntime
        _idle_sweeper: LifecycleIdleSweeper
        _lifecycle: LifecycleManager
        _live_sessions: LiveSessionDirectory
        _loop_factory: BuildLoopFactory
        _mcp: McpManager
        _projects: ProjectRuntimeService
        _resume: ResumeService
        _run_registry: RunRegistry
        _run_execution: RunPersistenceSupervisor
        _run_supervisor: RunSupervisor
        _sandbox: SandboxRuntimeService
        _secret_store: SecretStore
        _settings: RuntimeSettings
        _skill_store: SkillStore
        _workspace: WorkspaceCoordinator

        def set_surface(self, conversation_id: str, surface: str) -> None: ...
        async def probe_active_sandbox(self) -> tuple[bool, str, str]: ...
        async def probe_sandbox_config(self, settings: SandboxSettings) -> tuple[bool, str, str]: ...  # noqa: E501
        async def prewarm_model_probe(self) -> None: ...
        async def prewarm_vision_probe(self) -> None: ...
        def set_model_override(self, conversation_id: str, model_id: str | None) -> None: ...
        def set_autonomous(self, conversation_id: str, value: bool=True) -> None: ...
        def is_autonomous(self, conversation_id: str) -> bool: ...
        def set_quiet(self, conversation_id: str, value: bool=True) -> None: ...
        def is_quiet(self, conversation_id: str) -> bool: ...
        def set_assist(self, conversation_id: str, value: bool=True) -> None: ...
        def is_assist(self, conversation_id: str) -> bool: ...
        async def apply_settings_change(self, conversation_id: str, *, model_override: str | None=None, assist: bool | None=None, model_provided: bool | None=None) -> bool: ...  # noqa: E501
        def set_artifact_mode(self, conversation_id: str, on: bool) -> None: ...
        def set_appkit_mode(self, conversation_id: str, on: bool) -> None: ...
        def set_research_sources(self, conversation_id: str, sources: list[str]) -> None: ...
        def get_research_sources(self, conversation_id: str | None) -> tuple[str, ...]: ...
        def activate_contract_for_brief(self, conversation_id: str, build_brief: BuildBrief | None) -> None: ...  # noqa: E501
        def set_build_kind(self, conversation_id: str, kind: str | None) -> None: ...
        def expected_delivery_mode(self, conversation_id: str) -> str | None: ...
        def note_build_verify_result(self, conversation_id: str, *, passed: bool) -> None: ...
        def get_last_selected_model(self) -> str | None: ...
        def set_last_selected_model(self, model_id: str | None) -> None: ...
        def driver_models(self) -> dict[str, Any]: ...
        async def execute_pi_tool(self, conversation_id: str, tool_call: ToolCall) -> ToolResult: ...  # noqa: E501
        def upload_session(self, conversation_id: str) -> SandboxSession: ...
        def workspace_lock(self, conversation_id: str) -> asyncio.Lock: ...
        def workspace_fence(self, conversation_id: str) -> contextlib.AbstractAsyncContextManager[None]: ...  # noqa: E501
        @contextlib.asynccontextmanager
        async def workspace_mutation(self, conversation_id: str, operation: str, *, paths: tuple[str, ...]=()) -> AsyncIterator[None]:  # noqa: E501
            if False:
                yield
        async def record_workspace_mutation_locked(self, conversation_id: str, operation: str, *, paths: tuple[str, ...]=()) -> WorkspaceMutationEvent: ...  # noqa: E501
        async def finalize_host_workspace_change(self, conversation_id: str, operation: str) -> VersionRecord: ...  # noqa: E501
        async def finalize_host_mirror_change_locked(self, conversation_id: str, operation: str) -> VersionRecord: ...  # noqa: E501
        async def require_committed_host_mirror_locked(self, conversation_id: str) -> CommittedWorkspaceView: ...  # noqa: E501
        def add_upload_passages(self, conversation_id: str, passages: list[Any]) -> None: ...
        def get_upload_passages(self, conversation_id: str) -> list[Any]: ...
        def set_depth(self, conversation_id: str, tier: str | None) -> None: ...
        def set_iterative(self, conversation_id: str, enabled: bool) -> None: ...
        def set_recency(self, conversation_id: str, window: str | None) -> None: ...
        def research_stream(self, query: str, *, model_override: str | None=None, drop_weak: bool=False, domains_deny: frozenset[str]=frozenset(), think: bool=False, conversation_id: str | None=None, space_ids: frozenset[str]=frozenset(), owner_id: str | None=None, include_unclaimed_legacy: bool=False, sources: list[str] | tuple[str, ...] | None=None) -> AsyncIterator[dict[str, Any]]: ...  # noqa: E501
        def kick(self, conversation_id: str, *, claimed_user_seq: int | None=None) -> None: ...
        async def reconcile_sandbox_backend(self) -> int: ...
        async def reconcile_orphaned_runs(self, *, owner_id: str=DEFAULT_OWNER_ID) -> int: ...
        async def reload_mcp_pool(self) -> dict[str, Any]: ...
        def mcp_approval_state(self) -> dict[str, dict]: ...
        def on_connect(self, conversation_id: str) -> None: ...
        def on_disconnect(self, conversation_id: str, *, grace_s: float=60.0) -> None: ...
        def sandbox_state(self, conversation_id: str) -> str | None: ...
        def sandbox_instance_ids(self, conversation_id: str) -> list[str]: ...
        async def sweep_idle_once(self) -> int: ...
        async def sweep_abandoned_gates_once(self, *, owner_id: str=DEFAULT_OWNER_ID) -> int: ...
        def project_store(self) -> ProjectStore: ...
        async def restore_workspace_version(self, conversation_id: str, seq: int) -> dict: ...
        async def export_report(self, conversation_id: str, fmt: str, *, owner_id: str=DEFAULT_OWNER_ID) -> tuple[bytes, str, str] | None: ...  # noqa: E501
        def resolve_cid_prefix(self, cid8: str) -> str | None: ...
        async def resolve_owned_cid_prefix(self, cid8: str, owner_id: str) -> str | None: ...
        def preview_upstream(self, conversation_id: str) -> str | None: ...
        def preview_target_port(self, conversation_id: str) -> int | None: ...
        async def resolve_active_preview_projection(self, conversation_id: str, projection: Any) -> bool: ...  # noqa: E501
        async def resolve_finished_preview_runtime(self, conversation_id: str, contract: Any) -> dict[str, Any] | None: ...  # noqa: E501
        def port_upstream(self, conversation_id: str, port: int) -> str | None: ...
        async def wake_for_preview(self, cid8: str, port: int, *, owner_id: str=DEFAULT_OWNER_ID) -> str | None: ...  # noqa: E501
        def live_session(self, conversation_id: str) -> SandboxSession | None: ...
        def sandbox_backend_name(self) -> str | None: ...
        async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]: ...  # noqa: E501
        async def sessions_list(self, conversation_id: str) -> list[SessionInfo]: ...
        async def session_view(self, conversation_id: str, name: str, tail_chars: int) -> SessionView | None: ...  # noqa: E501
        async def preview(self, conversation_id: str) -> dict[str, Any]: ...
        async def ensure_preview(self, conversation_id: str) -> bool: ...
        async def resume_conversation(self, conversation_id: str) -> dict: ...
        async def forget_conversation(self, conversation_id: str) -> None: ...
        def running_conversation_ids(self) -> set[str]: ...
        # fmt: on

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
            cast(_RuntimeWiringSchema, self),
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
        return self._config_store.approvals.origin_approved(
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

    def _workflow_tool_definitions(self) -> tuple[Any, ...]:
        """State-free compatibility delegate pending PKG-11-WORKFLOWS."""

        return self._mcp.workflow_tool_definitions()

    def _workflow_skills(self) -> list[Any]:
        """State-free compatibility delegate pending PKG-11-WORKFLOWS."""

        return self._skill_store.list()

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


install_runtime_compatibility(ConversationRuntime)
