"""State-free legacy ``ConversationRuntime`` delegates retained until PKG-13."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from disco.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    AgentErrorEvent,
    EventSource,
    ObservationEvent,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator

    from disco.core import ToolCall, ToolResult, WorkspaceMutationEvent
    from disco.core.appkit import BuildBrief
    from disco.core.llm import SandboxSettings
    from disco.tools import SandboxSession
    from disco.tools.projects import ProjectStore, VersionRecord
    from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

    from .runtime import ConversationRuntime
    from .workspace_commit import CommittedWorkspaceView


def set_surface(self, conversation_id: str, surface: str) -> None:

    self._settings._set_surface(conversation_id, surface)


async def probe_active_sandbox(self) -> tuple[bool, str, str]:

    return await self._sandbox.probe_active_sandbox()


async def probe_sandbox_config(self, settings: SandboxSettings) -> tuple[bool, str, str]:

    return await self._sandbox.probe_sandbox_config(settings)


async def prewarm_model_probe(self) -> None:

    await self._drivers.prewarm_model_probe()


async def prewarm_vision_probe(self) -> None:

    await self._drivers.prewarm_vision_probe()


def set_model_override(self, conversation_id: str, model_id: str | None) -> None:

    self._settings.set_model_override(conversation_id, model_id)


def set_autonomous(self, conversation_id: str, value: bool = True) -> None:

    self._settings.set_autonomous(conversation_id, value)


def is_autonomous(self, conversation_id: str) -> bool:

    return self._settings.is_autonomous(conversation_id)


def set_quiet(self, conversation_id: str, value: bool = True) -> None:

    self._settings.set_quiet(conversation_id, value)


def is_quiet(self, conversation_id: str) -> bool:

    return self._settings.is_quiet(conversation_id)


def set_assist(self, conversation_id: str, value: bool = True) -> None:

    self._settings.set_assist(conversation_id, value)


def is_assist(self, conversation_id: str) -> bool:

    return self._settings.is_assist(conversation_id)


async def apply_settings_change(
    self,
    conversation_id: str,
    *,
    model_override: str | None = None,
    assist: bool | None = None,
    model_provided: bool | None = None,
) -> bool:

    return await self._settings.apply_settings_change(
        conversation_id, model_override=model_override, assist=assist, model_provided=model_provided
    )


def set_artifact_mode(self, conversation_id: str, on: bool) -> None:

    self._settings.set_artifact_mode(conversation_id, on)


def set_appkit_mode(self, conversation_id: str, on: bool) -> None:

    self._settings.set_appkit_mode(conversation_id, on)


def set_research_sources(self, conversation_id: str, sources: list[str]) -> None:

    self._settings.set_research_sources(conversation_id, sources)


def get_research_sources(self, conversation_id: str | None) -> tuple[str, ...]:

    return self._settings.get_research_sources(conversation_id)


def activate_contract_for_brief(self, conversation_id: str, build_brief: BuildBrief | None) -> None:

    self._contract.activate_contract_for_brief(conversation_id, build_brief)


def set_build_kind(self, conversation_id: str, kind: str | None) -> None:

    self._contract.set_build_kind(conversation_id, kind)


def expected_delivery_mode(self, conversation_id: str) -> str | None:

    return self._contract.expected_delivery_mode(conversation_id)


def note_build_verify_result(self, conversation_id: str, *, passed: bool) -> None:

    self._contract.note_build_verify_result(conversation_id, passed=passed)


def get_last_selected_model(self) -> str | None:

    return self._settings.model_binding.get_last_selected_model()


def set_last_selected_model(self, model_id: str | None) -> None:

    self._settings.model_binding.set_last_selected_model(model_id)


def driver_models(self) -> dict[str, Any]:

    return self._drivers.catalog()


async def execute_pi_tool(self, conversation_id: str, tool_call: ToolCall) -> ToolResult:

    executor = self._run_resources.executor(conversation_id)
    if executor is None:
        self._loop_factory.loop_for(conversation_id)
        executor = self._run_resources.executor(conversation_id)
    if executor is None:
        raise RuntimeError("tool executor was not composed")
    action = ActionEvent(
        source=EventSource.AGENT, thought=f"[pi] {tool_call.tool_name}", tool_call=tool_call
    )
    await self._store.append(conversation_id, action)
    result = await executor.execute(tool_call)
    if result.success:
        await self._store.append(
            conversation_id, ObservationEvent(tool_result=result, action_id=action.id)
        )
    else:
        await self._store.append(
            conversation_id,
            AgentErrorEvent(
                error=result.error or "tool failed",
                action_id=action.id,
                tool_call_id=tool_call.call_id,
            ),
        )
    return result


def upload_session(self, conversation_id: str) -> SandboxSession:

    return self._sessions.upload_session(conversation_id)


def workspace_lock(self, conversation_id: str) -> asyncio.Lock:

    return self._workspace.lock(conversation_id)


def workspace_fence(self, conversation_id: str) -> contextlib.AbstractAsyncContextManager[None]:

    return self._workspace.fence(conversation_id)


@contextlib.asynccontextmanager
async def workspace_mutation(
    self,
    conversation_id: str,
    operation: str,
    *,
    paths: tuple[str, ...] = (),
) -> AsyncIterator[None]:

    async with self._workspace.mutation(conversation_id, operation, paths=paths):
        yield


async def record_workspace_mutation_locked(
    self,
    conversation_id: str,
    operation: str,
    *,
    paths: tuple[str, ...] = (),
) -> WorkspaceMutationEvent:

    return await self._workspace.record_mutation_locked(conversation_id, operation, paths=paths)


async def finalize_host_workspace_change(
    self,
    conversation_id: str,
    operation: str,
) -> VersionRecord:

    return await self._workspace.finalize_sandbox_change(conversation_id, operation)


async def finalize_host_mirror_change_locked(
    self,
    conversation_id: str,
    operation: str,
) -> VersionRecord:

    return await self._workspace.finalize_host_mirror_change_locked(conversation_id, operation)


async def require_committed_host_mirror_locked(
    self,
    conversation_id: str,
) -> CommittedWorkspaceView:

    return await self._workspace.require_committed_host_mirror_locked(conversation_id)


def add_upload_passages(self, conversation_id: str, passages: list[Any]) -> None:

    self._dr.add_upload_passages(conversation_id, passages)


def get_upload_passages(self, conversation_id: str) -> list[Any]:

    return self._dr.get_upload_passages(conversation_id)


def set_depth(self, conversation_id: str, tier: str | None) -> None:

    self._dr.set_depth(conversation_id, tier)


def set_iterative(self, conversation_id: str, enabled: bool) -> None:

    self._dr.set_iterative(conversation_id, enabled)


def set_recency(self, conversation_id: str, window: str | None) -> None:

    self._dr.set_recency(conversation_id, window)


def research_stream(
    self,
    query: str,
    *,
    model_override: str | None = None,
    drop_weak: bool = False,
    domains_deny: frozenset[str] = frozenset(),
    think: bool = False,
    conversation_id: str | None = None,
    space_ids: frozenset[str] = frozenset(),
    owner_id: str | None = None,
    include_unclaimed_legacy: bool = False,
    sources: list[str] | tuple[str, ...] | None = None,
) -> AsyncIterator[dict[str, Any]]:

    return self._dr.research_stream(
        query,
        model_override=model_override,
        drop_weak=drop_weak,
        domains_deny=domains_deny,
        think=think,
        conversation_id=conversation_id,
        space_ids=space_ids,
        owner_id=owner_id,
        include_unclaimed_legacy=include_unclaimed_legacy,
        sources=sources,
    )


def kick(self, conversation_id: str, *, claimed_user_seq: int | None = None) -> None:

    self._run_controller.kick(conversation_id, claimed_user_seq=claimed_user_seq)


async def reconcile_sandbox_backend(self) -> int:

    return await self._sandbox_resources.reconcile()


async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:

    return await self._lifecycle.reconcile_orphaned_runs(owner_id=owner_id)


async def reload_mcp_pool(self) -> dict[str, Any]:

    return await self._mcp.reload()


def mcp_approval_state(self) -> dict[str, dict]:

    return self._mcp.mcp_approval_state()


def on_connect(self, conversation_id: str) -> None:

    self._connections.on_connect(conversation_id)


def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:

    self._connections.on_disconnect(conversation_id, grace_s=grace_s)


def sandbox_state(self, conversation_id: str) -> str | None:

    return self._lifecycle.sandbox_state(conversation_id)


def sandbox_instance_ids(self, conversation_id: str) -> list[str]:

    return self._lifecycle.sandbox_instance_ids(conversation_id)


async def sweep_idle_once(self) -> int:

    return await self._lifecycle.sweep_idle_once()


async def sweep_abandoned_gates_once(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:

    return await self._lifecycle.sweep_abandoned_gates_once(owner_id=owner_id)


def project_store(self) -> ProjectStore:

    return self._projects.current_project_store()


async def restore_workspace_version(self, conversation_id: str, seq: int) -> dict:

    return await self._workspace.restore_version(conversation_id, seq)


async def export_report(
    self,
    conversation_id: str,
    fmt: str,
    *,
    owner_id: str = DEFAULT_OWNER_ID,
) -> tuple[bytes, str, str] | None:

    return await self._dr.export_report(conversation_id, fmt, owner_id=owner_id)


def resolve_cid_prefix(self, cid8: str) -> str | None:

    return self._live_sessions.resolve_cid_prefix(cid8)


async def resolve_owned_cid_prefix(self, cid8: str, owner_id: str) -> str | None:

    return await self._live_sessions.resolve_owned_cid_prefix(cid8, owner_id)


def preview_upstream(self, conversation_id: str) -> str | None:

    port = self._preview.preview_target_port(conversation_id)
    return self._preview.port_upstream(conversation_id, port) if port is not None else None


def preview_target_port(self, conversation_id: str) -> int | None:

    return self._preview.preview_target_port(conversation_id)


async def resolve_active_preview_projection(
    self,
    conversation_id: str,
    projection: Any,
) -> bool:

    return await self._preview.resolve_active_preview_projection(conversation_id, projection)


async def resolve_finished_preview_runtime(
    self,
    conversation_id: str,
    contract: Any,
) -> dict[str, Any] | None:

    return await self._preview.resolve_finished_preview_runtime(conversation_id, contract)


def port_upstream(self, conversation_id: str, port: int) -> str | None:

    return self._preview.port_upstream(conversation_id, port)


async def wake_for_preview(
    self, cid8: str, port: int, *, owner_id: str = DEFAULT_OWNER_ID
) -> str | None:

    return await self._preview.wake_for_preview(cid8, port, owner_id=owner_id)


def live_session(self, conversation_id: str) -> SandboxSession | None:

    return self._live_sessions.live_session(conversation_id)


def sandbox_backend_name(self) -> str | None:

    return self._sandbox.backend_name()


async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:

    return await self._sessions.sessions_snapshot(conversation_id)


async def sessions_list(self, conversation_id: str) -> list[SessionInfo]:

    return (await self._sessions.sessions_snapshot(conversation_id))[0]


async def session_view(
    self, conversation_id: str, name: str, tail_chars: int
) -> SessionView | None:

    return await self._sessions.session_view(conversation_id, name, tail_chars)


async def preview(self, conversation_id: str) -> dict[str, Any]:

    return await self._preview.preview(conversation_id)


async def ensure_preview(self, conversation_id: str) -> bool:

    return await self._preview.ensure_preview(conversation_id)


async def resume_conversation(self, conversation_id: str) -> dict:
    # CONTRACT-DURABILITY: a resume after an agent-server restart is exactly
    # the path that used to lose the contract — fold BEFORE the resume seam
    # composes a loop so the executor bakes contract-derived state.

    return await self._conversation_control.resume_conversation(conversation_id)


async def forget_conversation(self, conversation_id: str) -> None:

    await self._workspace.forget(conversation_id)


def running_conversation_ids(self) -> set[str]:

    return set(self._run_registry.active_conversation_ids())


def _install_schedule_compatibility(runtime_cls: type[ConversationRuntime]) -> None:
    """Bridge PKG-11-WORKFLOWS migration; PKG-13-FACADES removes these delegates."""
    runtime_cls.running_conversation_ids = running_conversation_ids


def install_runtime_compatibility(runtime_cls: type[ConversationRuntime]) -> None:
    """Install explicit delegates without a dynamic attribute/service-locator seam."""

    runtime_cls.set_surface = set_surface

    runtime_cls.probe_active_sandbox = probe_active_sandbox

    runtime_cls.probe_sandbox_config = probe_sandbox_config

    runtime_cls.prewarm_model_probe = prewarm_model_probe

    runtime_cls.prewarm_vision_probe = prewarm_vision_probe

    runtime_cls.set_model_override = set_model_override

    runtime_cls.set_autonomous = set_autonomous

    runtime_cls.is_autonomous = is_autonomous

    runtime_cls.set_quiet = set_quiet

    runtime_cls.is_quiet = is_quiet

    runtime_cls.set_assist = set_assist

    runtime_cls.is_assist = is_assist

    runtime_cls.apply_settings_change = apply_settings_change

    runtime_cls.set_artifact_mode = set_artifact_mode

    runtime_cls.set_appkit_mode = set_appkit_mode

    runtime_cls.set_research_sources = set_research_sources

    runtime_cls.get_research_sources = get_research_sources

    runtime_cls.activate_contract_for_brief = activate_contract_for_brief

    runtime_cls.set_build_kind = set_build_kind

    runtime_cls.expected_delivery_mode = expected_delivery_mode

    runtime_cls.note_build_verify_result = note_build_verify_result

    runtime_cls.get_last_selected_model = get_last_selected_model

    runtime_cls.set_last_selected_model = set_last_selected_model

    runtime_cls.driver_models = driver_models

    runtime_cls.execute_pi_tool = execute_pi_tool

    runtime_cls.upload_session = upload_session

    runtime_cls.workspace_lock = workspace_lock

    runtime_cls.workspace_fence = workspace_fence

    runtime_cls.workspace_mutation = workspace_mutation

    runtime_cls.record_workspace_mutation_locked = record_workspace_mutation_locked

    runtime_cls.finalize_host_workspace_change = finalize_host_workspace_change

    runtime_cls.finalize_host_mirror_change_locked = finalize_host_mirror_change_locked

    runtime_cls.require_committed_host_mirror_locked = require_committed_host_mirror_locked

    runtime_cls.add_upload_passages = add_upload_passages

    runtime_cls.get_upload_passages = get_upload_passages

    runtime_cls.set_depth = set_depth

    runtime_cls.set_iterative = set_iterative

    runtime_cls.set_recency = set_recency

    runtime_cls.research_stream = research_stream

    runtime_cls.kick = kick

    runtime_cls.reconcile_sandbox_backend = reconcile_sandbox_backend

    runtime_cls.reconcile_orphaned_runs = reconcile_orphaned_runs

    runtime_cls.reload_mcp_pool = reload_mcp_pool

    runtime_cls.mcp_approval_state = mcp_approval_state

    runtime_cls.on_connect = on_connect

    runtime_cls.on_disconnect = on_disconnect

    runtime_cls.sandbox_state = sandbox_state

    runtime_cls.sandbox_instance_ids = sandbox_instance_ids

    runtime_cls.sweep_idle_once = sweep_idle_once

    runtime_cls.sweep_abandoned_gates_once = sweep_abandoned_gates_once

    runtime_cls.project_store = project_store

    runtime_cls.restore_workspace_version = restore_workspace_version

    runtime_cls.export_report = export_report

    runtime_cls.resolve_cid_prefix = resolve_cid_prefix

    runtime_cls.resolve_owned_cid_prefix = resolve_owned_cid_prefix

    runtime_cls.preview_upstream = preview_upstream

    runtime_cls.preview_target_port = preview_target_port

    runtime_cls.resolve_active_preview_projection = resolve_active_preview_projection

    runtime_cls.resolve_finished_preview_runtime = resolve_finished_preview_runtime

    runtime_cls.port_upstream = port_upstream

    runtime_cls.wake_for_preview = wake_for_preview

    runtime_cls.live_session = live_session

    runtime_cls.sandbox_backend_name = sandbox_backend_name

    runtime_cls.sessions_snapshot = sessions_snapshot

    runtime_cls.sessions_list = sessions_list

    runtime_cls.session_view = session_view

    runtime_cls.preview = preview

    runtime_cls.ensure_preview = ensure_preview

    runtime_cls.resume_conversation = resume_conversation

    runtime_cls.forget_conversation = forget_conversation

    _install_schedule_compatibility(runtime_cls)
