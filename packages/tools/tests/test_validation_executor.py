"""Tool anatomy/validation (§11.1) + executor↔loop boundary (§11.2)."""

from __future__ import annotations

import asyncio

from disco.core.llm import ModelExecutionPolicy
from disco.tools import (
    DefaultToolExecutor,
    ToolContext,
    ToolDef,
    ToolOutcome,
    agent_scope,
    build_default_registry,
    research_scope,
)
from disco.tools.builtin import FileReadTool
from disco.tools.registry import ToolRegistry, ToolScope
from pydantic import BaseModel
from tool_fakes import FakeSandboxInstance, call

_STANDARD = ModelExecutionPolicy.standard()


def _executor(scope=None, sandbox=None):
    return DefaultToolExecutor(
        build_default_registry(),
        scope or agent_scope(model_policy=_STANDARD),
        sandbox=sandbox or FakeSandboxInstance(),
    )


# ---- §11.1 anatomy & validation ---------------------------------------------


def test_schema_is_single_source():
    """to_spec().parameters_schema IS args_model.model_json_schema() — no drift."""
    d = FileReadTool().definition
    assert d.to_spec().parameters_schema == d.args_model.model_json_schema()


async def test_unknown_tool_never_executes():
    ex = _executor()
    res = await ex.execute(call("nonexistent_tool", x=1))
    assert res.success is False
    assert res.structured["kind"] == "unknown_tool"
    assert "file_read" in res.content  # lists available tools


async def test_invalid_arguments_returns_schema_and_does_not_execute():
    ex = _executor(sandbox=FakeSandboxInstance())
    res = await ex.execute(call("file_read"))  # missing required `path`
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    assert res.structured["validation_errors"]  # the Pydantic errors
    assert res.structured["expected_schema"]["properties"]["path"]  # the schema, for repair


async def test_repair_loop_then_success():
    """A corrected retry succeeds; identical repeated failures are byte-identical
    (so the loop's stuck detector would trip — cross-checked there)."""
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("note.txt", b"hello")
    ex = _executor(sandbox=sandbox)
    bad1 = await ex.execute(call("file_read"))  # invalid
    bad2 = await ex.execute(call("file_read"))  # identical invalid
    good = await ex.execute(call("file_read", path="note.txt"))
    assert bad1.structured["kind"] == bad2.structured["kind"] == "invalid_arguments"
    assert bad1.content == bad2.content  # identical → stuck-detectable
    assert good.success and "hello" in good.content  # numbered read


# ---- §11.2 executor ↔ loop boundary -----------------------------------------


async def test_execute_always_returns_never_raises_on_tool_exception():
    class Boom(BaseModel):
        pass

    class BoomTool:
        definition = ToolDef(name="boom", description="raises", args_model=Boom)

        async def run(self, args, ctx):
            raise RuntimeError("kaboom")

    reg = ToolRegistry()
    reg.register(BoomTool())
    ex = DefaultToolExecutor(reg, ToolScope(allowed_tools=frozenset({"boom"})))
    res = await ex.execute(call("boom"))
    assert res.success is False and res.structured["kind"] == "execution_error"
    assert "kaboom" in res.error


async def test_correlation_call_id_preserved_on_success_and_failure():
    ex = _executor()
    c1 = call("file_read", path="x")  # will fail (no file in fresh fake)
    r1 = await ex.execute(c1)
    assert r1.call_id == c1.call_id
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("y", b"ok")
    c2 = call("file_read", path="y")
    r2 = await _executor(sandbox=sandbox).execute(c2)
    assert r2.call_id == c2.call_id and r2.success


async def test_timeout_yields_timeout_result():
    class SlowArgs(BaseModel):
        pass

    class SlowTool:
        definition = ToolDef(name="slow", description="sleeps", args_model=SlowArgs)

        async def run(self, args, ctx):
            await asyncio.sleep(10)
            return ToolOutcome(success=True, content="never")

    reg = ToolRegistry()
    reg.register(SlowTool())
    ex = DefaultToolExecutor(reg, ToolScope(allowed_tools=frozenset({"slow"})), default_timeout_s=0)
    res = await ex.execute(call("slow"))
    assert res.success is False and res.structured["kind"] == "timeout"


def test_available_tools_equals_scope():
    research = DefaultToolExecutor(build_default_registry(), research_scope())
    names = {s.name for s in research.available_tools()}
    assert "shell" not in names and "file_write" not in names  # excluded from research
    assert {"search", "extract", "file_read", "code_exec"} <= names


# ---- ToolContext carries no raw secret (shape check) ------------------------


def test_tool_context_has_no_secret_field():
    fields = set(ToolContext.model_fields)
    assert "secret" not in fields and "api_key" not in fields
    assert fields == {
        "sandbox",
        "workspace_path",
        "timeout_s",
        "capabilities",
        "owner_id",
        "conversation_id",
        "sessions",
        "kernel",
        "assist",  # T1: weak-model-assist gate flag (a bool, not a secret)
    }
