from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from disco.agent_server.routes.conversations import make_conversations_router
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent
from fastapi import FastAPI


class _KillRuntime:
    def __init__(self, store: SqliteEventStore, *, resulting_status: ConversationStatus) -> None:
        self._store = store
        self._resulting_status = resulting_status

    def sandbox_instance_ids(self, conversation_id: str) -> list[str]:
        del conversation_id
        return []

    async def kill(self, conversation_id: str) -> None:
        await self._store.append(
            conversation_id,
            StatusEvent(status=self._resulting_status, detail="test kill"),
        )


def _app(store: SqliteEventStore, runtime: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(make_conversations_router(store, runtime))
    return app


def _lease(store: SqliteEventStore, conversation_id: str) -> int:
    lease = store.acquire_local_preview_lease(
        conversation_id=conversation_id,
        owner_id="local",
        target_port=8000,
        authority_id="live:generation-one",
        now=100,
        expires_at=1_000,
        listener_ports=(19120, 19121),
    )
    assert lease is not None
    return lease.listener_port


async def test_successful_kill_releases_preview_origin_after_runtime_teardown(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "kill-release.sqlite3")
    cid = "conv_kill_release"
    store.create_conversation(cid, owner_id="local")
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    listener_port = _lease(store, cid)

    transport = httpx.ASGITransport(
        app=_app(store, _KillRuntime(store, resulting_status=ConversationStatus.IDLE))
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/conversations/{cid}/kill")

    assert response.status_code == 200
    assert response.json()["state"]["execution_status"] == "IDLE"
    assert store.resolve_local_preview_lease(listener_port, now=101) is None
    store.close()


async def test_superseded_kill_does_not_release_newer_running_preview_origin(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "kill-superseded.sqlite3")
    cid = "conv_kill_superseded"
    store.create_conversation(cid, owner_id="local")
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    listener_port = _lease(store, cid)

    transport = httpx.ASGITransport(
        app=_app(store, _KillRuntime(store, resulting_status=ConversationStatus.RUNNING))
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/conversations/{cid}/kill")

    assert response.status_code == 200
    assert response.json()["state"]["execution_status"] == "RUNNING"
    assert store.resolve_local_preview_lease(listener_port, now=101) is not None
    store.close()


async def test_delete_releases_preview_origin_without_erasing_origin_reset_state(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "delete-release.sqlite3")
    cid = "conv_delete_release"
    store.create_conversation(cid, owner_id="local")
    listener_port = _lease(store, cid)
    assert store.complete_local_preview_storage_reset(
        listener_port,
        authority_id="live:generation-one",
        now=101,
    )

    transport = httpx.ASGITransport(app=_app(store, None))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(f"/conversations/{cid}")

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert store.resolve_local_preview_lease(listener_port, now=102) is None

    replacement = "conv_delete_replacement"
    store.create_conversation(replacement, owner_id="local")
    lease = store.acquire_local_preview_lease(
        conversation_id=replacement,
        owner_id="local",
        target_port=8000,
        authority_id="live:generation-two",
        now=103,
        expires_at=1_000,
        listener_ports=(listener_port,),
    )
    assert lease is not None
    assert lease.storage_reset_required is True
    store.close()
