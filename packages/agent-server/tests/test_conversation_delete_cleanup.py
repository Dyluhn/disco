"""Conversation delete must release the agent-runtime state (Build Kernel finding #5).

The app-server library delete removed only the DB rows, so the agent-server process
leaked the per-conversation runtime caches — most importantly the kernel PIN
(`_pinned_kernels`) — until process exit. These tests cover the cleanup path:

  * `ConversationRuntime.forget_conversation` drops the pin + the per-cid caches;
  * the agent-server DELETE route runs that cleanup AND deletes the rows;
  * the app-server delete best-effort NOTIFIES the agent-server when configured.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import DefaultLLMRouter, ModelEntry, ModelRole, RouterConfig
from disco.tools import ProcessSandboxService

CID = "conv_delete_cleanup"


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {})
    return ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


async def test_forget_conversation_clears_pin_and_caches() -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    # Seed per-conversation runtime state the way a live run would.
    rt._kernel_pin_store.set(CID, rt._disco_kernel)
    rt._run_registry._generations[CID] = 3
    rt._run_recovery._post_terminal_rekick_seq[CID] = 7
    rt._run_ingress._claimed_user_seqs[CID] = 7
    rt.set_surface(CID, "build")
    rt.set_autonomous(CID, True)
    rt._driver_preflight._proven.add((CID, ModelRole.AGENT_DRIVER, "m"))
    rt._driver_preflight._proven.add(("another-conversation", ModelRole.AGENT_DRIVER, "m"))
    rt._driver_preflight._ok["m"] = 123.0

    await rt.workspace.forget(CID)

    assert rt._kernel_pin_store.current(CID) is None  # the leak the finding cites
    assert CID not in rt._run_registry._generations
    assert CID not in rt._run_recovery._post_terminal_rekick_seq
    assert CID not in rt._run_ingress._claimed_user_seqs
    assert rt._settings._surface_of(CID) != "build"
    assert CID not in rt._settings._autonomous
    assert not any(proven[0] == CID for proven in rt._driver_preflight._proven)
    assert (
        "another-conversation",
        ModelRole.AGENT_DRIVER,
        "m",
    ) in rt._driver_preflight._proven
    # The success TTL is model-scoped and remains reusable by other conversations.
    assert rt._driver_preflight._ok["m"] == 123.0


async def test_forget_conversation_is_idempotent_on_unknown_cid() -> None:
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    await rt.workspace.forget("never-existed")  # must not raise


async def test_forget_conversation_reconciles_backend_after_runtime_restart() -> None:
    """Deleting a finished conversation after Agent restart has no cached executor or
    pending session to destroy.  The runtime must still ask the active backend to remove
    exact-conversation resources; otherwise process-backend tmux preview sessions retain
    listeners indefinitely and eventually exhaust the fixed preview-port pool."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    service = rt.sandbox._injected_service
    assert isinstance(service, ProcessSandboxService)
    service.destroy_by_conversation = AsyncMock()

    assert not rt._run_resources.has_executor(CID)
    assert not rt._run_resources.has_pending_session(CID)
    await rt.workspace.forget(CID)

    service.destroy_by_conversation.assert_awaited_once_with(CID)


async def test_agent_delete_route_clears_pin_rows_and_audio_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt._kernel_pin_store.set(CID, rt._disco_kernel)
    audio_dir = tmp_path / "cache" / "tts" / CID
    audio_dir.mkdir(parents=True)
    (audio_dir / "audio_overview_single_deadbeef.mp3").write_bytes(b"generated")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))

    app = create_app(store, runtime=rt)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/conversations/{CID}", params={"owner_id": "local"})

    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert rt._kernel_pin_store.current(CID) is None  # runtime state released on delete
    assert await store.list_conversations(owner_id="local") == []  # rows gone
    assert not audio_dir.exists()  # generated report audio cannot leak after deletion


# ---- app-server best-effort notify ------------------------------------------


async def test_app_delete_notifies_agent_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """With `DISCO_AGENT_BASE` set, the app-server delete fires a best-effort DELETE at
    the agent-server so it releases the runtime state for the deleted cid."""
    from disco.app_server.routes import conversations as appconv

    monkeypatch.setenv("DISCO_AGENT_BASE", "http://agent.test")
    seen: dict[str, str] = {}

    async def _fake_delete(self, url, *, params=None):  # noqa: ANN001
        seen["url"] = url
        seen["owner"] = (params or {}).get("owner_id", "")
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "delete", _fake_delete)
    await appconv._notify_agent_delete(CID, "local")

    assert seen["url"] == f"http://agent.test/conversations/{CID}"
    assert seen["owner"] == "local"


async def test_app_delete_notify_is_noop_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unconfigured (`DISCO_AGENT_BASE` unset) → no HTTP call, no error (graceful)."""
    from disco.app_server.routes import conversations as appconv

    monkeypatch.delenv("DISCO_AGENT_BASE", raising=False)
    monkeypatch.delenv("PMX_AGENT_BASE", raising=False)
    called = False

    async def _boom(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "delete", _boom)
    await appconv._notify_agent_delete(CID, "local")
    assert called is False  # no agent URL → never reaches the network


async def test_app_delete_notify_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A notify failure must NEVER propagate (the DB rows are already deleted)."""
    from disco.app_server.routes import conversations as appconv

    monkeypatch.setenv("DISCO_AGENT_BASE", "http://agent.test")

    async def _raise(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
        raise httpx.ConnectError("agent down")

    monkeypatch.setattr(httpx.AsyncClient, "delete", _raise)
    await appconv._notify_agent_delete(CID, "local")  # must not raise
