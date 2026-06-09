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
