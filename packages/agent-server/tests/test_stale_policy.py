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
from unittest.mock import AsyncMock, MagicMock

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
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )


def _user_msg(content: str = "hello") -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


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
    rt._loop_registry.bind("c1", MagicMock())  # simulate compose step
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
        rt._run_registry.register_task("c1", task)  # type: ignore[arg-type]
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
    rt._run_registry.register_task("c1", done_task)  # type: ignore[arg-type]
    # No work events → still pristine (task is done, event check passes).
    assert await rt._settings._conversation_is_pristine("c1") is True


# ── apply_settings_change unit tests ───────────────────────────────────────────


async def test_apply_settings_change_on_pristine(tmp_path, monkeypatch):
    """Pristine conversation: apply_settings_change returns True and applies both fields."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    # Mock _effective_policy so set_model_override doesn't crash without a real config.
    ok = await rt._settings.apply_settings_change("c1", assist=True)
    assert ok is True
    assert rt._settings._assist.get("c1") is True


async def test_apply_settings_change_model_override_on_pristine(tmp_path, monkeypatch):
    """model_override is applied when pristine."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    ok = await rt._settings.apply_settings_change(
        "c1",
        model_override="or-gpt-oss-120b",
    )
    assert ok is True
    assert rt._settings.model_binding._model_overrides.get("c1") == "or-gpt-oss-120b"


async def test_apply_settings_change_returns_false_when_loop_active(tmp_path, monkeypatch):
    """Non-pristine (loop registered) → returns False; assist UNMUTATED."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    rt._loop_registry.bind("c1", MagicMock())
    # Settings before the call
    rt._settings._assist["c1"] = False
    ok = await rt._settings.apply_settings_change("c1", assist=True)
    assert ok is False
    assert rt._settings._assist.get("c1") is False  # unmutated


async def test_apply_settings_change_returns_false_after_running_event(tmp_path, monkeypatch):
    """Non-pristine (RUNNING event) → returns False; model_override UNMUTATED."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.RUNNING))
    rt._settings.model_binding._model_overrides["c1"] = "old-model"
    ok = await rt._settings.apply_settings_change("c1", model_override="new-model")
    assert ok is False
    assert rt._settings.model_binding._model_overrides.get("c1") == "old-model"  # unmutated


async def test_apply_settings_change_pristine_after_uploads(tmp_path, monkeypatch):
    """Uploads (ENVIRONMENT + DatasourceEvent) stay patchable — setup events don't block."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", _env_msg("User uploaded: report.pdf"))
    await rt._store.append("c1", _datasource_event())
    ok = await rt._settings.apply_settings_change("c1", assist=True)
    assert ok is True
    assert rt._settings._assist.get("c1") is True


# ── no-deadlock concurrent test ────────────────────────────────────────────────


async def test_concurrent_apply_settings_change_no_deadlock(tmp_path, monkeypatch):
    """Two concurrent apply_settings_change calls on the same cid don't deadlock.
    The lock serializes them; the inner setters never re-acquire the lock."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")

    results: list[bool] = []

    async def patch_assist(value: bool) -> None:
        ok = await rt._settings.apply_settings_change("c1", assist=value)
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
        rt._loop_registry.bind("c1", object())  # type: ignore[arg-type]
        return events

    monkeypatch.setattr(rt._store, "get_events", get_events_then_compose)
    ok = await rt._settings.apply_settings_change(
        "c1",
        model_override="driver-local",
    )
    assert ok is False  # the final guard caught the loop composed during the await
    assert "c1" not in rt._settings.model_binding._model_overrides  # settings left UNMUTATED


async def test_patch_and_kick_no_deadlock(tmp_path, monkeypatch):
    """apply_settings_change (holds lock) + kick (sync, no lock) = no deadlock. The
    race is closed not by kick locking, but by the final synchronous _loops/_tasks
    guard in apply_settings_change (see the test above)."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")

    fake_loop = MagicMock()
    fake_loop.run = asyncio.coroutine(lambda: None) if False else None  # won't be called

    async def do_patch() -> bool:
        return await rt._settings.apply_settings_change("c1", assist=True)

    def do_kick() -> None:
        # kick() is sync; it composes + registers without acquiring the per-cid lock.
        # It may call _loop_for but that's fine — _loops[cid] gets set synchronously.
        rt._loop_registry.bind("c1", fake_loop)
        # No task scheduled in this test (we don't call the real kick path).

    # Run patch and kick "concurrently" (kick is sync so there's no real race,
    # but the test proves neither blocks the other).
    patch_result = await do_patch()
    do_kick()
    # patch ran first (pristine → True); kick happened after
    assert patch_result is True
    assert rt._loop_registry.loop("c1") is fake_loop


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
    r = client.patch(f"/conversations/{cid}/settings", json={"model_override": "or-test-model"})
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
    assert rt._settings.is_assist(cid) is True


async def test_patch_route_deep_research_settings_preserves_upload_cid(tmp_path, monkeypatch):
    """A pristine upload cid accepts the final DR controls before kickoff."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post(
        "/conversations",
        json={"surface": "deep_research", "depth_tier": "quick"},
    ).json()["conversation_id"]
    await store.append(cid, _env_msg())
    await store.append(cid, _datasource_event())

    r = client.patch(
        f"/conversations/{cid}/settings",
        json={
            "depth_tier": "exhaustive",
            "iterative": True,
            "recency_window": "week",
            "sources": ["arxiv", "ddgs"],
        },
    )
    assert r.status_code == 200
    assert rt.deep_research._depth_for(cid).value == "exhaustive"
    assert rt.deep_research._iterative_for(cid) is True
    assert rt.deep_research._recency_for(cid) == "week"
    assert rt._settings.get_research_sources(cid) == ("arxiv", "ddgs")


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
    rt._loop_registry.bind(cid, MagicMock())
    rt._settings._assist[cid] = False

    r = client.patch(f"/conversations/{cid}/settings", json={"assist": True})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "conversation_not_pristine"
    # assist must be UNMUTATED
    assert rt._settings._assist.get(cid) is False


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
    rt._settings.model_binding._model_overrides[cid] = "old-model"
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))

    r = client.patch(f"/conversations/{cid}/settings", json={"model_override": "new-model"})
    assert r.status_code == 409
    assert rt._settings.model_binding._model_overrides.get(cid) == "old-model"  # unmutated


# ── STATE-AWARE gate: terminal-state model swap (errored/finished/stuck/paused) ──


async def _seed_terminal(rt, cid: str, status: ConversationStatus) -> None:
    """A conversation that has done real work and then landed in `status` — the shape
    a build/agent run leaves behind when it errors, finishes, stops, or pauses."""
    from disco.core import ToolCall

    rt._store.create_conversation(cid)
    await rt._store.append(cid, PlanEvent(summary="p", steps=[PlanStep(title="s1")], revision=1))
    await rt._store.append(
        cid, ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    )
    await rt._store.append(cid, StatusEvent(status=status))


async def test_settable_on_terminal_states(tmp_path, monkeypatch):
    """A model swap is ALLOWED in every terminal/parked state (errored/finished/
    stuck/paused) — these are the deliberate "change the model before resuming" cases."""
    for status in (
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
        ConversationStatus.FINISHED,
        ConversationStatus.PAUSED,
    ):
        rt = _rt(tmp_path, monkeypatch)
        await _seed_terminal(rt, "c1", status)
        settable, terminal = await rt._settings._settable_kind("c1")
        assert settable is True, f"{status} must be settable"
        assert terminal is True, f"{status} must be the terminal path"


async def test_not_settable_while_running(tmp_path, monkeypatch):
    """RUNNING is NOT settable — swapping the brain mid-step is incoherent (409)."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.RUNNING))
    settable, _ = await rt._settings._settable_kind("c1")
    assert settable is False


async def test_not_settable_while_live_task(tmp_path, monkeypatch):
    """A live in-flight task disqualifies even a status that reads terminal (the run
    is still draining)."""
    rt = _rt(tmp_path, monkeypatch)
    await _seed_terminal(rt, "c1", ConversationStatus.FINISHED)
    loop = asyncio.get_event_loop()
    future: asyncio.Future[None] = loop.create_future()
    task: asyncio.Task[None] = asyncio.ensure_future(asyncio.shield(future))
    try:
        rt._run_registry.register_task("c1", task)  # type: ignore[arg-type]
        settable, _ = await rt._settings._settable_kind("c1")
        assert settable is False
    finally:
        future.cancel()
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


async def test_not_settable_in_gate_state(tmp_path, monkeypatch):
    """A gate-awaiting state (mid-run, waiting on the user) is NOT settable."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.WAITING_FOR_CONFIRMATION))
    settable, _ = await rt._settings._settable_kind("c1")
    assert settable is False


async def test_settable_idle_with_unfinished_plan(tmp_path, monkeypatch):
    """IDLE-with-an-approved-unfinished-plan (an interrupted run) is settable+terminal."""
    from disco.core import ToolCall

    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    await rt._store.append("c1", PlanEvent(summary="p", steps=[PlanStep(title="s1")], revision=1))
    await rt._store.append(
        "c1", ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    )
    # No FINISHED — left IDLE mid-execution.
    settable, terminal = await rt._settings._settable_kind("c1")
    assert settable is True and terminal is True


async def test_apply_change_on_errored_persists_and_evicts(tmp_path, monkeypatch):
    """The headline fix: model_override on an ERRORED conversation is APPLIED + persisted,
    and the cached loop/executor (bound to the old model) is evicted so the next kick
    re-resolves. The live sandbox is re-parked (preserved), not destroyed."""
    rt = _rt(tmp_path, monkeypatch)
    await _seed_terminal(rt, "c1", ConversationStatus.ERROR)
    rt._settings.model_binding._model_overrides["c1"] = "old-model"
    # Cached loop + executor from the failed run; the executor holds a live sandbox.
    sentinel_sandbox = object()
    fake_executor = MagicMock()
    fake_executor._sandbox = sentinel_sandbox
    fake_executor.sandbox = sentinel_sandbox
    rt._loop_registry.bind("c1", MagicMock())
    rt._run_resources.set_executor("c1", fake_executor)

    ok = await rt._settings.apply_settings_change(
        "c1",
        model_override="new-model",
    )
    assert ok is True
    assert rt._settings.model_binding._model_overrides.get("c1") == "new-model"  # persisted
    assert rt._loop_registry.loop("c1") is None  # old loop evicted
    assert not rt._run_resources.has_executor("c1")  # old executor evicted
    # Sandbox preserved (re-parked) so the rebuilt loop adopts the SAME workspace.
    assert rt._run_resources.pending_session("c1") is sentinel_sandbox


async def test_apply_change_on_finished_reresolves_driver(tmp_path, monkeypatch):
    """PROOF the new model drives the next turn: after a FINISHED-state swap, the router
    the next kick composes (`_router_now(pick=override)`) resolves AGENT_DRIVER to the NEW
    model — `_loop_for` builds its router with exactly this pick."""
    from disco.core.llm.types import ModelRole

    rt = _rt(tmp_path, monkeypatch)
    await _seed_terminal(rt, "c1", ConversationStatus.FINISHED)
    cfg = rt._config_store.load()
    old = cfg.assignments.get(ModelRole.AGENT_DRIVER)
    new = next(k for k in cfg.models if k != old)
    rt._settings.model_binding._model_overrides["c1"] = old
    rt._loop_registry.bind("c1", MagicMock())

    ok = await rt._settings.apply_settings_change("c1", model_override=new)
    assert ok is True
    assert rt._loop_registry.loop("c1") is None
    # The next compose resolves AGENT_DRIVER to the NEW model (not the old one).
    router = rt.drivers.router(pick=rt._settings.model_binding._model_overrides["c1"])
    assert router._config.assignments[ModelRole.AGENT_DRIVER] == new


async def test_loop_for_rebuilds_with_new_model_after_swap(tmp_path, monkeypatch):
    """End-to-end re-resolution via `_loop_for` (research surface — no sandbox needed):
    compose a loop pinned to model-a, FINISH it, swap to model-b, and confirm the NEXT
    `_loop_for` returns a NEW loop whose agent carries model-b (the old loop was evicted)."""
    from disco.core.llm.types import ModelRole

    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    rt._settings._set_surface("c1", "research")
    cfg = rt._config_store.load()
    model_a = cfg.assignments.get(ModelRole.AGENT_DRIVER)
    model_b = next(k for k in cfg.models if k != model_a)

    rt._settings.set_model_override("c1", model_a)
    loop1 = rt._loop_factory.loop_for("c1")
    assert loop1.agent._model_override == model_a

    # The run finishes, leaving the loop cached and bound to model-a.
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.FINISHED))

    ok = await rt._settings.apply_settings_change("c1", model_override=model_b)
    assert ok is True
    assert rt._loop_registry.loop("c1") is None  # evicted

    loop2 = rt._loop_factory.loop_for("c1")
    assert loop2 is not loop1  # genuinely re-composed
    assert loop2.agent._model_override == model_b  # the NEW model drives


# ── P1: explicit DEFAULT (null) reset in a terminal state (no silent-ignore) ──


async def test_explicit_null_on_terminal_clears_override_to_default(tmp_path, monkeypatch):
    """P1 — a user choosing DEFAULT (null) on a TERMINAL conversation that had an explicit
    model A must CLEAR the override (no silent-ignore) so the next turn runs the SERVER
    DEFAULT, not A. Asserts the override is cleared, the loop is evicted, and the
    re-resolved AGENT_DRIVER is the default (not A)."""
    from disco.core.llm.types import ModelRole

    rt = _rt(tmp_path, monkeypatch)
    await _seed_terminal(rt, "c1", ConversationStatus.FINISHED)
    cfg = rt._config_store.load()
    # The server default AGENT_DRIVER resolves via model_for (a role assignment OR the
    # default_model fallback) — not necessarily an explicit assignments entry.
    default = cfg.model_for(ModelRole.AGENT_DRIVER)
    model_a = next(k for k in cfg.models if k != default)
    rt._settings.model_binding._model_overrides["c1"] = model_a
    rt._loop_registry.bind("c1", MagicMock())

    # The PATCH route passes model_provided=True for an explicit null (reset to default).
    ok = await rt._settings.apply_settings_change(
        "c1",
        model_override=None,
        model_provided=True,
    )
    assert ok is True
    assert "c1" not in rt._settings.model_binding._model_overrides  # CLEARED (not left at model_a)
    assert rt._loop_registry.loop("c1") is None  # evicted → next kick re-resolves
    # The next compose resolves AGENT_DRIVER to the DEFAULT, not the prior explicit A.
    resolved = rt.drivers.router(
        pick=rt._settings.model_binding._model_overrides.get("c1")
    )._config.model_for(ModelRole.AGENT_DRIVER)
    assert resolved == default
    assert resolved != model_a


async def test_untouched_terminal_leaves_override(tmp_path, monkeypatch):
    """The other half: an UNTOUCHED picker (model_override field ABSENT, model_provided
    False) must LEAVE the conversation's model A unchanged — no clear, no evict."""
    rt = _rt(tmp_path, monkeypatch)
    await _seed_terminal(rt, "c1", ConversationStatus.FINISHED)
    rt._settings.model_binding._model_overrides["c1"] = "model-a"
    sentinel_loop = MagicMock()
    rt._loop_registry.bind("c1", sentinel_loop)

    # No model field, no assist → settable, but nothing requested → model untouched, loop
    # NOT evicted.
    ok = await rt._settings.apply_settings_change("c1", model_provided=False)
    assert ok is True
    assert rt._settings.model_binding._model_overrides.get("c1") == "model-a"  # left as-is
    assert rt._loop_registry.loop("c1") is sentinel_loop  # not evicted


async def test_explicit_null_pre_kick_seeds_sticky_not_clear(tmp_path, monkeypatch):
    """P3 preserved: an explicit null on the PRISTINE pre-kick path seeds the sticky
    last-selected model (a convenience), NOT a clear — so a create-time seed survives."""
    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    cfg = rt._config_store.load()
    sticky = next(iter(cfg.models))  # any valid catalogue key
    rt._settings.model_binding.set_last_selected_model(sticky)

    ok = await rt._settings.apply_settings_change(
        "c1",
        model_override=None,
        model_provided=True,
    )
    assert ok is True
    assert rt._settings.model_binding._model_overrides.get("c1") == sticky


async def test_patch_route_explicit_null_resets_terminal_to_default(tmp_path, monkeypatch):
    """PATCH /settings {"model_override": null} on a terminal conversation → 200 + the
    override is cleared (the route honors the explicit field via model_fields_set)."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    await _seed_terminal(rt, cid, ConversationStatus.FINISHED)
    rt._settings.model_binding._model_overrides[cid] = "explicit-model-a"

    r = client.patch(f"/conversations/{cid}/settings", json={"model_override": None})
    assert r.status_code == 200
    assert r.json()["model_override"] is None  # cleared
    assert cid not in rt._settings.model_binding._model_overrides
    sr = client.get(f"/conversations/{cid}/state")
    assert sr.json()["model_override"] is None


async def test_patch_route_assist_only_leaves_terminal_model(tmp_path, monkeypatch):
    """PATCH /settings with assist ONLY (model_override field ABSENT) on a terminal
    conversation must NOT touch the model — the field's absence means leave-as-is."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    await _seed_terminal(rt, cid, ConversationStatus.FINISHED)
    rt._settings.model_binding._model_overrides[cid] = "model-a"

    r = client.patch(f"/conversations/{cid}/settings", json={"assist": True})
    assert r.status_code == 200
    assert rt._settings.model_binding._model_overrides.get(cid) == "model-a"
    assert rt._settings.is_assist(cid) is True


async def test_resume_after_swap_composes_new_model(tmp_path, monkeypatch):
    """FULL-path proof (apply_settings_change → resume_conversation → kick → _loop_for):
    an ERRORED conversation swapped to model-b and then RESUMED composes its next-turn loop
    on model-b. Uses the research surface (no sandbox) and neutralizes the run task so no
    live model call is needed — we assert only the COMPOSED loop's driver model."""
    from disco.core.llm.types import ModelRole

    rt = _rt(tmp_path, monkeypatch)
    rt._store.create_conversation("c1")
    rt._settings._set_surface("c1", "research")
    cfg = rt._config_store.load()
    model_a = cfg.assignments.get(ModelRole.AGENT_DRIVER)
    model_b = next(k for k in cfg.models if k != model_a)
    rt._settings.set_model_override("c1", model_a)

    # A run that errored out (research surface ERROR is resumable).
    await rt._store.append("c1", _agent_msg("partial answer before the driver died"))
    await rt._store.append("c1", StatusEvent(status=ConversationStatus.ERROR))

    async def _noop_run_after_admission(conversation_id, loop, **_kwargs):  # noqa: ANN001
        return await rt._store.get_state(conversation_id)

    monkeypatch.setattr(
        rt._workspace,
        "run_after_admission",
        _noop_run_after_admission,
    )
    monkeypatch.setattr(
        rt._run_finalizer,
        "finalize_clean",
        AsyncMock(),
    )

    # The terminal-state swap is accepted + evicts any cached loop.
    assert (
        await rt._settings.apply_settings_change("c1", model_override=model_b)
        is True
    )

    result = await rt._resume.resume_conversation("c1")
    assert result["ok"] is True
    # The task is registered before kick returns; composition now occurs as its first
    # asynchronous stage so live context resolution cannot race the chosen model.
    await asyncio.sleep(0)
    # The loop kick composed for the NEXT turn is bound to the NEW model.
    composed = rt._loop_registry.loop("c1")
    assert composed is not None
    assert composed.agent._model_override == model_b


async def test_patch_route_ok_on_errored_conversation(tmp_path, monkeypatch):
    """PATCH /settings model_override on an ERRORED conversation → 200; persisted."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    await _seed_terminal(rt, cid, ConversationStatus.ERROR)
    rt._settings.model_binding._model_overrides[cid] = "old-model"

    r = client.patch(f"/conversations/{cid}/settings", json={"model_override": "new-model"})
    assert r.status_code == 200
    assert r.json()["model_override"] == "new-model"
    assert rt._settings.model_binding._model_overrides.get(cid) == "new-model"
    # /state overlays the pinned model so the picker can reflect it on resume.
    sr = client.get(f"/conversations/{cid}/state")
    assert sr.json()["model_override"] == "new-model"


async def test_patch_route_409_while_running_still_blocks(tmp_path, monkeypatch):
    """The mid-run guard is intact: a genuinely RUNNING conversation still 409s."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store=store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    cid = client.post("/conversations", json={"surface": "build"}).json()["conversation_id"]
    rt._settings.model_binding._model_overrides[cid] = "old-model"
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))

    r = client.patch(f"/conversations/{cid}/settings", json={"model_override": "new-model"})
    assert r.status_code == 409
    assert rt._settings.model_binding._model_overrides.get(cid) == "old-model"  # unmutated


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
    assert rt._settings._assist.get(cid) is False
