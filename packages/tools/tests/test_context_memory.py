"""CXT-2 tests: context_memory tool — read/write narrative kinds, structured-kind
write rejection, invalid kind/action, list determinism, and real scope visibility."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from disco.core.llm import ModelExecutionPolicy
from disco.tools import agent_scope, build_default_registry
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.context_memory import ContextMemoryArgs, ContextMemoryTool
from disco.tools.registry import artifact_scope
from disco.tools.secrets import CapabilityBroker

from tool_fakes import FakeSandboxInstance


def _ctx(sandbox: FakeSandboxInstance | None) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="test-cid",
    )


# --- scope visibility (not a dead registration) -------------------------------
def test_context_memory_is_registered_and_in_agent_scope() -> None:
    registry = build_default_registry()
    assert "context_memory" in registry.names()
    scope = agent_scope(model_policy=ModelExecutionPolicy.standard())
    offered = {t.definition.name for t in registry.in_scope(scope)}
    assert "context_memory" in offered


def test_context_memory_is_in_artifact_scope() -> None:
    registry = build_default_registry()
    offered = {t.definition.name for t in registry.in_scope(artifact_scope())}
    assert "context_memory" in offered


# --- list determinism ---------------------------------------------------------
@pytest.mark.asyncio
async def test_list_returns_sorted_durable_kinds() -> None:
    out = await ContextMemoryTool().run(ContextMemoryArgs(action="list"), _ctx(None))
    assert out.success
    kinds = out.structured["kinds"]
    assert kinds == sorted(kinds)
    assert len(kinds) == 10  # + design_direction (W2-DIRECTION)
    assert "current_goal" in kinds and "verifier_failures" in kinds
    # SUMMARY is multi-instance → not a durable singleton kind
    assert "summary" not in kinds


# --- narrative read/write round-trip ------------------------------------------
@pytest.mark.asyncio
async def test_write_then_read_narrative_kind() -> None:
    sbx = FakeSandboxInstance()
    tool = ContextMemoryTool()
    w = await tool.run(ContextMemoryArgs(action="write", kind="current_goal", content="ship it"), _ctx(sbx))
    assert w.success and w.structured["rel_path"].endswith("current_goal.md")
    r = await tool.run(ContextMemoryArgs(action="read", kind="current_goal"), _ctx(sbx))
    assert r.success and r.content == "ship it"


@pytest.mark.asyncio
async def test_read_absent_kind_reports_empty() -> None:
    out = await ContextMemoryTool().run(
        ContextMemoryArgs(action="read", kind="decisions"), _ctx(FakeSandboxInstance())
    )
    assert out.success and out.content == "(empty)"


# --- rejections ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_invalid_kind_rejected() -> None:
    out = await ContextMemoryTool().run(
        ContextMemoryArgs(action="read", kind="bogus"), _ctx(FakeSandboxInstance())
    )
    assert not out.success and out.error == "invalid_kind"


@pytest.mark.asyncio
async def test_structured_kind_write_rejected() -> None:
    out = await ContextMemoryTool().run(
        ContextMemoryArgs(action="write", kind="resource_manifest", content="[]"),
        _ctx(FakeSandboxInstance()),
    )
    assert not out.success and out.error == "structured_kind_readonly_via_tool"


@pytest.mark.asyncio
async def test_structured_kind_is_readable() -> None:
    out = await ContextMemoryTool().run(
        ContextMemoryArgs(action="read", kind="resource_manifest"), _ctx(FakeSandboxInstance())
    )
    assert out.success and out.content == "(empty)"


def test_invalid_action_rejected_at_schema_layer() -> None:
    # The args_model Literal forbids unknown actions; the executor guarantees a
    # valid args instance before run() is ever called.
    with pytest.raises(ValidationError):
        ContextMemoryArgs(action="delete")  # type: ignore[arg-type]
