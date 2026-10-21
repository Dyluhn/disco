"""Sealed-run share import: a full completed Agent bundle must import AND
re-export through the read-only path — a friendly 422 alone is not success.

Covers the live-caught failure where importing a version-1 bundle containing
a completed Agent's events returned 500 (`final workspace seal scope must
match the persisted conversation`): the foreign seal is quarantined (never
re-scoped or re-signed), the store validator is untouched, and a failed
import leaves no partial conversation.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    FinalWorkspaceSeal,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ResourceKey,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
)
from fastapi.testclient import TestClient

_DIGEST = "a" * 64


@pytest.fixture
def client() -> TestClient:
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    c = TestClient(create_app(store, runtime=runtime))
    c._store = store  # type: ignore[attr-defined]
    return c


def _seed_sealed(store: SqliteEventStore, cid: str) -> None:
    """A completed Agent run: user msg → action → paired observation →
    FINISHED → finish-triggered WorkspaceVersionEvent carrying a valid seal."""

    async def go() -> None:
        store.create_conversation(cid, owner_id="local", title="sealed run", surface="agent")
        await store.append(
            cid,
            MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hi")),
        )
        act = await store.append(
            cid,
            ActionEvent(
                source=EventSource.AGENT,
                thought="writing the file",
                tool_call=ToolCall(tool_name="file_write", arguments={"path": "a.txt"}),
            ),
        )
        await store.append(
            cid,
            ObservationEvent(
                action_id=act.id,
                tool_result=ToolResult(
                    call_id="c1", tool_name="file_write", success=True, content="wrote"
                ),
            ),
        )
        await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
        terminal_seq, latest_effect_seq = derive_final_workspace_fence(await store.get_events(cid))
        await store.append(
            cid,
            WorkspaceVersionEvent(
                version_seq=1,
                tree_digest=_DIGEST,
                trigger="finish",
                final_seal=FinalWorkspaceSeal(
                    scope=ResourceKey(namespace="workspace.tree", identifier=cid),
                    terminal_seq=terminal_seq,
                    latest_effect_seq=latest_effect_seq,
                    version_seq=1,
                    tree_digest=_DIGEST,
                    file_count=1,
                    total_bytes=5,
                ),
            ),
        )

    asyncio.run(go())


def _export(client: TestClient, cid: str) -> dict:
    r = client.get(f"/api/conversations/{cid}/share/bundle")
    assert r.status_code == 200, r.text
    return r.json()


def test_valid_sealed_bundle_imports_and_reexports(client: TestClient) -> None:
    _seed_sealed(client._store, "conv_sealed_src")  # type: ignore[attr-defined]
    bundle = _export(client, "conv_sealed_src")
    assert any(e.get("kind") == "workspace_version" for e in bundle["events"])

    r = client.post("/api/share/import", json=bundle)
    assert r.status_code == 200, r.text
    new_cid = r.json()["conversation_id"]
    assert new_cid != "conv_sealed_src"  # importer-minted

    events = client.get(f"/conversations/{new_cid}/events").json()["events"]
    assert len(events) == len(bundle["events"])
    # The foreign seal is quarantined, never re-scoped as new authority …
    version_events = [e for e in events if e["kind"] == "workspace_version"]
    assert version_events, "the version event must survive the import"
    assert all(e.get("final_seal") is None for e in version_events)
    # … while the original proof is retained in non-semantic meta.
    kept = version_events[0].get("meta", {}).get("imported_final_seal")
    assert kept["scope"]["identifier"] == "conv_sealed_src"
    assert version_events[0]["meta"]["imported_from_conversation"] == "conv_sealed_src"
    assert client._store.conversation_origin(new_cid) == "imported"  # type: ignore[attr-defined]

    # Successful import MUST re-export (the round trip the UI needs).
    reexport = _export(client, new_cid)
    assert len(reexport["events"]) == len(bundle["events"])
    assert "cassette" in reexport
    exported_version = next(e for e in reexport["events"] if e["kind"] == "workspace_version")
    original_version = next(e for e in bundle["events"] if e["kind"] == "workspace_version")
    assert exported_version["meta"]["imported_final_seal"] == original_version["final_seal"]
    assert exported_version["meta"]["imported_from_event_seq"] == original_version["seq"]
    second = client.post("/api/share/import", json=reexport)
    assert second.status_code == 200, second.text
    second_export = _export(client, second.json()["conversation_id"])
    second_version = next(e for e in second_export["events"] if e["kind"] == "workspace_version")
    assert second_version["final_seal"] is None
    assert second_version["meta"] == exported_version["meta"]


def test_imported_sealed_conversation_stays_read_only(client: TestClient) -> None:
    _seed_sealed(client._store, "conv_sealed_src")  # type: ignore[attr-defined]
    cid = client.post("/api/share/import", json=_export(client, "conv_sealed_src")).json()[
        "conversation_id"
    ]
    for path, kwargs in (
        (f"/conversations/{cid}/messages", {"json": {"content": "go"}}),
        (f"/conversations/{cid}/followup", {"json": {"content": "go"}}),
        (f"/conversations/{cid}/resume", {}),
    ):
        resp = client.post(path, **kwargs)
        assert resp.status_code == 409, (path, resp.text)
        assert resp.json()["detail"]["reason"] == "imported_read_only"


def test_tampered_live_seal_still_rejected(client: TestClient) -> None:
    """The validator is NOT loosened: a live seal scoped to another
    conversation, or with a stale fence, is still refused with zero events."""

    async def go() -> None:
        store = client._store  # type: ignore[attr-defined]
        store.create_conversation("conv_live", owner_id="local", surface="agent")
        await store.append(
            "conv_live",
            MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="work")),
        )
        await store.append("conv_live", StatusEvent(status=ConversationStatus.FINISHED))

    asyncio.run(go())

    async def append_foreign() -> None:
        store = client._store  # type: ignore[attr-defined]
        await store.append(
            "conv_live",
            WorkspaceVersionEvent(
                version_seq=1,
                tree_digest=_DIGEST,
                trigger="finish",
                final_seal=FinalWorkspaceSeal(
                    scope=ResourceKey(namespace="workspace.tree", identifier="conv_other"),
                    terminal_seq=2,
                    latest_effect_seq=None,
                    version_seq=1,
                    tree_digest=_DIGEST,
                    file_count=0,
                    total_bytes=0,
                ),
            ),
        )

    with pytest.raises(ValueError, match="scope must match"):
        asyncio.run(append_foreign())


def test_malformed_sealed_bundle_rejects_without_partial_import(client: TestClient) -> None:
    _seed_sealed(client._store, "conv_sealed_src")  # type: ignore[attr-defined]
    bundle = _export(client, "conv_sealed_src")
    bundle["events"].append({"kind": "not_a_real_kind", "smuggled": "field"})
    before = len(client.get("/conversations?owner_id=local").json()["conversation_ids"])
    r = client.post("/api/share/import", json=bundle)
    assert r.status_code == 422
    after = len(client.get("/conversations?owner_id=local").json()["conversation_ids"])
    assert after == before


def test_oversized_event_rolls_back_without_partial_import(client: TestClient) -> None:
    """Oversized untrusted payload is refused before expensive redaction or writes."""
    _seed_sealed(client._store, "conv_sealed_src")  # type: ignore[attr-defined]
    bundle = _export(client, "conv_sealed_src")
    for e in bundle["events"]:
        if e["kind"] == "message":
            e["message"]["content"] = "x" * (2 * 1024 * 1024)
            break
    before = len(client.get("/conversations?owner_id=local").json()["conversation_ids"])
    r = client.post("/api/share/import", json=bundle)
    assert r.status_code == 422, r.text
    after = len(client.get("/conversations?owner_id=local").json()["conversation_ids"])
    assert after == before


@pytest.mark.parametrize("tamper", ["scope", "terminal", "effect"])
def test_foreign_seal_must_match_source_history_before_quarantine(client, tamper):
    _seed_sealed(client._store, "conv_proof_src")
    bundle = _export(client, "conv_proof_src")
    seal = next(e["final_seal"] for e in bundle["events"] if e["kind"] == "workspace_version")
    if tamper == "scope":
        seal["scope"]["identifier"] = "unrelated-source"
    elif tamper == "terminal":
        seal["terminal_seq"] = 1
    else:
        seal["latest_effect_seq"] = 1
    before = client.get("/conversations?owner_id=local").json()["conversation_ids"]
    response = client.post("/api/share/import", json=bundle)
    assert response.status_code == 422
    assert client.get("/conversations?owner_id=local").json()["conversation_ids"] == before
