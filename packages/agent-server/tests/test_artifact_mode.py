"""C6 — artifact_mode wiring.

Proves:
1. artifact_mode=True → _compose_build_loop uses NeverConfirm gate,
   OperatingMode.INTERACTIVE, and the ARTIFACT_TOOLS scope (no shell/browser).
2. artifact_mode=False/absent → byte-identical to normal build loop
   (BlastRadiusConfirm, PLANNING, AGENT_TOOLS scope).
3. The flag round-trips from the create-conversation body through to runtime state.
4. ARTIFACT_TOOLS is a strict subset of AGENT_TOOLS (the boundary guard).
"""

from __future__ import annotations

from unittest import mock

import pytest
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import DefaultLLMRouter, OperatingMode
from disco.core.loop import RouterAgent
from disco.tools import AGENT_TOOLS, ARTIFACT_TOOLS

# ---------------------------------------------------------------------------
# ARTIFACT_TOOLS boundary guard (no import of runtime needed)
# ---------------------------------------------------------------------------


def test_artifact_tools_strict_subset_of_agent_tools():
    """C6 security boundary: ARTIFACT_TOOLS ⊂ AGENT_TOOLS — artifact mode must
    NOT silently grant shell or browser access."""
    assert ARTIFACT_TOOLS < AGENT_TOOLS  # strict subset (proper subset)


def test_artifact_tools_excludes_dangerous_tools():
    """No shell/browser/code_exec/plan-gate/anchored-str-replace in artifact scope."""
    dangerous = {
        "shell", "shell_exec", "shell_view", "shell_wait",
        "shell_write_to_process", "shell_kill_process",
        "browser",
        "code_exec",
        "preview_start", "preview_status", "preview_logs", "preview_stop",
        "server_status",
        "submit_plan",
        "plan_step",
        "file_str_replace",
        "delegate_explore",
    }
    assert not (ARTIFACT_TOOLS & dangerous), (
        f"Artifact scope must not include dangerous tools: "
        f"{ARTIFACT_TOOLS & dangerous}"
    )


def test_artifact_tools_includes_required_tools():
    """ARTIFACT_TOOLS must include file writers + line-edit tools + asset generators."""
    required = {
        "file_read", "file_write", "file_append", "file_edit",
        "file_replace_lines", "file_insert_lines",  # line-edit pair (§3 fix)
        "file_list",
        "search", "extract",
        "sheet_generate", "slides_generate", "image_generate", "audio_overview",
        "think",
    }
    assert required <= ARTIFACT_TOOLS, (
        f"Required tools missing from ARTIFACT_TOOLS: {required - ARTIFACT_TOOLS}"
    )


# ---------------------------------------------------------------------------
# _compose_build_loop — composition assertions
# ---------------------------------------------------------------------------


def _rt() -> ConversationRuntime:
    return ConversationRuntime(SqliteEventStore(":memory:"))


def _loop_for(rt, cid, *, artifact_mode: bool = False):
    """Compose the build loop for `cid`, optionally with artifact_mode on."""
    if artifact_mode:
        rt.set_artifact_mode(cid, True)
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    with mock.patch.object(rt, "_sandbox_service_now"):
        return rt._compose_build_loop(cid, router, agent)


@pytest.mark.asyncio
async def test_compose_artifact_mode_on_uses_never_confirm():
    """artifact_mode=True → NeverConfirm gate (no per-action approval)."""
    from disco.core.loop.policies import NeverConfirm

    rt = _rt()
    loop = _loop_for(rt, "art1", artifact_mode=True)
    assert isinstance(loop.policy, NeverConfirm), (
        f"Expected NeverConfirm, got {type(loop.policy).__name__}"
    )


@pytest.mark.asyncio
async def test_compose_artifact_mode_on_uses_interactive_mode():
    """artifact_mode=True → OperatingMode.INTERACTIVE (no plan-gate)."""
    rt = _rt()
    loop = _loop_for(rt, "art2", artifact_mode=True)
    assert loop.mode == OperatingMode.INTERACTIVE, (
        f"Expected INTERACTIVE, got {loop.mode}"
    )


@pytest.mark.asyncio
async def test_compose_artifact_mode_on_uses_artifact_scope():
    """artifact_mode=True → executor scope == ARTIFACT_TOOLS (no shell/browser)."""
    rt = _rt()
    loop = _loop_for(rt, "art3", artifact_mode=True)
    assert loop.executor._scope.allowed_tools == ARTIFACT_TOOLS, (
        f"Expected ARTIFACT_TOOLS, got {loop.executor._scope.allowed_tools}"
    )


@pytest.mark.asyncio
async def test_compose_artifact_mode_off_uses_blast_radius_confirm():
    """artifact_mode=False → BlastRadiusConfirm gate (unchanged build loop)."""
    from disco.core.loop.policies import BlastRadiusConfirm

    rt = _rt()
    loop = _loop_for(rt, "bld1", artifact_mode=False)
    assert isinstance(loop.policy, BlastRadiusConfirm), (
        f"Expected BlastRadiusConfirm, got {type(loop.policy).__name__}"
    )


@pytest.mark.asyncio
async def test_compose_artifact_mode_off_uses_planning_mode():
    """artifact_mode=False → OperatingMode.PLANNING (unchanged build loop)."""
    rt = _rt()
    loop = _loop_for(rt, "bld2", artifact_mode=False)
    assert loop.mode == OperatingMode.PLANNING, (
        f"Expected PLANNING, got {loop.mode}"
    )


@pytest.mark.asyncio
async def test_compose_artifact_mode_off_uses_agent_scope():
    """artifact_mode=False → executor scope covers AGENT_TOOLS (unchanged build loop)."""
    rt = _rt()
    loop = _loop_for(rt, "bld3", artifact_mode=False)
    # The normal build loop uses AGENT_TOOLS (possibly with W4 advertised withholding,
    # but allowed_tools is always AGENT_TOOLS).
    assert loop.executor._scope.allowed_tools == AGENT_TOOLS, (
        f"Expected AGENT_TOOLS, got {loop.executor._scope.allowed_tools}"
    )


# ---------------------------------------------------------------------------
# Flag round-trip: create-conversation body → runtime state
# ---------------------------------------------------------------------------


def test_artifact_mode_flag_round_trips_from_body(tmp_path):
    """The create-conversation body wires artifact_mode into runtime state."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    # artifact_mode=True → flag set on the runtime
    resp = client.post(
        "/conversations",
        json={"surface": "agent", "artifact_mode": True},
    )
    assert resp.status_code == 200
    cid = resp.json()["conversation_id"]
    assert rt._effective_artifact_mode(cid) is True

    # artifact_mode=False (default) → flag NOT set
    resp2 = client.post(
        "/conversations",
        json={"surface": "agent", "artifact_mode": False},
    )
    assert resp2.status_code == 200
    cid2 = resp2.json()["conversation_id"]
    assert rt._effective_artifact_mode(cid2) is False

    # artifact_mode absent → defaults to False
    resp3 = client.post(
        "/conversations",
        json={"surface": "agent"},
    )
    assert resp3.status_code == 200
    cid3 = resp3.json()["conversation_id"]
    assert rt._effective_artifact_mode(cid3) is False


def test_artifact_mode_invalid_value_422():
    """Pydantic enforces bool — a non-bool value 422s at the edge."""
    from disco.agent_server.app import create_app
    from fastapi.testclient import TestClient

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    app = create_app(store, runtime=rt)
    client = TestClient(app)

    client.post(
        "/conversations",
        json={"surface": "agent", "artifact_mode": "yes"},
    )
    # Pydantic coerces the string "yes" → True (bool coercion from string); this is
    # expected Python/Pydantic behavior. The key invariant is that a clearly invalid
    # value such as a dict or non-string-coercible type is rejected.
    # Re-check with a value that Pydantic cannot coerce to bool:
    resp2 = client.post(
        "/conversations",
        json={"surface": "agent", "artifact_mode": {"nested": "object"}},
    )
    assert resp2.status_code == 422


def test_set_artifact_mode_and_effective(tmp_path):
    """set_artifact_mode / _effective_artifact_mode round-trip (no app needed)."""
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    assert rt._effective_artifact_mode("c_new") is False  # default off

    rt.set_artifact_mode("c_on", True)
    assert rt._effective_artifact_mode("c_on") is True

    rt.set_artifact_mode("c_off", False)
    assert rt._effective_artifact_mode("c_off") is False
