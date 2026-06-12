"""The "agent" surface — a framing copy of "build" (identical loop / tools / sandbox
/ workspace persistence; only the frontend framing + entry differ). These guard:

1. "agent" is a first-class accepted surface (set_surface does not coerce it away).
2. The `_surface_of` recovery ladder reads the AUTHORITATIVE `conversations.surface`
   column FIRST, so an agent conversation is never silently downgraded to "build"
   (the manifest heuristic can only ever say "build") when the in-memory/sidecar
   cache is lost — and the same DB rung closes a pre-existing hole where a build
   paused at its plan gate (no manifest yet) mis-derived "deep_research".
3. The surface survives a server restart via the DB rung even with no sidecar.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.tools.projects import StorageStatus


def _rt(tmp_path, monkeypatch, db="events.db"):
    """A runtime over a FILE-backed store (so the conversations row survives a
    simulated restart) + a sidecar under PMX_DB."""
    monkeypatch.setenv("PMX_DB", str(tmp_path / "sidecar.db"))
    store = SqliteEventStore(str(tmp_path / db))
    rt = ConversationRuntime(
        store,
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "secrets.json", box=SecretBox(None)),
    )
    return rt, store


def test_set_surface_accepts_agent(tmp_path, monkeypatch):
    rt, _ = _rt(tmp_path, monkeypatch)
    rt.set_surface("c1", "agent")
    assert rt._surface["c1"] == "agent"  # NOT coerced to research


def test_surface_of_reads_db_column(tmp_path, monkeypatch):
    """With the in-memory cache empty, the durable conversations.surface column is
    the recovery answer — and it's the only signal that distinguishes agent from build."""
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("c1", owner_id="local", surface="agent")
    rt._surface.pop("c1", None)  # simulate a lost sidecar / pre-warmed cache
    assert rt._surface_of("c1") == "agent"


def test_surface_of_db_rung_beats_manifest_downgrade(tmp_path, monkeypatch):
    """Regression (Fable): an agent conversation that has snapshotted a workspace
    manifest must recover as 'agent', not 'build'. The manifest heuristic would
    derive 'build'; the DB column rung must short-circuit before it ever runs."""
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("c1", owner_id="local", surface="agent")
    rt._surface.pop("c1", None)

    # Force the manifest heuristic to be available + positive (it would say "build").
    class _FakeProjStore:
        def status(self):
            return StorageStatus.OK

        def get(self, cid):
            return object()  # a manifest exists for this cid

    monkeypatch.setattr(rt, "_project_store_now", lambda: _FakeProjStore())
    assert rt._surface_of("c1") == "agent"  # DB rung wins; never downgraded


def test_surface_of_recovers_agent_across_restart(tmp_path, monkeypatch):
    """No sidecar entry at all (the worst case: sidecar lost with the process),
    but the DB row says 'agent' → a fresh runtime over the same DB recovers it."""
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("c1", owner_id="local", surface="agent")
    # brand-new runtime over the SAME event DB, fresh (empty) sidecar
    rt2, _ = _rt(tmp_path, monkeypatch)
    assert rt2._surface_of("c1") == "agent"


def test_agent_composes_the_same_loop_as_build(tmp_path, monkeypatch):
    """The behavioral copy: composing the loop for an 'agent' conversation yields the
    SAME agent class + tool executor as 'build', and a DIFFERENT one from 'research'."""
    rt, store = _rt(tmp_path, monkeypatch)
    for cid, surface in (("a", "agent"), ("b", "build"), ("r", "research")):
        store.create_conversation(cid, owner_id="local", surface=surface)
        rt.set_surface(cid, surface)

    loop_agent = rt._loop_for("a")
    loop_build = rt._loop_for("b")
    loop_research = rt._loop_for("r")

    # Same composition as build (agent class + executor), distinct from research.
    assert type(loop_agent.agent) is type(loop_build.agent)
    assert type(loop_agent.executor) is type(loop_build.executor)
    assert type(loop_agent.agent) is not type(loop_research.agent)
    assert type(loop_agent.executor) is not type(loop_research.executor)
