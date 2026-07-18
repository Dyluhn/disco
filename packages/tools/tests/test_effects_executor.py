from __future__ import annotations

from typing import Literal

import pytest
from disco.core import (
    ActionProfile,
    EffectCapability,
    MutationReceipt,
    ObservationReceipt,
    OpaqueEffectReceipt,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
    ToolBehavior,
    ToolCall,
)
from disco.core.effects import CoverageSpan, CoverageUnit
from disco.tools import DefaultToolExecutor, ToolDef, ToolOutcome, ToolRegistry, ToolScope
from pydantic import BaseModel, ConfigDict, ValidationError

_SHA_A = "a" * 64
_SHA_B = "b" * 64


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["read", "save"] = "read"


def _key() -> ResourceKey:
    return ResourceKey(namespace="workspace", identifier="index.html")


def _revision(digest: str = _SHA_A) -> ResourceRevision:
    return ResourceRevision(resource=_key(), digest=digest)


def _read_receipt() -> ObservationReceipt:
    return ObservationReceipt(
        capability=EffectCapability.WORKSPACE_CONTENT_READ,
        revision=_revision(),
        coverage=ResourceCoverage(
            unit=CoverageUnit.BYTES,
            spans=(CoverageSpan(start=0, end=1),),
            total=1,
        ),
        complete=True,
    )


def _mutation_receipt() -> MutationReceipt:
    return MutationReceipt(
        resource=_key(),
        before=_revision(),
        after=_revision(_SHA_B),
        after_size_bytes=2,
    )


class _EffectTool:
    def __init__(
        self,
        *,
        name: str,
        behavior: ToolBehavior | None,
        receipt: ObservationReceipt | MutationReceipt,
    ) -> None:
        self.definition = ToolDef(
            name=name,
            description="effect test tool",
            args_model=_Args,
            runs_in="in_process",
            read_only=bool(behavior and behavior.planner_safe),
            behavior=behavior,
        )
        self._receipt = receipt

    async def run(self, args: _Args, ctx: object) -> ToolOutcome:
        del args, ctx
        return ToolOutcome(success=True, content="ok", effect_receipts=(self._receipt,))


class _MixedTool(_EffectTool):
    def action_profile(self, args: _Args) -> ActionProfile:
        capability = (
            EffectCapability.WORKSPACE_CONTENT_READ
            if args.operation == "read"
            else EffectCapability.WORKSPACE_MUTATE
        )
        return ActionProfile(capabilities=frozenset({capability}))


def _executor(*tools: _EffectTool) -> DefaultToolExecutor:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    names = frozenset(tool.definition.name for tool in tools)
    return DefaultToolExecutor(registry, ToolScope(allowed_tools=names))


@pytest.mark.asyncio
async def test_executor_maps_permitted_typed_receipts_without_activation() -> None:
    behavior = ToolBehavior(
        planner_safe=True,
        possible_capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ}),
    )
    tool = _EffectTool(name="read_probe", behavior=behavior, receipt=_read_receipt())
    executor = _executor(tool)

    result = await executor.execute(
        ToolCall(tool_name="read_probe", arguments={"operation": "read"})
    )

    assert result.success is True
    assert result.effect_receipts == (_read_receipt(),)
    assert executor.tool_names_with_capability(EffectCapability.WORKSPACE_CONTENT_READ) == {
        "read_probe"
    }
    # The legacy planner query is still the live source in K1.
    assert executor.readonly_tool_names() == {"read_probe"}


@pytest.mark.asyncio
async def test_undeclared_or_unclassified_receipts_are_downgraded_to_opaque() -> None:
    classified = _EffectTool(
        name="bad_claim",
        behavior=ToolBehavior(
            planner_safe=True,
            possible_capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ}),
        ),
        receipt=_mutation_receipt(),
    )
    unclassified = _EffectTool(name="legacy", behavior=None, receipt=_read_receipt())
    executor = _executor(classified, unclassified)

    bad = await executor.execute(ToolCall(tool_name="bad_claim", arguments={}))
    legacy = await executor.execute(ToolCall(tool_name="legacy", arguments={}))

    assert isinstance(bad.effect_receipts[0], OpaqueEffectReceipt)
    assert "undeclared capabilities" in bad.effect_receipts[0].reason
    assert bad.effect_receipts[0].trusted is False
    assert isinstance(legacy.effect_receipts[0], OpaqueEffectReceipt)
    assert "unclassified" in legacy.effect_receipts[0].reason


def test_argument_classifier_narrows_a_mixed_tool_and_cannot_expand_behavior() -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset(
            {
                EffectCapability.WORKSPACE_CONTENT_READ,
                EffectCapability.WORKSPACE_MUTATE,
            }
        ),
    )
    tool = _MixedTool(name="mixed", behavior=behavior, receipt=_read_receipt())
    executor = _executor(tool)

    assert executor.action_profile_for_call("mixed", {"operation": "read"}) == ActionProfile(
        capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ})
    )
    assert executor.action_profile_for_call("mixed", {"operation": "save"}) == ActionProfile(
        capabilities=frozenset({EffectCapability.WORKSPACE_MUTATE})
    )

    class _ExpandingTool(_MixedTool):
        def action_profile(self, args: _Args) -> ActionProfile:
            del args
            return ActionProfile(capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}))

    expanding = _ExpandingTool(name="expanding", behavior=behavior, receipt=_read_receipt())
    expanding_executor = _executor(expanding)
    assert expanding_executor.action_profile_for_call("expanding", {}) is None


def test_registry_completeness_fails_for_a_new_callable_unclassified_tool() -> None:
    classified = _EffectTool(
        name="classified",
        behavior=ToolBehavior(
            planner_safe=True,
            possible_capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ}),
        ),
        receipt=_read_receipt(),
    )
    new_tool = _EffectTool(name="new_callable", behavior=None, receipt=_read_receipt())
    executor = _executor(classified, new_tool)

    assert executor.unclassified_tool_names() == {"new_callable"}
    with pytest.raises(ValueError, match="new_callable"):
        executor.assert_behavior_complete()


def test_shadow_behavior_cannot_change_legacy_planner_safety() -> None:
    with pytest.raises(ValidationError, match="must match read_only"):
        ToolDef(
            name="mismatch",
            description="x",
            args_model=_Args,
            read_only=False,
            behavior=ToolBehavior(
                planner_safe=True,
                possible_capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ}),
            ),
        )
