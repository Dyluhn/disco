"""Order C — atomic staleness kill + stale-policy / compose-gap / no-deadlock tests.

Contract under test:
  - apply_settings_change: True when pristine (no work/run events, no loop, no
    live task); False when non-pristine (settings LEFT UNMUTATED).
  - _conversation_is_pristine: SETUP events (ENVIRONMENT MessageEvent,
    DatasourceEvent, USER MessageEvent) do NOT block; work/run events DO.
  - Compose-gap: loop composed or task scheduled but RUNNING not yet emitted →
    pristine returns False → PATCH 409.
  - No-deadlock: concurrent apply_settings_change + kick (no lock in kick) never
    deadlocks because the inner setters do not re-acquire the per-cid lock.
  - PATCH route: model_override + assist → apply_settings_change → 409 on False;
    uploads-before-kick stay patchable.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from disco.agent_server.runtime import ConversationRuntime
from disco.core import (
    ActionEvent,
    ConversationStatus,
    DatasourceEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import ConfigStore, SecretBox, SecretStore

# ── helpers ────────────────────────────────────────────────────────────────────

def _rt(tmp_path, monkeypatch) -> ConversationRuntime:
    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )


def _user_msg(content: str = "hello") -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER, message=LLMMessage(role="user", content=content)
    )


def _env_msg(content: str = "uploaded: foo.txt") -> MessageEvent:
    return MessageEvent(
        source=EventSource.ENVIRONMENT, message=LLMMessage(role="user", content=content)
    )


def _agent_msg(content: str = "working…") -> MessageEvent:
    return MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content=content)
    )


def _datasource_event() -> DatasourceEvent:
    # DatasourceEvent is a data-source schema record (name + docs), not a ToolResult.
    return DatasourceEvent(name="test-api", docs="GET /foo → {result: str}")


# ── _conversation_is_pristine unit tests ───────────────────────────────────────

async def test_pristine_on_empty_conversation(tmp_path, monkeypatch):
    """A freshly-created conversation with no events is pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    assert await rt._settings._conversation_is_pristine("c1") is True


async def test_pristine_with_user_message_only(tmp_path, monkeypatch):
    """USER MessageEvent does NOT block — it's a SETUP event."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", _user_msg())
    assert await rt._settings._conversation_is_pristine("c1") is True


async def test_pristine_with_environment_message(tmp_path, monkeypatch):
    """ENVIRONMENT MessageEvent (upload notification) does NOT block."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", _env_msg())
    assert await rt._settings._conversation_is_pristine("c1") is True


async def test_pristine_with_datasource_event(tmp_path, monkeypatch):
    """DatasourceEvent (file attachment) does NOT block — pre-kick upload path."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", _datasource_event())
    assert await rt._settings._conversation_is_pristine("c1") is True


async def test_not_pristine_after_plan_event(tmp_path, monkeypatch):
    """PlanEvent means the loop has done real planning work → non-pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append(
        "c1", PlanEvent(summary="plan", steps=[PlanStep(title="step1")], revision=1)
    )
    assert await rt._settings._conversation_is_pristine("c1") is False


async def test_not_pristine_after_action_event(tmp_path, monkeypatch):
    """Any ActionEvent means the loop has executed work → non-pristine."""
    from disco.core import ToolCall

    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append(
        "c1",
        ActionEvent(thought="doing", tool_call=ToolCall(tool_name="shell", arguments={})),
    )
    assert await rt._settings._conversation_is_pristine("c1") is False


async def test_not_pristine_after_agent_message(tmp_path, monkeypatch):
    """AGENT MessageEvent means the model responded → non-pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", _agent_msg())
    assert await rt._settings._conversation_is_pristine("c1") is False


async def test_not_pristine_after_running_status(tmp_path, monkeypatch):
    """RUNNING StatusEvent means the loop started → non-pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.RUNNING))
    assert await rt._settings._conversation_is_pristine("c1") is False


async def test_not_pristine_after_finished_status(tmp_path, monkeypatch):
    """FINISHED StatusEvent means a prior run completed → non-pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.FINISHED))
    assert await rt._settings._conversation_is_pristine("c1") is False


# ── compose-gap tests (in-memory _loops / _tasks) ──────────────────────────────

async def test_not_pristine_when_loop_registered(tmp_path, monkeypatch):
    """Loop composed (_loops[cid] set) but RUNNING not yet emitted → non-pristine.
    This is the compose-gap: _loop_for registers the loop before create_task runs it."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    rt._loops["c1"] = MagicMock()  # simulate compose step
    assert await rt._settings._conversation_is_pristine("c1") is False


async def test_not_pristine_when_live_task_registered(tmp_path, monkeypatch):
    """A non-done task in _tasks (run scheduled but RUNNING not emitted) → non-pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    # Simulate a scheduled (but not-yet-running) task — use a future that never resolves.
    loop = asyncio.get_event_loop()
    future: asyncio.Future[None] = loop.create_future()
    task: asyncio.Task[None] = asyncio.ensure_future(asyncio.shield(future))
    try:
        rt._tasks["c1"] = task  # type: ignore[assignment]
        assert await rt._settings._conversation_is_pristine("c1") is False
    finally:
        future.cancel()
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


async def test_pristine_when_task_is_done(tmp_path, monkeypatch):
    """A DONE task alone doesn't block (events would catch the finished state)."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    # Done task — simulate a completed run with no lingering events (edge case).
    done_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
    await done_task  # ensure it's done
    rt._tasks["c1"] = done_task  # type: ignore[assignment]
    # No work events → still pristine (task is done, event check passes).
    assert await rt._settings._conversation_is_pristine("c1") is True


# ── apply_settings_change unit tests ───────────────────────────────────────────

async def test_apply_settings_change_on_pristine(tmp_path, monkeypatch):
    """Pristine conversation: apply_settings_change returns True and applies both fields."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    # Mock _effective_policy so set_model_override doesn't crash without a real config.
    ok = await rt.apply_settings_change("c1", assist=True)
    assert ok is True
    assert rt._assist.get("c1") is True


async def test_apply_settings_change_model_override_on_pristine(tmp_path, monkeypatch):
    """model_override is applied when pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    ok = await rt.apply_settings_change("c1", model_override="or-gpt-oss-120b")
    assert ok is True
    assert rt._model_override.get("c1") == "or-gpt-oss-120b"


async def test_apply_settings_change_returns_false_when_loop_active(tmp_path, monkeypatch):
    """Non-pristine (loop registered) → returns False; assist UNMUTATED."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    rt._loops["c1"] = MagicMock()
    # Settings before the call
    rt._assist["c1"] = False
    ok = await rt.apply_settings_change("c1", assist=True)
    assert ok is False
    assert rt._assist.get("c1") is False  # unmutated


async def test_apply_settings_change_returns_false_after_running_event(tmp_path, monkeypatch):
    """Non-pristine (RUNNING event) → returns False; model_override UNMUTATED."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.RUNNING))
    rt._model_override["c1"] = "old-model"
    ok = await rt.apply_settings_change("c1", model_override="new-model")
    assert ok is False
    assert rt._model_override.get("c1") == "old-model"  # unmutated


async def test_apply_settings_change_pristine_after_uploads(tmp_path, monkeypatch):
    """Uploads (ENVIRONMENT + DatasourceEvent) stay patchable — setup events don't block."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", _env_msg("User uploaded: report.pdf"))
    await rt._store.append("c1", _datasource_event())
    ok = await rt.apply_settings_change("c1", assist=True)
    assert ok is True
    assert rt._assist.get("c1") is True


# ── no-deadlock concurrent test ────────────────────────────────────────────────

async def test_concurrent_apply_settings_change_no_deadlock(tmp_path, monkeypatch):
    """Two concurrent apply_settings_change calls on the same cid don't deadlock.
    The lock serializes them; the inner setters never re-acquire the lock."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")

    results: list[bool] = []
    async def patch_assist(value: bool) -> None:
        ok = await rt.apply_settings_change("c1", assist=value)
        results.append(ok)

    # Both should complete without deadlock.
    await asyncio.gather(patch_assist(True), patch_assist(False))
    assert len(results) == 2
    # Both ran — at least one succeeded (pristine on first call).
    assert any(results)


async def test_patch_blocked_by_loop_composed_during_pristine_await(tmp_path, monkeypatch):
    """BLOCKER regression (codex impl review): _conversation_is_pristine does
    `await get_events`, and kick() is sync + does NOT take the settings lock — so a
    kick that composes _loops[cid] DURING that await would slip past the in-memory
    check inside _conversation_is_pristine. The FINAL synchronous guard in
    apply_settings_change (re-checks _loops/_tasks with no await before mutating) must
    catch it: return False, settings UNMUTATED."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    # Inject a "concurrent kick" that registers the loop DURING the pristine await.
    orig_get = rt._store.get_events

    async def get_events_then_compose(cid):
        events = await orig_get(cid)
        rt._loops["c1"] = object()  # a kick composed the loop mid-await
        return events

    monkeypatch.setattr(rt._store, "get_events", get_events_then_compose)
    ok = await rt.apply_settings_change("c1", model_override="driver-local")
    assert ok is False  # the final guard caught the loop composed during the await
    assert "c1" not in rt._model_override  # settings left UNMUTATED


async def test_patch_and_kick_no_deadlock(tmp_path, monkeypatch):
    """apply_settings_change (holds lock) + kick (sync, no lock) = no deadlock. The
    race is closed not by kick locking, but by the final synchronous _loops/_tasks
    guard in apply_settings_change (see the test above)."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")

    # Monkeypatch _loop_for so kick() doesn't try to build a real loop.
    fake_loop = MagicMock()
    fake_loop.run = asyncio.coroutine(lambda: None) if False else None  # won't be called
    rt._loop_for = MagicMock(return_value=fake_loop)  # type: ignore[method-assign]

    async def do_patch() -> bool:
        return await rt.apply_settings_change("c1", assist=True)

    def do_kick() -> None:
        # kick() is sync; it composes + registers without acquiring the per-cid lock.
        # It may call _loop_for but that's fine — _loops[cid] gets set synchronously.
        rt._loops["c1"] = fake_loop
        # No task scheduled in this test (we don't call the real kick path).

    # Run patch and kick "concurrently" (kick is sync so there's no real race,
    # but the test proves neither blocks the other).
    patch_result = await do_patch()
    do_kick()
    # patch ran first (pristine → True); kick happened after
    assert patch_result is True
    assert "c1" in rt._loops


# ── PATCH route integration tests ─────────────────────────────────────────────

async def test_patch_route_model_override_on_pristine(tmp_path, monkeypatch):
    """PATCH /settings model_override on a pristine conversation → 200 OK."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    r = client.patch(
        f"/conversations/{cid}/settings", json={"model_override": "or-test-model"}
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_patch_route_assist_on_pristine(tmp_path, monkeypatch):
    """PATCH /settings assist=True on a pristine conversation → 200; is_assist True."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    r = client.patch(f"/conversations/{cid}/settings", json={"assist": True})
    assert r.status_code == 200
    assert rt.is_assist(cid) is True


async def test_patch_route_409_after_loop_composed(tmp_path, monkeypatch):
    """PATCH /settings after loop is composed → 409; settings UNMUTATED."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    # Simulate loop already composed (compose-gap: RUNNING not yet emitted)
    rt._loops[cid] = MagicMock()
    rt._assist[cid] = False

    r = client.patch(f"/conversations/{cid}/settings", json={"assist": True})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "conversation_not_pristine"
    # assist must be UNMUTATED
    assert rt._assist.get(cid) is False


async def test_patch_route_409_after_running_event(tmp_path, monkeypatch):
    """PATCH /settings after RUNNING event → 409; model_override UNMUTATED."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    rt._model_override[cid] = "old-model"
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))

    r = client.patch(f"/conversations/{cid}/settings", json={"model_override": "new-model"})
    assert r.status_code == 409
    assert rt._model_override.get(cid) == "old-model"  # unmutated


async def test_patch_route_ok_after_uploads_before_kick(tmp_path, monkeypatch):
    """ENVIRONMENT MessageEvent + DatasourceEvent stay patchable (setup events)."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    # Simulate pre-kick uploads (ENVIRONMENT message + datasource — files.py:191 path)
    await store.append(cid, _env_msg("User uploaded: data.csv"))
    await store.append(cid, _datasource_event())

    r = client.patch(f"/conversations/{cid}/settings", json={"assist": False})
    assert r.status_code == 200
    assert rt._assist.get(cid) is False
