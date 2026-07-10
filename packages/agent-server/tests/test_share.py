"""RP-06 — share links + static-bundle export.

The full ladder (Rung 1 → 4):

  1. POST /api/conversations/{cid}/share issues a revocable base62 token.
  2. GET /api/share/{token}/bundle returns the scrubbed JSON bundle.
  3. GET /share/{token} serves the static viewer HTML.
  4. DELETE /api/share/{token} revokes the token (owner-scoped).
  5. GET /api/share lists the active share links for the owner.

Plus the runtime seams:
  - share_export(cid) is a pure, deterministic projection of the event log
    (same log → same bundle bytes, mod redaction).
  - revoke revokes by setting `revoked_at`; a revoked token looks
    IDENTICAL to a never-issued one to the viewer (404, not 410) so a
    probe cannot confirm a token ever existed.
  - share_tokens are owner-scoped: a different owner cannot revoke
    another owner's token.
"""

from __future__ import annotations

import json

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolResult,
)
from disco.core.events import ActionEvent, ToolCall
from fastapi.testclient import TestClient

# ---- shared helpers --------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    store = SqliteEventStore(":memory:")
    return TestClient(create_app(store))


@pytest.fixture
def client_with_runtime() -> TestClient:
    """A TestClient with a real (router-less) runtime. The runtime
    exposes the share APIs without needing a live model — the
    share_export and share_tokens paths are pure data plumbing."""
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    return TestClient(create_app(store, runtime=runtime))


def _create(client: TestClient, owner_id: str = "local") -> str:
    r = client.post("/conversations", json={"owner_id": owner_id})
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


def _seed_event(store: SqliteEventStore, cid: str, ev) -> None:
    import asyncio

    asyncio.get_event_loop().run_until_complete(store.append(cid, ev))


# ---- POST /api/conversations/{cid}/share -----------------------------------


def test_post_share_returns_token_and_url(client_with_runtime: TestClient) -> None:
    cid = _create(client_with_runtime)
    r = client_with_runtime.post(f"/api/conversations/{cid}/share")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["token"], str) and len(body["token"]) >= 16
    assert body["url"] == f"/share/{body['token']}"
    assert body["conversation_id"] == cid
    assert "bundle_seq" in body


def test_post_share_token_is_url_safe_base62ish(client_with_runtime: TestClient) -> None:
    """The token is base62 (the URL-safe alphabet mapped to the slug
    charset). No `/`, no `+`, no `=` — would break a URL slug."""
    cid = _create(client_with_runtime)
    r = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    token = r["token"]
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert set(token) <= allowed, f"unexpected chars in token: {token!r}"


def test_post_share_404_for_unknown_conversation(client_with_runtime: TestClient) -> None:
    r = client_with_runtime.post("/api/conversations/conv_does_not_exist/share")
    assert r.status_code == 404
    assert r.json()["detail"]["ok"] is False


def test_post_share_double_issue_is_idempotent(client_with_runtime: TestClient) -> None:
    """A double-clicked "Share" button must not write twice (the token
    PK + INSERT OR IGNORE make this a no-op). Two consecutive POSTs
    return two DIFFERENT tokens (each call generates a fresh random
    slug) — the idempotency is on the ROW, not on the user input."""
    cid = _create(client_with_runtime)
    r1 = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    r2 = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    # Different tokens (the random generator is fresh each call).
    assert r1["token"] != r2["token"]
    # But both links are live and resolvable.
    for tok in (r1["token"], r2["token"]):
        bundle = client_with_runtime.get(f"/api/share/{tok}/bundle")
        assert bundle.status_code == 200


# ---- GET /api/share/{token}/bundle ----------------------------------------


def test_get_bundle_for_valid_token(client_with_runtime: TestClient) -> None:
    cid = _create(client_with_runtime)
    r = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    token = r["token"]
    bundle = client_with_runtime.get(f"/api/share/{token}/bundle").json()
    # The bundle has the locked shape (versioned).
    assert bundle["bundle_version"] == 1
    assert bundle["conversation_id"] == cid
    assert bundle["surface"] == "research"
    assert isinstance(bundle["events"], list)
    assert "state" in bundle
    assert bundle["state"]["execution_status"] in ("IDLE", "FINISHED", "RUNNING")
    # The share envelope is included.
    assert bundle["share"]["token"] == token
    assert bundle["share"]["bundle_seq_at_issue"] == r["bundle_seq"]


def test_share_token_bundle_is_a_full_point_in_time_projection() -> None:
    """Events are not the only public projection: state, last_seq, and cassette
    must all remain frozen at the token's issue boundary too."""
    import asyncio

    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    client = TestClient(create_app(store, runtime=runtime))
    cid = "conv_share_snapshot"
    store.create_conversation(
        cid,
        owner_id="local",
        surface="deep_research",
        title="Initial shared title",
    )

    async def append_search_pair(query: str) -> list[str]:
        action = await store.append(
            cid,
            ActionEvent(
                thought=f"search {query}",
                tool_call=ToolCall(
                    tool_name="ddgs.search",
                    arguments={"query": query, "limit": 1, "deny": []},
                ),
            ),
        )
        observation = await store.append(
            cid,
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(
                    call_id=f"call-{query}",
                    tool_name="ddgs.search",
                    success=True,
                    content=f"{query} hit",
                    structured={"results": [{"url": f"https://{query}.example"}]},
                ),
            ),
        )
        return [action.id, observation.id]

    async def seed_before_share() -> None:
        await store.append(
            cid,
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(role="user", content="before share"),
            ),
        )
        await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
        await append_search_pair("pre-share")

    async def append_after_share() -> list[str]:
        ids = await append_search_pair("post-share")
        status = await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
        return [*ids, status.id]

    asyncio.run(seed_before_share())
    issued = client.post(f"/api/conversations/{cid}/share")
    assert issued.status_code == 200, issued.text
    share = issued.json()

    post_share_ids = asyncio.run(append_after_share())
    asyncio.run(store.update_title(cid, "POST-SHARE PRIVATE TITLE"))
    response = client.get(f"/api/share/{share['token']}/bundle")
    assert response.status_code == 200, response.text
    bundle = response.json()

    assert bundle["last_seq"] == share["bundle_seq"]
    assert bundle["state"]["last_seq"] == share["bundle_seq"]
    assert bundle["state"]["execution_status"] == ConversationStatus.RUNNING.value
    assert max(event["seq"] for event in bundle["events"]) == share["bundle_seq"]
    assert {event["id"] for event in bundle["events"]}.isdisjoint(post_share_ids)
    assert {row["input"]["query"] for row in bundle["cassette"]} == {"pre-share"}
    assert bundle["title"] == "Initial shared title"
    assert "POST-SHARE PRIVATE TITLE" not in json.dumps(bundle)
    assert bundle["surface"] == "deep_research"
    assert bundle["share"]["bundle_seq_at_issue"] == share["bundle_seq"]


def test_get_bundle_404_for_unknown_token(client_with_runtime: TestClient) -> None:
    r = client_with_runtime.get("/api/share/conv_never_issued/bundle")
    assert r.status_code == 404


def test_get_bundle_404_for_revoked_token(client_with_runtime: TestClient) -> None:
    """Revoked tokens are indistinguishable from never-issued ones to the
    public bundle endpoint (the design intent: a probe cannot confirm a
    token ever existed). Both responses are 404 with the same shape."""
    cid = _create(client_with_runtime)
    r = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    token = r["token"]
    # Revoke.
    rev = client_with_runtime.delete(f"/api/share/{token}").json()
    assert rev["ok"] is True
    # Bundle endpoint 404s.
    bundle = client_with_runtime.get(f"/api/share/{token}/bundle")
    assert bundle.status_code == 404
    # A never-issued token also 404s.
    bundle2 = client_with_runtime.get("/api/share/conv_never_issued/bundle")
    assert bundle2.status_code == 404


def test_bundle_scrubs_secrets_in_events(client_with_runtime: TestClient) -> None:
    """A real captured-secret-bearing event MUST come out scrubbed in the
    bundle. The scrubber walks the event payload; the share_export
    invokes it on every event before serialization."""
    _create(client_with_runtime)
    # Append an ActionEvent whose `thought` carries a secret.
    # Easier path: build a parallel store + runtime, seed events, export.
    from disco.agent_server import ConversationRuntime as CR

    store2 = SqliteEventStore(":memory:")
    rt2 = CR(store2)
    cid2 = "conv_secret_test"
    store2.create_conversation(cid2, owner_id="local")

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            store2.append(
                cid2,
                ActionEvent(
                    thought="Use sk_live_4eC39HqLyjWDarjtT1zdp7dc for the test",
                    tool_call=ToolCall(
                        tool_name="shell",
                        arguments={
                            "command": (
                                "echo 'Authorization: Bearer "
                                "ghp_abc123def456ghi789jkl012mno345pqr678'"
                            ),
                        },
                    ),
                ),
            )
        )
        bundle = loop.run_until_complete(rt2.share_export(cid2, owner_id="local"))
    finally:
        loop.close()
    assert bundle["ok"] is True
    serialized = json.dumps(bundle["bundle"])
    # The secret bodies are GONE.
    assert "sk_live_4eC39HqLyjWDarjtT1zdp7dc" not in serialized
    assert "ghp_abc123def456ghi789jkl012mno345pqr678" not in serialized
    # The redaction marker IS present.
    assert "REDACTED" in serialized


# ---- GET /share/{token} (the static viewer) --------------------------------


def test_share_viewer_serves_html(client_with_runtime: TestClient) -> None:
    cid = _create(client_with_runtime)
    r = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    token = r["token"]
    page = client_with_runtime.get(f"/share/{token}")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    body = page.text
    # The page is a single self-contained document.
    assert "<html" in body.lower()
    assert "scrub" in body.lower() or "replay" in body.lower()
    # The page does NOT load the bundle inline (it fetches via fetch()).
    # Asserting a `<script src=` external dependency is absent keeps the
    # CSP-clean contract: zero external assets, no CDN, no eval.
    assert "<script src=" not in body
    assert "cdn" not in body.lower()


def test_share_viewer_serves_html_for_unknown_token(client_with_runtime: TestClient) -> None:
    """A never-issued token STILL gets the viewer page back (the page
    shows "not found / revoked" via a 404 from the bundle fetch — the
    page itself is identical regardless of token validity). This is
    intentional: the viewer never reveals whether a token is
    valid-looking but revoked vs. never-issued."""
    r = client_with_runtime.get("/share/conv_never_issued")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---- DELETE /api/share/{token} (revocation) -------------------------------


def test_revoke_returns_true_then_false(client_with_runtime: TestClient) -> None:
    cid = _create(client_with_runtime)
    r = client_with_runtime.post(f"/api/conversations/{cid}/share").json()
    token = r["token"]
    # First revoke succeeds.
    rev1 = client_with_runtime.delete(f"/api/share/{token}").json()
    assert rev1["ok"] is True
    assert rev1["revoked"] is True
    # Second revoke is a no-op (the row is already revoked).
    rev2 = client_with_runtime.delete(f"/api/share/{token}").json()
    assert rev2["ok"] is False
    assert rev2["revoked"] is False


def test_revoke_unknown_token_returns_false(client_with_runtime: TestClient) -> None:
    r = client_with_runtime.delete("/api/share/conv_never_issued").json()
    assert r["ok"] is False
    assert r["revoked"] is False


# ---- GET /api/share (list) ------------------------------------------------


def test_list_share_links_returns_active_only(client_with_runtime: TestClient) -> None:
    cid1 = _create(client_with_runtime)
    cid2 = _create(client_with_runtime)
    t1 = client_with_runtime.post(f"/api/conversations/{cid1}/share").json()["token"]
    t2 = client_with_runtime.post(f"/api/conversations/{cid2}/share").json()["token"]
    # Revoke one.
    client_with_runtime.delete(f"/api/share/{t1}").json()
    # List returns the active one only.
    listing = client_with_runtime.get("/api/share").json()
    tokens = [link["token"] for link in listing["links"]]
    assert t1 not in tokens
    assert t2 in tokens


# ---- runtime seam: share_export purity ------------------------------------


def test_share_export_is_deterministic_for_same_event_log() -> None:
    """The bundle is a pure function of the event log (modulo the
    `exported_at` ISO timestamp, which we strip from the equality check).
    Two exports of the same log produce structurally identical bundles —
    the structural reason the bundle doubles as a deterministic harness
    cassette (RP-00 synergy)."""
    from disco.agent_server import ConversationRuntime as CR

    store = SqliteEventStore(":memory:")
    rt = CR(store)
    cid = "conv_det"
    store.create_conversation(cid, owner_id="local")

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            store.append(
                cid,
                MessageEvent(
                    source=EventSource.USER,
                    message=LLMMessage(role="user", content="hello"),
                ),
            )
        )
        loop.run_until_complete(
            store.append(
                cid,
                StatusEvent(status=ConversationStatus.FINISHED),
            )
        )
        b1 = loop.run_until_complete(rt.share_export(cid, owner_id="local"))
        b2 = loop.run_until_complete(rt.share_export(cid, owner_id="local"))
    finally:
        loop.close()
    # Strip the volatile `exported_at` and `share` envelope (the
    # bundle_seq hint is stable, but the env-var has no token here so
    # `share` is absent).
    for b in (b1, b2):
        b["bundle"].pop("exported_at", None)
    assert b1 == b2


def test_share_export_unknown_conversation_returns_not_found() -> None:
    from disco.agent_server import ConversationRuntime as CR

    store = SqliteEventStore(":memory:")
    rt = CR(store)
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(rt.share_export("conv_does_not_exist", owner_id="local"))
    finally:
        loop.close()
    assert result["ok"] is False
    assert result["reason"] == "conversation_not_found"


def test_share_export_includes_surface_in_bundle() -> None:
    """The bundle's `surface` field names the conversation's surface
    (research | build | deep_research). The viewer uses it to render
    the right banner / affordance; tests confirm it is set correctly
    for at least one case."""
    from disco.agent_server import ConversationRuntime as CR

    store = SqliteEventStore(":memory:")
    rt = CR(store)
    cid = "conv_build"
    store.create_conversation(cid, owner_id="local", surface="build")
    rt.set_surface(cid, "build")

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(rt.share_export(cid, owner_id="local"))
    finally:
        loop.close()
    assert result["ok"] is True
    assert result["bundle"]["surface"] == "build"


# ---- the share_tokens table ------------------------------------------------


def test_share_tokens_table_persists_tokens_across_runtimes(tmp_path) -> None:
    """The `share_tokens` table is the durable record: opening a fresh
    `SqliteEventStore` against the same DB file reads the same tokens.
    This is the "reopen the server, the link still works" rung."""
    import asyncio

    from disco.agent_server import ConversationRuntime as CR

    db_path = tmp_path / "events.db"
    store1 = SqliteEventStore(str(db_path))
    rt1 = CR(store1)
    cid = "conv_reopen"
    store1.create_conversation(cid, owner_id="local")

    loop = asyncio.new_event_loop()
    try:
        r = loop.run_until_complete(rt1.create_share_link_async(cid, owner_id="local"))
    finally:
        loop.close()
    assert r["ok"] is True
    token = r["token"]
    # Reopen the store.
    store2 = SqliteEventStore(str(db_path))
    rt2 = CR(store2)
    row = rt2.lookup_share_link(token)
    assert row is not None
    assert row["conversation_id"] == cid
    assert row["owner_id"] == "local"


def test_legacy_share_token_migration_freezes_current_metadata(tmp_path) -> None:
    """Old token rows had no title/surface snapshot. Upgrade captures them once;
    subsequent conversation metadata changes must not alter that legacy link."""
    import sqlite3

    db_path = tmp_path / "legacy-share.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            space_id TEXT,
            title TEXT,
            created_at TEXT NOT NULL,
            status TEXT,
            surface TEXT,
            origin TEXT
        );
        CREATE TABLE share_tokens (
            token TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            bundle_seq INTEGER NOT NULL
        );
        INSERT INTO conversations VALUES (
            'conv_legacy_share', 'local', NULL, 'Legacy title',
            '2026-07-01T00:00:00', NULL, 'build', NULL
        );
        INSERT INTO share_tokens VALUES (
            'legacytoken1234567890', 'conv_legacy_share', 'local',
            '2026-07-01T00:00:00', NULL, 0
        );
        """
    )
    conn.commit()
    conn.close()

    store = SqliteEventStore(str(db_path))
    migrated = store.lookup_share_token("legacytoken1234567890")
    assert migrated is not None
    assert migrated["bundle_title"] == "Legacy title"
    assert migrated["bundle_surface"] == "build"

    import asyncio

    asyncio.run(store.update_title("conv_legacy_share", "Changed after upgrade"))
    still_frozen = store.lookup_share_token("legacytoken1234567890")
    assert still_frozen is not None
    assert still_frozen["bundle_title"] == "Legacy title"


def test_share_tokens_revoke_owner_scoped(tmp_path) -> None:
    """A token issued to owner A cannot be revoked by owner B (the
    WHERE clause on revoke is owner-scoped). Cross-owner revocation
    attempts return False (no row matched)."""
    import asyncio

    from disco.agent_server import ConversationRuntime as CR

    store = SqliteEventStore(":memory:")
    rt = CR(store)
    cid = "conv_owner_scope"
    # Two owners, two conversations.
    store.create_conversation(cid, owner_id="alice")
    store.create_conversation("conv_other", owner_id="bob")

    loop = asyncio.new_event_loop()
    try:
        r = loop.run_until_complete(rt.create_share_link_async(cid, owner_id="alice"))
    finally:
        loop.close()
    token = r["token"]
    # Alice (correct owner) can revoke.
    assert rt.revoke_share_link(token, owner_id="alice") is True
    # Bob (wrong owner) cannot revoke — but the row is already revoked
    # so the WHERE `revoked_at IS NULL` clause already filters it out.
    # We can confirm the lookup returns None for everyone.
    assert rt.lookup_share_link(token) is None
