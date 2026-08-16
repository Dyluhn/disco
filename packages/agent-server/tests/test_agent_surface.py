"""The "agent" surface — Build-capable tools and workspace persistence with
proportional Agent framing and finish policy. These guard:

1. "agent" is a first-class accepted surface (set_surface does not coerce it away).
2. The `_surface_of` recovery ladder reads the AUTHORITATIVE `conversations.surface`
   column FIRST, so an agent conversation is never silently downgraded to "build"
   (the manifest heuristic can only ever say "build") when the in-memory/sidecar
   cache is lost — and the same DB rung closes a pre-existing hole where a build
   paused at its plan gate (no manifest yet) mis-derived "deep_research".
3. The surface survives a server restart via the DB rung even with no sidecar.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from disco.agent_server import ConversationRuntime
from disco.core import BuildPlatformAdmissionEvent, SqliteEventStore, WorkspaceMutationEvent
from disco.core.appkit import BuildBrief
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
    rt.settings._set_surface("c1", "agent")
    assert rt.settings._surface_of("c1") == "agent"  # NOT coerced to research


def test_surface_of_reads_db_column(tmp_path, monkeypatch):
    """With the in-memory cache empty, the durable conversations.surface column is
    the recovery answer — and it's the only signal that distinguishes agent from build."""
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("c1", owner_id="local", surface="agent")
    rt.settings._surface_settings.forget("c1")  # simulate a lost sidecar / pre-warmed cache
    assert rt._surface_of("c1") == "agent"


def test_surface_of_db_rung_beats_manifest_downgrade(tmp_path, monkeypatch):
    """Regression (Fable): an agent conversation that has snapshotted a workspace
    manifest must recover as 'agent', not 'build'. The manifest heuristic would
    derive 'build'; the DB column rung must short-circuit before it ever runs."""
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("c1", owner_id="local", surface="agent")
    rt.settings._surface_settings.forget("c1")

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
    """Agent keeps Build's tools but permits a direct answer with no fake action."""
    rt, store = _rt(tmp_path, monkeypatch)
    for cid, surface in (("a", "agent"), ("b", "build"), ("r", "research")):
        store.create_conversation(cid, owner_id="local", surface=surface)
        rt.settings._set_surface(cid, surface)

    loop_agent = rt._loop_for("a")
    loop_build = rt._loop_for("b")
    loop_research = rt._loop_for("r")

    # Same composition as build (agent class + executor), distinct from research.
    assert type(loop_agent.agent) is type(loop_build.agent)
    assert type(loop_agent.executor) is type(loop_build.executor)
    assert type(loop_agent.agent) is not type(loop_research.agent)
    assert type(loop_agent.executor) is not type(loop_research.executor)
    assert loop_agent._require_productive_action_before_finish is False
    assert loop_build._require_productive_action_before_finish is True
    assert loop_agent._delivery_contract_resolver is None
    assert loop_build._delivery_contract_resolver is not None
    assert "a" not in rt._build_platform.selected_profiles


async def test_plain_agent_has_no_provisional_web_admission(tmp_path, monkeypatch):
    rt, store = _rt(tmp_path, monkeypatch)
    for cid, surface in (("a", "agent"), ("b", "build")):
        store.create_conversation(cid, owner_id="local", surface=surface)
        rt.settings._set_surface(cid, surface)
        rt._loop_for(cid)
        await store.append(
            cid,
            WorkspaceMutationEvent(
                operation="agent.run-intent.message",
                run_protocol_version=1,
            ),
        )
        await rt._build_platform.record_route_locked(cid)

    agent_admissions = [
        event
        for event in await store.get_events("a")
        if isinstance(event, BuildPlatformAdmissionEvent)
    ]
    build_admissions = [
        event
        for event in await store.get_events("b")
        if isinstance(event, BuildPlatformAdmissionEvent)
    ]
    assert agent_admissions == []
    assert [event.profile_id for event in build_admissions] == ["disco.freeform_web@1"]


def test_strict_appkit_agent_keeps_productive_action_finish_gate(tmp_path, monkeypatch):
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("strict", owner_id="local", surface="agent", appkit_mode=True)
    rt.settings._set_surface("strict", "agent")

    loop = rt._loop_for("strict")

    assert loop._require_productive_action_before_finish is True


async def test_agent_ingress_drops_legacy_build_contract_marker(tmp_path, monkeypatch):
    """A stale UI/client cannot pre-classify an ordinary Agent task as an app."""
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("agent-neutral", owner_id="local", surface="agent")
    rt.settings._set_surface("agent-neutral", "agent")
    send = AsyncMock(return_value=object())
    monkeypatch.setattr(rt.conversation_control, "send_user_turn", send)

    await rt.send_user_turn(
        "agent-neutral",
        "summarize these notes",
        build_brief=BuildBrief(app_kind="web_app", primary_goal="summarize these notes"),
    )

    assert send.await_args.kwargs["build_brief"] is None


async def test_build_ingress_retains_explicit_contract_marker(tmp_path, monkeypatch):
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("build-contract", owner_id="local", surface="build")
    rt.settings._set_surface("build-contract", "build")
    send = AsyncMock(return_value=object())
    monkeypatch.setattr(rt.conversation_control, "send_user_turn", send)
    brief = BuildBrief(app_kind="web_app", primary_goal="build a site")

    await rt.send_user_turn("build-contract", "build a site", build_brief=brief)

    assert send.await_args.kwargs["build_brief"] is brief


async def test_strict_appkit_agent_retains_classified_contract_marker(tmp_path, monkeypatch):
    rt, store = _rt(tmp_path, monkeypatch)
    store.create_conversation("strict-appkit", owner_id="local", surface="agent", appkit_mode=True)
    rt.settings._set_surface("strict-appkit", "agent")
    send = AsyncMock(return_value=object())
    monkeypatch.setattr(rt.conversation_control, "send_user_turn", send)
    brief = BuildBrief(app_kind="web_app", primary_goal="create an AppKit app")

    await rt.send_user_turn("strict-appkit", "create an AppKit app", build_brief=brief)

    assert send.await_args.kwargs["build_brief"] is brief
