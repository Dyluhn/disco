"""B0 — the per-conversation model pick PERSISTS across a runtime restart. Before
this, `_model_override` was an in-memory dict; a server restart silently reverted
every build to the default model (it moved paid-model work back onto the local
default without telling the user)."""

from __future__ import annotations

from perpleximanus.agent_server import ConversationRuntime
from perpleximanus.core import SqliteEventStore
from perpleximanus.core.llm import ConfigStore, SecretBox, SecretStore


def _runtime(tmp_path, monkeypatch) -> ConversationRuntime:
    # PMX_DB drives the override sidecar path; point it at a tmp DB.
    monkeypatch.setenv("PMX_DB", str(tmp_path / "conv.db"))
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "secrets.json", box=SecretBox(None)),
    )


def test_model_override_survives_a_restart(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    rt.set_model_override("conv_abc", "or-deepseek-deepseek-v4-pro")
    assert rt._model_override["conv_abc"] == "or-deepseek-deepseek-v4-pro"

    # Simulate a server restart: a brand-new runtime over the SAME PMX_DB path.
    rt2 = _runtime(tmp_path, monkeypatch)
    assert rt2._model_override.get("conv_abc") == "or-deepseek-deepseek-v4-pro"  # not lost


def test_no_db_path_stays_in_memory_only(tmp_path, monkeypatch):
    # No PMX_DB → no sidecar (tests/dev); set still works in-memory, just not durable.
    monkeypatch.delenv("PMX_DB", raising=False)
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "c.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )
    rt.set_model_override("conv_x", "some-model")
    assert rt._model_override["conv_x"] == "some-model"
    assert rt._override_path == ""


class _FakeExecutor:
    def __init__(self):
        self.killed = False

    async def kill(self):
        self.killed = True


async def test_kill_drops_executor_and_loop_so_resume_rebuilds(tmp_path, monkeypatch):
    """The unrecoverable-loop fix: after kill(), the (now-dead) executor + loop are
    REMOVED from the caches, so a resume builds a fresh sandbox instead of reusing the
    killed executor (which returns 'executor killed; instance revoked' forever)."""
    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )
    cid = "conv_kill"
    ex = _FakeExecutor()
    rt._executors[cid] = ex
    rt._loops[cid] = object()  # a stand-in loop
    await rt.kill(cid)
    assert ex.killed is True
    assert cid not in rt._executors  # dropped → resume won't reuse the dead one
    assert cid not in rt._loops


async def test_teardown_clears_rehydrate_flag_so_continuation_restores_files(
    tmp_path, monkeypatch
):
    """The 'can't keep building after the first plan finished' fix: tearing down a
    FINISHED build's sandbox must clear the rehydrate-once flag, so the NEXT run
    restores the snapshot into the fresh sandbox instead of starting from an EMPTY
    workspace (which silently loses all prior work)."""
    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )
    cid = "conv_build"
    # First run rehydrated once → the flag is set (and stays set within a live
    # session so each subsequent kick doesn't re-overwrite in-progress files).
    await rt._maybe_rehydrate(cid)
    assert cid in rt._rehydrated
    # FINISHED → the sandbox is torn down. The flag MUST clear, else the next run
    # skips rehydrate and the continuation builds on nothing.
    rt._executors[cid] = _FakeExecutor()
    await rt._teardown_sandbox(cid)
    assert cid not in rt._rehydrated


async def test_reconcile_marks_orphaned_running_paused_and_notes(tmp_path, monkeypatch):
    """Startup orphan reconciliation: a conversation left RUNNING (its loop died with
    the previous server process) is marked PAUSED with an interrupted note; a FINISHED
    one is untouched — fixes the stale-'RUNNING'-forever-after-a-crash."""
    from perpleximanus.core import (
        ConversationStatus,
        EventSource,
        LLMMessage,
        MessageEvent,
        StatusEvent,
    )

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store,
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )

    # an orphaned RUNNING build (its loop died mid-run)
    store.create_conversation("conv_orphan", surface="build")
    await store.append(
        "conv_orphan",
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="build X")),
    )
    await store.append("conv_orphan", StatusEvent(status=ConversationStatus.RUNNING))
    # a FINISHED one — must NOT be touched
    store.create_conversation("conv_done", surface="build")
    await store.append("conv_done", StatusEvent(status=ConversationStatus.FINISHED))

    n = await rt.reconcile_orphaned_runs()

    assert n == 1
    assert (await store.get_state("conv_orphan")).execution_status is ConversationStatus.PAUSED
    assert (await store.get_state("conv_done")).execution_status is ConversationStatus.FINISHED
    # the interrupted note is on the log so the UI can explain the pause
    events = await store.get_events("conv_orphan")
    assert any(
        getattr(e, "source", None) is EventSource.ENVIRONMENT
        and "interrupted" in getattr(getattr(e, "message", None), "content", "")
        for e in events
    )


# ---- auto-suspend on tab-close (lifecycle G) --------------------------------


async def _runtime_with_projects(tmp_path, monkeypatch, root: str = ""):
    """A runtime whose config has projects_root set (or not) — auto-suspend only
    fires when storage is configured (otherwise the snapshot wouldn't be durable)."""
    from perpleximanus.core.llm import ProjectStorageSettings

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    cfg = ConfigStore(tmp_path / "config.json")
    if root:
        cfg.save_projects(ProjectStorageSettings(projects_root=root))
    rt = ConversationRuntime(
        store,
        config_store=cfg,
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )
    return rt, store


async def _set_status(store, cid, status):
    from perpleximanus.core import StatusEvent

    store.create_conversation(cid, surface="build")
    await store.append(cid, StatusEvent(status=status))


async def test_suspend_frees_idle_sandbox_but_not_a_running_one(tmp_path, monkeypatch):
    """The core auto-suspend policy: when the last viewer leaves, a build that is NOT
    actively RUNNING has its sandbox torn down (freeing container/port/memory) after
    snapshotting; an in-flight RUNNING run is left alone to finish in the background."""
    from perpleximanus.core import ConversationStatus

    rt, store = await _runtime_with_projects(tmp_path, monkeypatch, root=str(tmp_path / "ws"))

    # an IDLE (finished) build with a live sandbox → suspend tears it down
    idle = _FakeExecutor()
    rt._executors["conv_idle"] = idle
    await _set_status(store, "conv_idle", ConversationStatus.FINISHED)
    await rt._suspend("conv_idle")
    assert idle.killed is True
    assert "conv_idle" not in rt._executors  # sandbox freed

    # a RUNNING build → left alone (don't interrupt in-flight work)
    running = _FakeExecutor()
    rt._executors["conv_run"] = running
    await _set_status(store, "conv_run", ConversationStatus.RUNNING)
    await rt._suspend("conv_run")
    assert running.killed is False
    assert "conv_run" in rt._executors  # still live


async def test_suspend_is_a_noop_without_durable_storage(tmp_path, monkeypatch):
    """No projects_root → no durable snapshot, so tearing the sandbox down would LOSE
    work. Auto-suspend must keep the sandbox in that config (resume has nothing to
    restore from otherwise)."""
    from perpleximanus.core import ConversationStatus

    rt, store = await _runtime_with_projects(tmp_path, monkeypatch, root="")  # not configured
    ex = _FakeExecutor()
    rt._executors["conv_x"] = ex
    await _set_status(store, "conv_x", ConversationStatus.FINISHED)
    await rt._suspend("conv_x")
    assert ex.killed is False
    assert "conv_x" in rt._executors  # kept — nothing to restore from


async def test_on_disconnect_grace_fires_suspend_but_reconnect_cancels_it(tmp_path, monkeypatch):
    """Tab-close scheduling: the last on_disconnect schedules a suspend after the grace;
    an on_connect within the grace (a reconnect/blip) cancels it so the sandbox survives.
    A real disconnect with no reconnect lets the suspend fire."""
    import asyncio

    from perpleximanus.core import ConversationStatus

    rt, store = await _runtime_with_projects(tmp_path, monkeypatch, root=str(tmp_path / "ws"))
    await _set_status(store, "conv_a", ConversationStatus.FINISHED)

    # reconnect within the grace → the pending suspend is cancelled, sandbox kept
    keep = _FakeExecutor()
    rt._executors["conv_a"] = keep
    rt.on_connect("conv_a")
    rt.on_disconnect("conv_a", grace_s=0.05)
    rt.on_connect("conv_a")  # a blip reconnected before the grace elapsed
    await asyncio.sleep(0.12)
    assert keep.killed is False
    assert "conv_a" in rt._executors

    # now the viewer really leaves and nothing reconnects → suspend fires
    rt.on_disconnect("conv_a", grace_s=0.05)
    await asyncio.sleep(0.12)
    assert keep.killed is True
    assert "conv_a" not in rt._executors


async def test_deep_research_resume_carries_the_partial_report_forward(tmp_path, monkeypatch):
    """Checkpointed Deep Research resume (runtime wiring): a PAUSED run with a partial
    (bounded_by='stopped') ReportEvent on the log resumes by passing that report as
    `resume_from` to the engine — so completed sections are carried, not redone — and
    flips the status back to RUNNING/plan_approved first."""
    from perpleximanus.core import (
        ConversationStatus,
        EventSource,
        LLMMessage,
        MessageEvent,
        PlanEvent,
        PlanStep,
        ReportEvent,
        ReportSection,
        StatusEvent,
    )

    monkeypatch.setenv("PMX_DB", str(tmp_path / "c.db"))
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store,
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )
    cid = "conv_dr"
    store.create_conversation(cid, surface="deep_research")
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="state of X"),
        ),
    )
    plan = PlanEvent(
        summary="report on X",
        steps=[PlanStep(title="A"), PlanStep(title="B")],
        revision=1,
    )
    await store.append(cid, plan)
    # a partial report from a prior Stop: section A done, section B not.
    partial = ReportEvent(
        query="state of X",
        summary="(partial)",
        sections=[
            ReportSection(
                id="s0", title="A", markdown="body [[p0]]", cited_passage_ids=["p0"]
            )
        ],
        passages=[],
        all_hits=[],
        bounded_by="stopped",
    )
    await store.append(cid, partial)
    await store.append(cid, StatusEvent(status=ConversationStatus.PAUSED, detail="stopped"))

    # Capture what _execute_deep_research is handed (don't run the real engine).
    captured: dict = {}

    async def _fake_execute(conversation_id, plan_arg, *, resume_from=None):
        captured["cid"] = conversation_id
        captured["resume_from"] = resume_from

    monkeypatch.setattr(rt, "_execute_deep_research", _fake_execute)
    await rt._maybe_run_deep_research(cid)

    # it resumed with the partial report (its completed section carried), and
    # flipped the status back to RUNNING/plan_approved before executing.
    assert captured["resume_from"] is not None
    assert [s.title for s in captured["resume_from"].sections] == ["A"]
    last_status = next(
        e for e in reversed(await store.get_events(cid)) if isinstance(e, StatusEvent)
    )
    assert last_status.status is ConversationStatus.RUNNING
    assert last_status.detail == "plan_approved"
