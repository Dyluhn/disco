"""Wire-through: DefaultToolExecutor stamps its model_policy's assist gate onto
EVERY ToolContext it builds.

The executor is the only thing that constructs ToolContext for live tool runs,
so a tool can only see the weak-model gate if the executor copies its policy's
assist value into ctx.assist.  These tests prove the executor populates ctx.assist
from model_policy.assist — the value-flow the ToolContext-field tests can't see
because they build the context by hand."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from disco.core.appkit.primitives import PrimitiveVerifyResult
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.llm import ModelExecutionPolicy
from disco.tools.anatomy import ToolDef
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import ToolRegistry, ToolScope
from pydantic import BaseModel

pytestmark = pytest.mark.asyncio


class _NoArgs(BaseModel):
    pass


_TOOL_DEF = ToolDef(name="probe", description="in-process probe", args_model=_NoArgs)

_WEAK_POLICY = ModelExecutionPolicy(tier="weak", anchored_edit=True)
_STANDARD_POLICY = ModelExecutionPolicy.standard()


def _executor(*, model_policy: ModelExecutionPolicy) -> DefaultToolExecutor:
    reg = ToolRegistry()
    return DefaultToolExecutor(
        reg, ToolScope(allowed_tools=frozenset({"probe"})), model_policy=model_policy
    )


async def test_build_context_stamps_assist_true():
    """weak model_policy → ctx.assist True for an in-process tool."""
    ctx = await _executor(model_policy=_WEAK_POLICY)._build_context(_TOOL_DEF)
    assert ctx.assist is True


async def test_build_context_stamps_assist_false():
    """standard model_policy → ctx.assist False (gate genuinely OFF, not just defaulted)."""
    ctx = await _executor(model_policy=_STANDARD_POLICY)._build_context(_TOOL_DEF)
    assert ctx.assist is False


async def test_build_context_assist_defaults_off():
    """Construction with no model_policy → standard policy → ctx.assist False, so the
    capable-model default carries through the executor unchanged."""
    reg = ToolRegistry()
    ex = DefaultToolExecutor(reg, ToolScope(allowed_tools=frozenset({"probe"})))
    ctx = await ex._build_context(_TOOL_DEF)
    assert ctx.assist is False


async def test_build_context_stamps_host_live_primitive_verifier():
    async def live_verify(
        live_id: str,
        app: AppSpec,
        design: DesignSpec,
        tree: Mapping[str, str],
    ) -> PrimitiveVerifyResult:
        del live_id, app, design, tree
        return PrimitiveVerifyResult(ok=False, detail="not invoked")

    reg = ToolRegistry()
    ex = DefaultToolExecutor(
        reg,
        ToolScope(allowed_tools=frozenset({"probe"})),
        primitive_live_verifier=live_verify,
    )
    ctx = await ex._build_context(_TOOL_DEF)
    assert ctx.primitive_live_verifier is live_verify
