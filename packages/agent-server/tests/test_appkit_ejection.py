"""Confirmed AppKit-to-Freeform revision lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock
from unittest.mock import MagicMock

import disco.agent_server.build_platform_runtime as build_platform_runtime
import httpx
import pytest
from disco.agent_server.routes.preview import make_preview_router
from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    APPKIT_EJECTION_SOURCE_TRIGGER,
    APPKIT_EJECTION_TARGET_TRIGGER,
    ActionEvent,
    AppKitEjectionEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    WorkspaceMutationEvent,
    current_appkit_ejection,
    current_build_platform_admission,
)
from disco.core.llm import DefaultLLMRouter, OperatingMode
from disco.core.loop import RouterAgent
from disco.tools import AppKitPhase, AppKitToolExecutor
from disco.tools.appkit_scope import (
    APPKIT_EJECTION_PATH,
    APPKIT_EJECTION_SOURCE_LABEL,
    APPKIT_EJECTION_TARGET_LABEL,
    AppKitEjectionReceipt,
)
from disco.tools.projects import ProjectStore, StorageError
from fastapi import FastAPI


class _MemorySession:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path.lstrip("/")] = data

    async def read_file(self, path: str) -> bytes:
        normalized = path.lstrip("/")
        try:
            return self.files[normalized]
        except KeyError as exc:
            raise FileNotFoundError(normalized) from exc

    async def list_dir(self, path: str) -> list[str]:
        normalized = "" if path in {"", "."} else path.strip("/")
        prefix = f"{normalized}/" if normalized else ""
        children = {
            remainder.split("/", 1)[0]
            for rel in self.files
            if rel.startswith(prefix) and (remainder := rel[len(prefix) :])
        }
        if not children and normalized:
            raise FileNotFoundError(normalized)
        return sorted(children)


class _PreviewRuntime:
    def __init__(self, store: ProjectStore) -> None:
        self._store = store

    async def wake_for_preview(self, cid8: str, port: int) -> None:
        return None

    def live_session(self, conversation_id: str) -> None:
        return None

    def _current_project_store(self) -> ProjectStore:
        return self._store

    @property
    def projects(self) -> SimpleNamespace:
        return SimpleNamespace(current_project_store=self._current_project_store)


def _runtime(store: SqliteEventStore, root: Path) -> ConversationRuntime:
    config = MagicMock()
    config.projects.projects_root = str(root)
    config_store = MagicMock()
    config_store.load.return_value = config
    return ConversationRuntime(store, router=MagicMock(), config_store=config_store)


async def _seed_appkit_attempt(
    runtime: ConversationRuntime,
    conversation_id: str,
) -> tuple[ToolCall, ActionEvent, BuildPlatformAdmissionEvent]:
    store = runtime._store
    intent = await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-message",
            run_protocol_version=1,
        ),
    )
    assert isinstance(intent, WorkspaceMutationEvent)
    async with runtime.workspace.lock(conversation_id):
        async with runtime.workspace.interprocess_mutation_fence(conversation_id):
            await runtime._build_platform.record_route_locked(conversation_id)
    admission = current_build_platform_admission(await store.get_events(conversation_id))
    assert admission is not None and admission.profile_id == "disco.appkit_web@1"
    await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_protocol_version=1,
            run_intent_id=intent.id,
            agent_view_id="view-1",
        ),
    )
    call = ToolCall(
        tool_name="request_custom_build",
        arguments={
            "reason": "the requested capability requires custom code",
            "needed_capabilities": ["shell", "file_write"],
        },
    )
    action = await store.append(
        conversation_id,
        ActionEvent(
            agent_view_id="view-1",
            thought="request the governed profile transition",
            tool_call=call,
        ),
    )
    assert isinstance(action, ActionEvent)
    return call, action, admission


async def test_confirmed_ejection_cuts_previewable_audited_revision_and_restarts_freeform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCO_APPKIT_PLATFORM_ROUTE", raising=False)
    conversation_id = "conv_appkit_ejection"
    store = SqliteEventStore(":memory:")
    store.create_conversation(
        conversation_id,
        owner_id="local",
        surface="agent",
        appkit_mode=True,
    )
    runtime = _runtime(store, tmp_path)
    session = _MemorySession({"index.html": b"<h1>governed AppKit revision</h1>"})
    router = MagicMock(spec=DefaultLLMRouter)
    agent = MagicMock(spec=RouterAgent)
    with (
        mock.patch.object(runtime.sandbox, "_sandbox_service_now"),
        mock.patch.object(runtime._settings, "_effective_driver_endpoint", return_value=None),
    ):
        loop = runtime._compose_build_loop(
            conversation_id,
            router,
            agent,
            driver_context_window=8192,
        )
    assert isinstance(loop.executor, AppKitToolExecutor)
    runtime._loop_registry.bind(conversation_id, loop)
    loop.mode = OperatingMode.LONG_HORIZON
    loop.executor.appkit_phase.phase = AppKitPhase.BUILD
    loop.executor._sandbox = session
    runtime._run_resources.set_executor(conversation_id, loop.executor)
    call, action, initial_admission = await _seed_appkit_attempt(runtime, conversation_id)
    assert initial_admission.route == "platform"
    assert initial_admission.composition_digest is not None
    assert initial_admission.run_identity is not None

    # Executing the host callback without persisted confirmation must leave both
    # the workspace and immutable-version history untouched.
    denied = await loop.executor.execute_attributed(call, "view-1")
    assert denied.success is False
    assert "recorded human confirmation" in denied.error
    assert APPKIT_EJECTION_PATH not in session.files
    assert runtime.projects.current_project_store().list_versions(conversation_id) == []

    await store.append(
        conversation_id,
        StatusEvent(
            status=ConversationStatus.WAITING_FOR_CONFIRMATION,
            detail=action.id,
            agent_view_id="view-1",
        ),
    )
    await store.append(
        conversation_id,
        StatusEvent(status=ConversationStatus.RUNNING, agent_view_id="view-1"),
    )

    # Composition/engine failures are preflight failures: they cannot publish
    # either the governed source or the ejected target revision.
    with monkeypatch.context() as failed_engine:
        failed_engine.setattr(
            build_platform_runtime,
            "select_freeform_platform_route",
            MagicMock(side_effect=RuntimeError("replacement engine unavailable")),
        )
        rejected = await loop.executor.execute_attributed(call, "view-1")
    assert rejected.success is False
    assert "replacement engine unavailable" in rejected.error
    assert APPKIT_EJECTION_PATH not in session.files
    assert runtime.projects.current_project_store().list_versions(conversation_id) == []
    assert current_appkit_ejection(await store.get_events(conversation_id)) is None

    result = await loop.executor.execute_attributed(call, "view-1")

    assert result.success is True, result.error
    assert result.structured is not None
    receipt = AppKitEjectionReceipt.model_validate(result.structured["ejection"])

    assert receipt.appkit_verified is False
    assert receipt.lost_guarantees == APPKIT_EJECTION_LOST_GUARANTEES
    assert receipt.preview_version_seq == receipt.ejected_version_seq
    assert receipt.source_version_seq != receipt.ejected_version_seq

    projects = runtime.projects.current_project_store()
    versions = {record.seq: record for record in projects.list_versions(conversation_id)}
    source = versions[receipt.source_version_seq]
    target = versions[receipt.ejected_version_seq]
    assert source.pinned is True
    assert source.trigger == APPKIT_EJECTION_SOURCE_TRIGGER
    assert source.label == APPKIT_EJECTION_SOURCE_LABEL
    assert target.trigger == APPKIT_EJECTION_TARGET_TRIGGER
    assert target.label == APPKIT_EJECTION_TARGET_LABEL
    with projects.open_verified_version(conversation_id, source.seq) as verified:
        with pytest.raises(StorageError, match="not in the verified version manifest"):
            verified.read_bytes(APPKIT_EJECTION_PATH)
    with projects.open_verified_version(conversation_id, target.seq) as verified:
        manifest = json.loads(verified.read_bytes(APPKIT_EJECTION_PATH))
    assert manifest["appkit_verified"] is False
    assert manifest["source_version_seq"] == source.seq
    assert manifest["source_tree_digest"] == source.tree_digest
    assert tuple(manifest["lost_guarantees"]) == APPKIT_EJECTION_LOST_GUARANTEES

    events = await store.get_events(conversation_id)
    ejection = current_appkit_ejection(events)
    admission = current_build_platform_admission(events)
    assert isinstance(ejection, AppKitEjectionEvent)
    assert ejection.action_id == action.id
    assert ejection.tool_call_id == call.call_id
    assert admission is not None
    assert admission.route == "platform"
    assert admission.profile_id == "disco.freeform_web@1"
    assert admission.transition == "appkit_ejection"
    assert admission.supersedes_admission_id == initial_admission.id
    assert admission.composition_digest != initial_admission.composition_digest
    assert admission.run_identity != initial_admission.run_identity
    assert runtime._settings._effective_appkit_mode(conversation_id) is False

    app = FastAPI()
    app.include_router(make_preview_router(store, cast(Any, _PreviewRuntime(projects))))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        source_preview = await client.get(
            f"/conversations/{conversation_id}/preview-app/?version={source.seq}"
        )
        target_preview = await client.get(
            f"/conversations/{conversation_id}/preview-app/?version={target.seq}"
        )
    assert source_preview.status_code == 200
    assert target_preview.status_code == 200
    assert b"governed AppKit revision" in source_preview.content
    assert b"governed AppKit revision" in target_preview.content

    restarted = _runtime(store, tmp_path)
    await restarted._build_platform.prepare_route_pin(conversation_id)
    assert restarted._settings._effective_appkit_mode(conversation_id) is False
    assert restarted._build_platform.route_pins[conversation_id] == "platform"
