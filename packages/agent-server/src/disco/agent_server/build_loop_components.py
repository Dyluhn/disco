"""Pure build-loop composition components and typed ports.

This module holds the stateless helpers, MCP tool wrappers, the no-tool
executor for research surfaces, and the typed port Protocols that the
``BuildLoopFactory``/``BuildLoopComposer`` collaborators consume.  No symbol
here references ``ConversationRuntime``; every runtime concern is reached
through a narrow, explicitly-typed port.

The helpers are mechanical extractions from ``runtime.py`` (pure move, zero
behavior change).  They remain ``Any``-free at their public seams: every
parameter and return type is a concrete Core/Tools type or a port defined in
this module.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from disco.core import (
    SecurityRisk,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.env import disco_env
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    ModelExecutionPolicy,
    ToolSpec,
)
from disco.core.loop import (
    AgentLoop,
    RouterAgent,
    SealabilityProbeResult,
    host_verify_authoritative_enabled,
)
from disco.core.loop.context_budget import derive_context_caps
from disco.core.release.spec import ReleaseIntent
from disco.core.store import EventStore
from disco.core.workflow import WorkflowRun
from disco.tools import (
    AppKitPhaseState,
    AppKitToolExecutor,
    DefaultToolExecutor,
    SandboxService,
    SandboxSession,
    SandboxSpec,
    ToolDef,
    ToolRegistry,
    ToolScope,
    WorkflowPhase,
    WorkflowPhaseState,
    agent_scope,
    artifact_scope,
    build_default_registry,
)
from disco.tools.builtin.verify_appkit_app import (
    APPKIT_LIVE_PREVIEW_NAME,
    APPKIT_VITE_PACKAGE_SHA_RELPATH,
)
from disco.tools.mcp import McpPool
from disco.tools.projects import ProjectStore

from .build_contract_service import _ToolScopeAuditRecorder
from .build_platform_shadow import (
    build_platform_shadow_enabled,
    observe_legacy_build,
)
from .preview_manager import PreviewManager

logger = logging.getLogger(__name__)

_TOOLSCOPE_AUDIT_FLAG = "TOOLSCOPE_AUDIT"
_WORKFLOW_ROUTER_FLAG = "WORKFLOW_ROUTER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ── environment-flag readers ──────────────────────────────────────────────


def toolscope_audit_enabled() -> bool:
    """True iff DISCO_TOOLSCOPE_AUDIT is truthy (default OFF)."""
    return str(disco_env(_TOOLSCOPE_AUDIT_FLAG) or "").strip().lower() in _TRUTHY


def workflow_router_enabled() -> bool:
    """True iff DISCO_WORKFLOW_ROUTER is truthy (default OFF)."""
    return str(disco_env(_WORKFLOW_ROUTER_FLAG) or "").strip().lower() in _TRUTHY


# ── MCP tool wrappers ─────────────────────────────────────────────────────


class _MCPToolWrapper:
    """Thin Tool-protocol wrapper that adapts an MCP ToolDef for the registry.

    MCP tools run through the pool's per-server client (routed by qualified
    name), not through the default executor's sandbox path.  The definition is
    the ToolDef built at pool start; ``run`` invokes the real tool via the
    pool.
    """

    def __init__(self, tdef: Any, pool: Any) -> None:
        self.definition = tdef
        self._pool = pool

    async def run(self, args: Any, ctx: Any) -> Any:
        from disco.tools.anatomy import ToolOutcome
        from disco.tools.mcp import fence_mcp_result
        from disco.tools.mcp.naming import split_qualified_name

        qn = self.definition.name
        parts = split_qualified_name(qn)
        if parts is None:
            return ToolOutcome(
                success=False,
                content=f"MCP tool {qn!r}: not a valid qualified name",
                error="invalid qualified name",
            )
        server, tool = parts
        try:
            if hasattr(args, "model_dump"):
                raw_args = args.model_dump()
            else:
                raw_args = dict(args)
            result = await self._pool.call_tool(server, tool, raw_args)
            content = fence_mcp_result(server, tool, result)
            return ToolOutcome(
                success=not result.get("isError", False),
                content=content,
            )
        except Exception as exc:
            return ToolOutcome(
                success=False,
                content=f"MCP tool {qn!r} call failed: {exc}",
                error=str(exc),
            )


class _MetaToolSearchWrapper:
    """Wraps ``tool_search`` meta-tool with the full MCP tool list for searching.

    The LLM calls this when the pool is over the schema cap.  It runs an
    orchestrator-side keyword search over ALL MCP tools (including those
    hidden from the advertised set) and returns qualified names + descriptions.
    The active schema set is never mutated — discovery only.
    """

    def __init__(self, tdef: Any, all_tool_descs: list[dict[str, str]]) -> None:
        self.definition = tdef
        self._all_tool_descs = all_tool_descs

    async def run(self, args: Any, ctx: Any) -> Any:
        import json

        from disco.tools.anatomy import ToolOutcome
        from disco.tools.mcp.tool_search import _tool_search_handler

        results = await _tool_search_handler(
            query=args.query,
            limit=args.limit,
            all_tools=self._all_tool_descs,
        )
        content = json.dumps(results, indent=2) if results else "No matching tools found."
        return ToolOutcome(success=True, content=content, structured={"results": results})


# ── MCP composition helpers ───────────────────────────────────────────────


def _mcp_base_risk_by_tool(tools: list[ToolDef]) -> dict[str, SecurityRisk]:
    """Project operator-configured MCP risk tiers into the live analyzer map."""
    risks: dict[str, SecurityRisk] = {}
    for tool in tools:
        risk = getattr(tool, "base_risk", None)
        if isinstance(risk, SecurityRisk):
            risks[tool.name] = risk
    return risks


def _apply_mcp_scope(
    executor: DefaultToolExecutor,
    all_mcp_tools: list[ToolDef],
    call_target: Any,
    *,
    max_active_schemas: int = 20,
) -> None:
    """Register MCP tools in the executor and apply the advertised/callable split."""
    if not all_mcp_tools:
        return

    mcp_names = frozenset(t.name for t in all_mcp_tools)
    non_mcp_allowed = executor._scope.allowed_tools
    non_mcp_advertised = (
        executor._scope.advertised_tools
        if executor._scope.advertised_tools is not None
        else non_mcp_allowed
    )

    scope_update: dict[str, Any] = {"allowed_tools": non_mcp_allowed | mcp_names}
    if executor._scope.advertised_tools is not None:
        scope_update["advertised_tools"] = non_mcp_advertised | mcp_names
    executor._scope = executor._scope.model_copy(update=scope_update)
    for tdef in all_mcp_tools:
        executor._registry.register(_MCPToolWrapper(tdef, call_target))

    if len(all_mcp_tools) > max_active_schemas:
        from disco.tools.mcp.tool_search import meta_tool_search

        ts_def = meta_tool_search()
        all_tool_descs = [{"name": t.name, "description": t.description} for t in all_mcp_tools]
        executor._registry.register(_MetaToolSearchWrapper(ts_def, all_tool_descs))
        executor._scope = executor._scope.model_copy(
            update={
                "allowed_tools": executor._scope.allowed_tools | {"tool_search"},
                "advertised_tools": non_mcp_advertised | {"tool_search"},
            }
        )
    elif executor._scope.advertised_tools is not None:
        executor._scope = executor._scope.model_copy(
            update={"advertised_tools": executor._scope.advertised_tools | mcp_names}
        )


def _freeform_ejection_tool_specs(
    base_scope: ToolScope,
    all_mcp_tools: list[ToolDef],
    call_target: Any,
    *,
    max_active_schemas: int,
) -> tuple[ToolSpec, ...]:
    """Resolve the ordinary post-ejection catalog without widening the live executor."""
    preview = DefaultToolExecutor(build_default_registry(), base_scope)
    _apply_mcp_scope(
        preview,
        all_mcp_tools,
        call_target,
        max_active_schemas=max_active_schemas,
    )
    return tuple(preview.available_tools())


def _mcp_composition_inputs(
    mcp_pool: McpPool | None,
    mcp_http_tools: Mapping[str, ToolDef],
    config_store: ConfigStore,
) -> tuple[list[ToolDef], int]:
    """Snapshot one loop's MCP catalog and configured visibility ceiling."""
    tools: list[ToolDef] = []
    if mcp_pool is not None and mcp_pool.started:
        tools.extend(mcp_pool.snapshot())
    if mcp_http_tools:
        tools.extend(mcp_http_tools.values())
    config = config_store.load()
    return tools, config.mcp.max_active_schemas if config.mcp else 20


def _bind_ejection(
    ejection_service: Any,
    conversation_id: str,
    executor: AppKitToolExecutor,
    base_scope: ToolScope,
    mcp_tools: list[ToolDef],
    mcp_call_target: Any,
    max_active_schemas: int,
) -> None:
    specs = _freeform_ejection_tool_specs(
        base_scope,
        mcp_tools,
        mcp_call_target,
        max_active_schemas=max_active_schemas,
    )
    executor.set_ejection_callback(
        lambda call: ejection_service.eject(
            conversation_id,
            call,
            tool_specs=specs,
        )
    )


def _build_appkit_registry() -> ToolRegistry:
    """Default registry plus AppKit-only tools for strict AppKit executors."""
    from disco.tools.builtin.app_kit import APPKIT_V2_TOOLS
    from disco.tools.builtin.design_lint import DesignLintTool
    from disco.tools.builtin.request_custom_build import RequestCustomBuildTool
    from disco.tools.builtin.verify_appkit_app import VerifyAppKitAppTool

    registry = build_default_registry()
    for tool_cls in (
        *APPKIT_V2_TOOLS,
        DesignLintTool,
        RequestCustomBuildTool,
        VerifyAppKitAppTool,
    ):
        registry.register(tool_cls())
    return registry


async def _sync_appkit_live_preview(session: SandboxSession) -> None:
    """Keep strict AppKit on one real, platform-managed Vite development server."""
    manager = getattr(session, "_preview_manager", None)
    if manager is None:
        manager = PreviewManager(session)
        session._preview_manager = manager

    install_failure = ""
    package_changed = False
    dependencies_refreshed = False
    try:
        package_bytes = await session.read_file("package.json")
        package_digest = hashlib.sha256(package_bytes).hexdigest()
        try:
            await session.list_dir("node_modules")
            has_modules = True
        except Exception:  # noqa: BLE001
            has_modules = False
        try:
            marker = (await session.read_file(APPKIT_VITE_PACKAGE_SHA_RELPATH)).decode().strip()
        except Exception:  # noqa: BLE001
            marker = ""
        package_changed = marker != package_digest
        if not has_modules or marker != package_digest:
            if not await session.file_exists("package-lock.json"):
                install_failure = "AppKit preview requires its generated package-lock.json."
            else:
                installed = await session.exec_shell(
                    "npm ci --no-audit --no-fund",
                    timeout_s=300,
                )
                if getattr(installed, "exit_code", 1) != 0 or bool(
                    getattr(installed, "timed_out", False)
                ):
                    detail = str(
                        getattr(installed, "stderr", "")
                        or getattr(installed, "stdout", "")
                        or "npm ci failed"
                    ).strip()
                    install_failure = f"AppKit preview dependency setup failed: {detail[-1200:]}"
                else:
                    dependencies_refreshed = True
                    await session.write_file(
                        APPKIT_VITE_PACKAGE_SHA_RELPATH,
                        (package_digest + "\n").encode(),
                    )
    except Exception as exc:  # noqa: BLE001
        install_failure = f"AppKit preview preparation failed: {exc}"

    try:
        current = manager.canonical_lifecycle_session()
        if (
            install_failure
            and current is not None
            and getattr(getattr(current, "status", None), "value", "") in {"running", "unavailable"}
        ):
            current.update_error = install_failure
            return
        if package_changed and dependencies_refreshed and current is not None:
            await manager.stop(current.name)
        preview = await manager.start(
            framework="vite",
            name=APPKIT_LIVE_PREVIEW_NAME,
            supervise=True,
        )
        preview.update_error = install_failure or None
    except Exception:  # noqa: BLE001
        logger.warning("AppKit managed preview could not start", exc_info=True)


# ── no-tool executor (research surface) ───────────────────────────────────


class _NoToolExecutor:
    """A read-only surface with no tools: the model answers directly."""

    def available_tools(self) -> list:
        return []

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            content="",
            error="no tools are available on this surface",
        )


# ── typed ports ───────────────────────────────────────────────────────────


@runtime_checkable
class LoopSettingsPort(Protocol):
    """Read-only settings and policy resolution for loop composition."""

    def _surface_of(self, conversation_id: str) -> str: ...

    def _effective_autonomous(self, conversation_id: str) -> bool: ...

    def _effective_quiet(self, conversation_id: str) -> bool: ...

    def _effective_artifact_mode(self, conversation_id: str) -> bool: ...

    def _effective_appkit_mode(self, conversation_id: str) -> bool: ...

    def _effective_policy(self, conversation_id: str) -> ModelExecutionPolicy: ...

    def _effective_driver_endpoint(
        self, conversation_id: str
    ) -> tuple[str, str, str | None] | None: ...

    def _driver_context_window(self, conversation_id: str | None = ...) -> int | None: ...

    def _get_model_override(self, conversation_id: str) -> str | None: ...


@runtime_checkable
class LoopSandboxPort(Protocol):
    """Sandbox session creation, spec building, and rehydration."""

    def _sandbox_service_now(self) -> SandboxService: ...

    def _build_sandbox_spec(
        self, *, surface: str = ..., mcp_egress_hosts: frozenset[str] | None = ...
    ) -> SandboxSpec: ...

    async def _rehydrate_after_recreate(self, conversation_id: str) -> None: ...


@runtime_checkable
class LoopMcpPort(Protocol):
    """MCP pool, HTTP tools, call target, egress, and approval state."""

    @property
    def _mcp_pool(self) -> McpPool | None: ...

    @property
    def _mcp_http_tools(self) -> dict[str, ToolDef]: ...

    @property
    def _mcp_call_target(self) -> Any: ...

    def _mcp_egress_hosts(self) -> frozenset[str]: ...

    @property
    def _mcp_approval_pending(self) -> dict[str, dict]: ...

    def _config_store(self) -> ConfigStore: ...


@runtime_checkable
class LoopContractPort(Protocol):
    """Build-contract scope guards, finalizers, starters, and audit."""

    def _build_scope_guard(
        self,
        conversation_id: str,
        *,
        observer: Callable[[str, Any, Any], None] | None = ...,
    ) -> tuple[Any, Callable[[str], None] | None]: ...

    def _build_scope_audit_guard(
        self, conversation_id: str
    ) -> tuple[Any, Callable[[str], None]]: ...

    def _toolscope_audit_recorder(self, conversation_id: str) -> _ToolScopeAuditRecorder: ...

    def _finalizer_alias_for(self, conversation_id: str) -> str | None: ...

    def _starter_kit_for(self, conversation_id: str) -> str | None: ...

    def _host_verify_canary_hook_for(
        self, conversation_id: str
    ) -> Callable[[Any], Awaitable[None]] | None: ...


@runtime_checkable
class LoopWorkspacePort(Protocol):
    """Workspace locks, fences, commit hooks, and run-admission state."""

    def lock(self, conversation_id: str) -> Any: ...

    def fence(self, conversation_id: str) -> Any: ...

    def interprocess_mutation_fence(self, conversation_id: str) -> Any: ...

    def terminal_commit_hook(
        self, conversation_id: str
    ) -> Callable[[StatusEvent], Awaitable[StatusEvent]]: ...

    def finish_sealability_probe(
        self, conversation_id: str
    ) -> Callable[[], Awaitable[SealabilityProbeResult]]: ...

    def has_admitted_run(self, conversation_id: str) -> bool: ...


@runtime_checkable
class LoopBuildPlatformPort(Protocol):
    """Build Platform route selection and shadow observation."""

    def select_builtin(
        self,
        conversation_id: str,
        *,
        appkit: bool,
        eligible: bool,
        tool_specs: Iterable[ToolSpec],
    ) -> None: ...


@runtime_checkable
class LoopRetrievalPort(Protocol):
    """Retrieval handlers for the capability broker."""

    def _retrieval_handlers(self) -> dict[str, Any]: ...


@runtime_checkable
class LoopEventStorePort(EventStore, Protocol):
    """Event store for execution-admission checks and ephemeral publishing.

    Extends :class:`EventStore` so the port can be passed directly to
    ``AgentLoop.__init__``.  Adds ``conversation_owner_id_sync`` for the
    workflow store owner-id resolution.
    """

    def conversation_owner_id_sync(self, conversation_id: str) -> str | None: ...


@runtime_checkable
class LoopProjectStorePort(Protocol):
    """Project store and release-intent writer for the executor."""

    def _project_store_now(self) -> ProjectStore: ...

    async def _write_release_intent(
        self, conversation_id: str, owner_id: str, intent: ReleaseIntent
    ) -> None: ...


@runtime_checkable
class LoopRouterPort(Protocol):
    """Router construction for loop composition."""

    def _router_now(
        self,
        pick: str | None = ...,
        *,
        surface: str | None = ...,
        autonomous: bool = ...,
        conversation_id: str | None = ...,
        appkit_mode: bool = ...,
    ) -> DefaultLLMRouter: ...


@runtime_checkable
class LoopDeepResearchPort(Protocol):
    """Deep Research loop composition delegate."""

    def _compose_deep_research_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = ...,
    ) -> AgentLoop: ...


# ── compose context ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ComposeContext:
    """Resolved policy/scope/budget state for one compose call."""

    model_policy: ModelExecutionPolicy
    scope: ToolScope
    driver_context_window: int | None
    read_char_budget: int
    scope_guard: Any
    on_tool_success: Any
    art_mode: bool
    appkit_mode: bool
    appkit_autonomous: bool
    appkit_phase: AppKitPhaseState | None
    workflow_router_mode: bool
    workflow_phase: WorkflowPhaseState | None


def resolve_compose_context(
    settings: LoopSettingsPort,
    contract: LoopContractPort,
    conversation_id: str,
    driver_context_window: int | None,
    sealed_workflow_run: WorkflowRun | None,
    sealed_workflow_instance_id: str | None,
) -> ComposeContext:
    """Resolve all policy/scope/budget inputs for one compose call."""
    model_policy = settings._effective_policy(conversation_id)
    art_mode = settings._effective_artifact_mode(conversation_id)
    scope = artifact_scope() if art_mode else agent_scope(model_policy=model_policy)
    dcw = (
        driver_context_window
        if isinstance(driver_context_window, int) and driver_context_window > 0
        else settings._driver_context_window(conversation_id)
    )
    read_char_budget = derive_context_caps(
        assist=model_policy.assist, context_window=dcw
    ).read_char_budget
    audit_on = toolscope_audit_enabled()
    if art_mode:
        observer = (
            contract._toolscope_audit_recorder(conversation_id).record if audit_on else None
        )
        scope_guard, on_tool_success = contract._build_scope_guard(
            conversation_id, observer=observer
        )
    elif audit_on:
        scope_guard, on_tool_success = contract._build_scope_audit_guard(conversation_id)
    else:
        scope_guard, on_tool_success = (None, None)
    appkit_mode = settings._effective_appkit_mode(conversation_id) and not art_mode
    appkit_autonomous = settings._effective_autonomous(conversation_id)
    appkit_phase = AppKitPhaseState() if appkit_mode else None
    workflow_router_mode = (
        workflow_router_enabled()
        and settings._surface_of(conversation_id) == "agent"
        and not art_mode
        and not appkit_mode
        and sealed_workflow_run is None
    )
    workflow_phase = (
        WorkflowPhaseState(
            phase=WorkflowPhase.RUN,
            instance_id=sealed_workflow_instance_id,
            output_path_template=(
                sealed_workflow_run.definition.output_contract.path_template
                if sealed_workflow_run is not None
                else None
            ),
        )
        if sealed_workflow_run is not None
        else (WorkflowPhaseState() if workflow_router_mode else None)
    )
    return ComposeContext(
        model_policy=model_policy,
        scope=scope,
        driver_context_window=dcw,
        read_char_budget=read_char_budget,
        scope_guard=scope_guard,
        on_tool_success=on_tool_success,
        art_mode=art_mode,
        appkit_mode=appkit_mode,
        appkit_autonomous=appkit_autonomous,
        appkit_phase=appkit_phase,
        workflow_router_mode=workflow_router_mode,
        workflow_phase=workflow_phase,
    )


# ── re-exports for the factory ────────────────────────────────────────────

__all__ = [
    "ComposeContext",
    "LoopBuildPlatformPort",
    "LoopContractPort",
    "LoopDeepResearchPort",
    "LoopEventStorePort",
    "LoopMcpPort",
    "LoopProjectStorePort",
    "LoopRetrievalPort",
    "LoopRouterPort",
    "LoopSandboxPort",
    "LoopSettingsPort",
    "LoopWorkspacePort",
    "_NoToolExecutor",
    "_apply_mcp_scope",
    "_bind_ejection",
    "_build_appkit_registry",
    "_freeform_ejection_tool_specs",
    "_mcp_base_risk_by_tool",
    "_mcp_composition_inputs",
    "_sync_appkit_live_preview",
    "host_verify_authoritative_enabled",
    "observe_legacy_build",
    "build_platform_shadow_enabled",
    "resolve_compose_context",
    "toolscope_audit_enabled",
    "workflow_router_enabled",
]