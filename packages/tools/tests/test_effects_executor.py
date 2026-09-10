from __future__ import annotations

import asyncio
from typing import Literal, cast

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
from disco.tools.sandbox.base import SandboxError
from disco.tools.secrets import CapabilityDenied
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
    registry = ToolRegistry(allow_unclassified_for_testing=True)
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
    assert result.action_profile == behavior.static_profile()
    assert executor._catalog.tool_names_with_capability(
        EffectCapability.WORKSPACE_CONTENT_READ, executor._scope
    ) == {"read_probe"}
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


@pytest.mark.asyncio
async def test_invalid_classifier_persists_broad_profile_but_trusts_no_exact_receipt() -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset(
            {
                EffectCapability.WORKSPACE_CONTENT_READ,
                EffectCapability.WORKSPACE_MUTATE,
            }
        ),
    )

    class _ExpandingTool(_MixedTool):
        def action_profile(self, args: _Args) -> ActionProfile:
            del args
            return ActionProfile(capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}))

    tool = _ExpandingTool(name="expanding_run", behavior=behavior, receipt=_read_receipt())
    result = await _executor(tool).execute(
        ToolCall(tool_name="expanding_run", arguments={"operation": "read"})
    )

    assert result.action_profile == behavior.static_profile()
    assert isinstance(result.effect_receipts[0], OpaqueEffectReceipt)
    assert "classifier" in result.effect_receipts[0].reason


@pytest.mark.asyncio
async def test_profile_persists_on_tool_reported_failure_and_raised_exception() -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )

    class _ReportedFailure(_EffectTool):
        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            return ToolOutcome(success=False, content="failed", error="failed")

    class _RaisedFailure(_EffectTool):
        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            raise RuntimeError("boom")

    reported = _ReportedFailure(name="reported", behavior=behavior, receipt=_read_receipt())
    raised = _RaisedFailure(name="raised", behavior=behavior, receipt=_read_receipt())
    executor = _executor(reported, raised)

    reported_result = await executor.execute(ToolCall(tool_name="reported", arguments={}))
    raised_result = await executor.execute(ToolCall(tool_name="raised", arguments={}))

    assert reported_result.success is False
    assert raised_result.success is False
    assert reported_result.action_profile == behavior.static_profile()
    assert raised_result.action_profile == behavior.static_profile()


@pytest.mark.asyncio
async def test_wrong_runtime_outcome_is_a_profiled_execution_failure() -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )

    class _WrongReturn(_EffectTool):
        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            return cast(ToolOutcome, None)

    tool = _WrongReturn(name="wrong_return", behavior=behavior, receipt=_read_receipt())
    result = await _executor(tool).execute(ToolCall(tool_name="wrong_return", arguments={}))

    assert result.success is False
    assert result.structured is not None
    assert result.structured["kind"] == "execution_error"
    assert result.action_profile == behavior.static_profile()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected_kind"),
    [(CapabilityDenied("search"), "denied"), (SandboxError("gone"), "sandbox_error")],
)
async def test_profile_persists_on_mapped_execution_failures(
    raised: Exception,
    expected_kind: str,
) -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )

    class _MappedFailure(_EffectTool):
        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            raise raised

    tool = _MappedFailure(name="mapped", behavior=behavior, receipt=_read_receipt())
    result = await _executor(tool).execute(ToolCall(tool_name="mapped", arguments={}))

    assert result.success is False
    assert result.structured is not None
    assert result.structured["kind"] == expected_kind
    assert result.action_profile == behavior.static_profile()


@pytest.mark.asyncio
async def test_profile_persists_on_timeout_and_context_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )

    class _Slow(_EffectTool):
        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            await asyncio.sleep(1)
            return ToolOutcome(success=True, content="late")

    slow = _Slow(name="slow", behavior=behavior, receipt=_read_receipt())
    registry = ToolRegistry()
    registry.register(slow)
    timeout_executor = DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset({"slow"})),
        default_timeout_s=0,
    )
    timed_out = await timeout_executor.execute(ToolCall(tool_name="slow", arguments={}))

    async def _broken_context(definition: ToolDef) -> object:
        del definition
        raise RuntimeError("context unavailable")

    context_executor = _executor(slow)
    monkeypatch.setattr(context_executor, "_build_context", _broken_context)
    context_failed = await context_executor.execute(ToolCall(tool_name="slow", arguments={}))

    assert timed_out.structured is not None
    assert timed_out.structured["kind"] == "timeout"
    assert timed_out.action_profile == behavior.static_profile()
    assert context_failed.structured is not None
    assert context_failed.structured["kind"] == "execution_error"
    assert context_failed.action_profile == behavior.static_profile()


@pytest.mark.asyncio
async def test_workspace_lock_blocks_tool_context_and_execution_until_released() -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )
    entered = asyncio.Event()
    allow_exit = asyncio.Event()

    class _BlockingTool(_EffectTool):
        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            entered.set()
            await allow_exit.wait()
            return ToolOutcome(success=True, content="ok")

    tool = _BlockingTool(name="blocking", behavior=behavior, receipt=_read_receipt())
    registry = ToolRegistry()
    registry.register(tool)
    workspace_lock = asyncio.Lock()
    executor = DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset({"blocking"})),
        workspace_lock=workspace_lock,
    )

    await workspace_lock.acquire()
    task = asyncio.create_task(executor.execute(ToolCall(tool_name="blocking", arguments={})))
    try:
        # Scheduling the invocation while the host owns the workspace lock must
        # not construct a context or enter the tool. This is the barrier that
        # prevents a tool mutation from racing a host-owned final snapshot.
        await asyncio.sleep(0)
        assert entered.is_set() is False

        workspace_lock.release()
        await asyncio.wait_for(entered.wait(), timeout=1)
        allow_exit.set()
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        if workspace_lock.locked():
            workspace_lock.release()
        allow_exit.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert result.success is True


@pytest.mark.asyncio
async def test_execution_superseded_is_a_structured_non_executing_result() -> None:
    """A generation refusal is normal control flow, never a validation crash."""

    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )

    class _MustNotRun(_EffectTool):
        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            raise AssertionError("superseded tool reached its effect")

    seen_view_ids: list[str | None] = []

    async def refuse(agent_view_id: str | None) -> str:
        seen_view_ids.append(agent_view_id)
        return "a newer model view owns the workspace"

    tool = _MustNotRun(name="superseded", behavior=behavior, receipt=_read_receipt())
    registry = ToolRegistry()
    registry.register(tool)
    executor = DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset({"superseded"})),
        execution_admission=refuse,
    )

    result = await executor.execute_attributed(
        ToolCall(tool_name="superseded", arguments={}),
        "aview_current",
    )

    assert seen_view_ids == ["aview_current"]
    assert result.success is False
    assert result.error == "a newer model view owns the workspace"
    assert result.structured is not None
    assert result.structured["kind"] == "execution_superseded"
    assert result.action_profile == behavior.static_profile()


@pytest.mark.asyncio
async def test_pre_execution_refusals_have_no_action_profile() -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )
    tool = _EffectTool(name="probe", behavior=behavior, receipt=_read_receipt())
    executor = _executor(tool)

    unknown = await executor.execute(ToolCall(tool_name="unknown", arguments={}))
    invalid = await executor.execute(
        ToolCall(tool_name="probe", arguments={"operation": "not-valid"})
    )

    assert unknown.action_profile is None
    assert invalid.action_profile is None


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

    assert executor._catalog.unclassified_tool_names(executor._scope) == {"new_callable"}
    with pytest.raises(ValueError, match="new_callable"):
        executor._catalog.assert_behavior_complete(executor._scope)


def test_registry_is_strict_by_default_with_an_explicit_test_only_escape() -> None:
    tool = _EffectTool(name="legacy", behavior=None, receipt=_read_receipt())
    with pytest.raises(ValueError, match="no behavior metadata"):
        ToolRegistry().register(tool)

    registry = ToolRegistry(allow_unclassified_for_testing=True)
    registry.register(tool)
    assert registry.names() == {"legacy"}


def test_registry_revalidates_model_copy_forged_behavior() -> None:
    behavior = ToolBehavior(
        planner_safe=False,
        possible_capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}),
    )
    tool = _EffectTool(name="forged", behavior=behavior, receipt=_read_receipt())
    forged = tool.definition.model_copy(
        update={
            "behavior": ToolBehavior(
                planner_safe=True,
                possible_capabilities=frozenset({EffectCapability.EXTERNAL_OBSERVE}),
            )
        }
    )

    class _ForgedTool:
        definition = forged

        async def run(self, args: _Args, ctx: object) -> ToolOutcome:
            del args, ctx
            return ToolOutcome(success=True, content="should not run")

    with pytest.raises(ValueError, match="must match read_only"):
        ToolRegistry().register(_ForgedTool())


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
