"""Tests for the `think` NO-OP reasoning-scratchpad tool (HS-07).

Acceptance:
  1. Calling `think(thought=...)` returns a clean ack ToolOutcome
     (success=True, short content) and has NO side effect — no file is written,
     no state changes, the sandbox filesystem is untouched.
  2. `think` is present in the default tool registry and in the agent scope's
     offered tool set.
  3. The `thought` argument is NOT echoed back into the ToolOutcome (neither
     in `content` nor in `structured`) — it must never leak into the
     deliverable set.
"""

from __future__ import annotations

import pytest
from conftest import FakeSandboxInstance, call
from disco.tools import (
    DefaultToolExecutor,
    agent_scope,
    build_default_registry,
)
from disco.tools.builtin import ThinkTool
from disco.tools.builtin.think import ThinkArgs
from disco.tools.secrets import CapabilityBroker


# ---- Test 1: ack ToolOutcome + no side effect -------------------------------


async def test_think_returns_clean_ack_with_no_side_effects():
    """think(thought=...) returns success=True ack and touches nothing on disk."""
    sandbox = FakeSandboxInstance()
    fs_before = dict(sandbox._fs)  # snapshot in-memory filesystem

    ex = DefaultToolExecutor(
        build_default_registry(),
        agent_scope(),
        sandbox=sandbox,
    )

    res = await ex.execute(call("think", thought="step 1 then step 2 then stop"))

    # 1. Clean ack from the executor's ToolResult (event-contract shape)
    assert res.success is True
    assert res.error is None
    assert res.tool_name == "think"
    # short content; not a verbose echo
    assert isinstance(res.content, str) and 0 < len(res.content) <= 80
    # thought MUST NOT leak back into the model-visible result
    assert "step 1 then step 2 then stop" not in res.content
    if res.structured is not None:
        # structured is even more dangerous (downstream consumers) — must NOT
        # carry the thought verbatim.
        assert "step 1 then step 2 then stop" not in str(res.structured)

    # 2. No side effect on the sandbox filesystem
    assert sandbox._fs == fs_before, (
        f"think mutated the sandbox fs: before={fs_before} after={sandbox._fs}"
    )

    # 3. Direct call to run() confirms the same NO-OP behavior and that the
    #    tool-level ToolOutcome has no artifacts (so the thought cannot
    #    surface as a workspace deliverable either).
    from disco.tools.anatomy import ToolContext

    ctx = ToolContext(
        sandbox=None,  # in_process tool — sandbox is not required
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="test-cid",
    )
    out = await ThinkTool().run(
        ThinkArgs(thought="private reasoning that must NOT appear in result"),
        ctx,
    )
    assert out.success is True
    assert out.error is None
    assert out.artifacts == [], "think must NEVER produce a workspace artifact"
    assert "private reasoning that must NOT appear in result" not in out.content
    assert "private reasoning that must NOT appear in result" not in str(out.structured or {})


# ---- Test 2: present in default registry + offered tool set -----------------


def test_think_in_default_registry_and_agent_scope():
    """think is registered by build_default_registry and offered in agent_scope."""
    reg = build_default_registry()
    scope = agent_scope()

    # in the registry
    assert "think" in reg.names()
    # in the agent scope
    assert "think" in scope.allowed_tools
    # and resolvable through the registry under that scope
    tool = reg.get("think", scope=scope)
    assert tool is not None
    assert tool.definition.name == "think"


def test_think_tool_def_is_readonly_and_in_process():
    """think is read-only + in_process, matching the plan_step NO-OP pattern."""
    d = ThinkTool().definition
    assert d.name == "think"
    assert d.read_only is True
    assert d.runs_in == "in_process"
    # No capabilities required (it's a pure NO-OP, no sandbox, no fs, no network)
    assert d.needs == frozenset()
