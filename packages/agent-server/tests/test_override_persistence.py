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
