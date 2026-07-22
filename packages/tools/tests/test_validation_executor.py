"""Tool anatomy/validation (§11.1) + executor↔loop boundary (§11.2)."""

from __future__ import annotations

import asyncio
from typing import Literal

from disco.core.llm import ModelExecutionPolicy
from disco.tools import (
    DefaultToolExecutor,
    ToolContext,
    ToolDef,
    ToolOutcome,
    agent_scope,
    build_default_registry,
    research_scope,
    validate_args,
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


async def test_invalid_arguments_message_is_self_correcting():
    """B-G: a bad arg KEY (model invents `cmd` instead of `command`) must yield a
    message that names BOTH the unexpected key AND the expected/required field,
    so a weak model can self-correct rather than repeat the call into the
    5-failure stuck gate. The detail must be in the model-visible string
    (`error`/`content`), NOT only in `structured`."""
    ex = _executor(sandbox=FakeSandboxInstance())
    res = await ex.execute(call("shell", cmd="ls -la"))  # wrong key: cmd not command
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    seen = res.error or ""
    assert res.content == seen  # model sees this exact string
    # names the unexpected key the model invented...
    assert "cmd" in seen and "unexpected" in seen.lower()
    # ...and the field it should have used (required), with type
    assert "command" in seen
    assert "required" in seen
    # generic across tools: the expected-arguments surface is included
    assert "Expected arguments" in seen


async def test_invalid_arguments_message_deterministic_for_stuck_detector():
    """Identical bad calls produce byte-identical messages (so the loop's
    stuck detector still trips after the fix)."""
    ex = _executor(sandbox=FakeSandboxInstance())
    a = await ex.execute(call("shell", cmd="ls"))
    b = await ex.execute(call("shell", cmd="ls"))
    assert a.content == b.content


async def test_missing_required_message_names_field():
    """No-key-at-all case still names the missing required field generically."""
    ex = _executor(sandbox=FakeSandboxInstance())
    res = await ex.execute(call("file_read"))  # missing required `path`
    seen = res.error or ""
    assert "path" in seen and "missing required argument" in seen


# ---- Bug 18 — nested array-of-objects arg validation is ACTIONABLE -----------
#
# Live MiniMax-M3 finding (build_soak revise_after_finish): the model called
# `update_plan_progress({"steps": ["", "", ""]})` repeatedly. The bare pydantic
# message ("Input should be a valid dictionary or instance of PlanProgressItem")
# never showed the EXPECTED nested shape/example, so the model looped to STUCK.
# The fix enriches the validation error with the nested shape + enum + one concrete
# example — WITHOUT weakening the schema (`["", "", ""]` is still REJECTED).


async def test_update_plan_progress_malformed_steps_message_is_actionable():
    """`update_plan_progress({"steps": ["", "", ""]})` → invalid_arguments whose
    model-visible message shows the bad arg path, that steps is a LIST OF OBJECTS,
    the `index`/`state` fields, the `state` enum (pending/active/done), AND a
    concrete example — so a model can copy the shape instead of looping."""
    ex = _executor()
    res = await ex.execute(call("update_plan_progress", steps=["", "", ""]))
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    seen = res.error or ""
    assert res.content == seen  # the model sees exactly this string
    # bad arg path (the failing list index) is named
    assert "steps.0" in seen
    # the expected container: a list of objects
    assert "list of objects" in seen
    # the nested fields + the full state enum
    assert "index" in seen and "state" in seen
    assert "pending" in seen and "active" in seen and "done" in seen
    # a concrete, copyable example of a correct call
    assert '{"index": 1, "state": "pending"}' in seen
    assert "Example: steps=[" in seen


async def test_update_plan_progress_well_formed_steps_succeeds():
    """The corrected shape the actionable error points at actually validates +
    runs — proving the hint enables real recovery (not just a nicer rejection)."""
    ex = _executor()
    res = await ex.execute(call("update_plan_progress", steps=[{"index": 1, "state": "done"}]))
    assert res.success is True
    assert "1/1 done" in res.content


async def test_list_field_item_wrapper_unwraps_for_submit_plan():
    ex = _executor()
    res = await ex.execute(
        call(
            "submit_plan",
            summary="Ship the feature.",
            steps={"item": [{"title": "Do the thing"}]},
        )
    )

    assert res.success is True
    assert "plan received" in res.content


def test_submit_plan_schema_requires_at_least_one_step():
    from disco.tools.builtin.plan import SubmitPlanArgs

    steps = SubmitPlanArgs.model_json_schema()["properties"]["steps"]
    assert steps["minItems"] == 1


async def test_list_field_items_wrapper_unwraps_generically():
    ex = _executor()
    res = await ex.execute(
        call(
            "update_plan_progress",
            steps={"items": [{"index": 1, "state": "done"}]},
        )
    )

    assert res.success is True
    assert "1/1 done" in res.content


def test_validate_args_unwraps_list_field_item_wrapper() -> None:
    tool = build_default_registry().get(
        "submit_plan",
        scope=ToolScope(allowed_tools=frozenset({"submit_plan"})),
    )
    assert tool is not None

    args = validate_args(
        tool.definition,
        {"summary": "Ship it.", "steps": {"item": [{"title": "Do the thing"}]}},
    )

    assert args.steps[0].title == "Do the thing"


async def test_submit_plan_wrong_steps_shape_still_refuses_with_example():
    ex = _executor()
    res = await ex.execute(
        call(
            "submit_plan",
            summary="Ship the feature.",
            steps={"item": {"title": "not a list"}},
        )
    )

    assert res.success is False
    seen = res.error or ""
    assert "steps must be a JSON array" in seen
    assert '{"steps": [{"title": "..."}]}' in seen


async def test_submit_plan_done_condition_error_teaches_only_valid_shapes():
    """A malformed optional union gets a copyable correction, never a string
    placeholder that repeats the same validation failure."""
    ex = _executor()
    bad = await ex.execute(
        call(
            "submit_plan",
            summary="Ship the feature.",
            steps=[{"title": "Create the page", "done_condition": "static"}],
        )
    )

    assert bad.success is False
    assert bad.structured["kind"] == "invalid_arguments"
    seen = bad.error or ""
    assert "'steps[*].done_condition'" in seen
    assert '{"kind": "file_exists", "path": "..."}' in seen
    assert "set it to null, or omit it" in seen
    assert "Do not send a string" in seen
    assert '"done_condition": "..."' not in seen

    # Every correction the hint proposes is accepted by the unchanged schema.
    object_condition = await ex.execute(
        call(
            "submit_plan",
            summary="Ship the feature.",
            steps=[
                {
                    "title": "Create the page",
                    "done_condition": {"kind": "file_exists", "path": "..."},
                }
            ],
        )
    )
    null_condition = await ex.execute(
        call(
            "submit_plan",
            summary="Ship the feature.",
            steps=[{"title": "Create the page", "done_condition": None}],
        )
    )
    omitted_condition = await ex.execute(
        call(
            "submit_plan",
            summary="Ship the feature.",
            steps=[{"title": "Create the page"}],
        )
    )
    assert object_condition.success is True
    assert null_condition.success is True
    assert omitted_condition.success is True


async def test_list_wrapper_does_not_affect_non_list_fields():
    ex = _executor()
    res = await ex.execute(
        call(
            "submit_plan",
            summary={"item": ["not", "a", "summary"]},
            steps=[{"title": "Do the thing"}],
        )
    )

    assert res.success is False
    seen = res.error or ""
    assert "argument 'summary'" in seen
    assert "steps must be a JSON array" in seen


async def test_malformed_nested_arg_is_rejected_never_coerced():
    """MUST-NOT-REGRESS: a malformed nested arg is REJECTED (invalid_arguments) and
    the tool NEVER runs — the empty strings are not normalized into objects. Only
    the error message got richer; the schema still rejects."""
    ex = _executor()
    res = await ex.execute(call("update_plan_progress", steps=["", "", ""]))
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    # the raw structured pydantic errors are preserved (no coercion happened) and
    # the tool's own success output ("plan progress: x/y done") is absent — the
    # tool body never ran on the bad input (the empty strings were not normalized).
    assert res.structured["validation_errors"]
    assert "plan progress:" not in (res.content or "")


async def test_nested_shape_hint_is_generic_across_tools():
    """The actionable nested-shape hint is GENERIC — any tool with a nested model
    (or list-of-model) arg benefits, not just update_plan_progress. A fresh tool
    with a `list[_Item]` arg, called with a malformed item, gets the same shape +
    example treatment."""

    class _Item(BaseModel):
        name: str
        kind: Literal["alpha", "beta"]

    class _NestedArgs(BaseModel):
        items: list[_Item]

    class _NestedTool:
        definition = ToolDef(
            name="nested_tool", description="takes nested items", args_model=_NestedArgs
        )

        async def run(self, args, ctx):
            return ToolOutcome(success=True, content="ran")

    reg = ToolRegistry(allow_unclassified_for_testing=True)
    reg.register(_NestedTool())
    ex = DefaultToolExecutor(reg, ToolScope(allowed_tools=frozenset({"nested_tool"})))
    res = await ex.execute(call("nested_tool", items=["nope"]))
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    seen = res.error or ""
    assert "items.0" in seen
    assert "list of objects" in seen
    # the nested model's fields + enum + a concrete example, all generically derived
    assert '"name": <string>' in seen
    assert '"kind": "alpha"|"beta"' in seen
    assert "Example: items=[" in seen


async def test_actionable_nested_message_is_deterministic():
    """Identical malformed nested calls produce byte-identical messages, so the
    loop's stuck detector still trips after the fix (Bug 18 must not break stuck)."""
    ex = _executor()
    a = await ex.execute(call("update_plan_progress", steps=["", "", ""]))
    b = await ex.execute(call("update_plan_progress", steps=["", "", ""]))
    assert a.content == b.content


# ---- submit_plan tolerates the habitual `revision` field --------------------
#
# FRICTION FIX: the build agent frequently attaches a `revision` field to
# submit_plan (a carry-over habit from revision builds). That key used to surface
# in the self-correcting validation message as an "unexpected argument — not
# accepted by this tool" red herring (and confused the model) whenever the call
# also tripped another error. submit_plan now declares `revision` as an explicit
# optional, ignored field so the habitual call SUCCEEDS instead of erroring.


async def test_submit_plan_with_revision_succeeds():
    """`submit_plan` carrying the habitual `revision` field validates + runs (the
    field is accepted-but-ignored), with `summary`/`steps` still required."""
    ex = _executor()
    res = await ex.execute(
        call(
            "submit_plan",
            revision=1,
            summary="Ship the feature.",
            steps=[{"title": "Do the thing"}],
        )
    )
    assert res.success is True
    assert res.structured is None or res.structured.get("kind") != "invalid_arguments"
    # revision is ignored, not persisted as a plan field
    assert "plan received" in (res.content or "")


async def test_submit_plan_revision_never_flagged_as_unexpected():
    """Even when the SAME call trips a real error (missing `summary`), `revision`
    must NOT appear as an unexpected argument — it's a known, ignored field now —
    while the genuine missing-required error is still reported."""
    ex = _executor()
    res = await ex.execute(
        call("submit_plan", revision=2, steps=[{"title": "x"}])  # summary missing
    )
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    seen = res.error or ""
    # the real problem is named...
    assert "summary" in seen and "missing required argument" in seen
    # ...but the habitual `revision` is NOT slandered as unexpected/not-accepted:
    # the red-herring "unexpected argument(s)" reason is absent entirely.
    assert "unexpected argument" not in seen
    assert "not accepted by this tool" not in seen
    # (revision does still appear in the benign Expected-arguments surface as a
    # known optional field — that's the point: it's accepted, not rejected.)
    assert "revision (object | None, optional)" in seen


async def test_submit_plan_genuinely_unknown_field_still_flagged():
    """MUST-NOT-REGRESS: a genuinely-unknown invented key on submit_plan still
    surfaces as an unexpected argument (the schema didn't go permissive — only
    `revision` was explicitly whitelisted)."""
    ex = _executor()
    res = await ex.execute(
        call(
            "submit_plan",
            bogus_field=123,
            steps=[{"title": "x"}],  # summary still missing → invalid
        )
    )
    assert res.success is False
    assert res.structured["kind"] == "invalid_arguments"
    seen = res.error or ""
    assert "bogus_field" in seen and "unexpected" in seen.lower()


# ---- §11.2 executor ↔ loop boundary -----------------------------------------


async def test_execute_always_returns_never_raises_on_tool_exception():
    class Boom(BaseModel):
        pass

    class BoomTool:
        definition = ToolDef(name="boom", description="raises", args_model=Boom)

        async def run(self, args, ctx):
            raise RuntimeError("kaboom")

    reg = ToolRegistry(allow_unclassified_for_testing=True)
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

    reg = ToolRegistry(allow_unclassified_for_testing=True)
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
        # ROOT-5: (base_url, model_id, api_key_env) — the conversation's effective driver
        # endpoint. The api_key_env is the env-var NAME, NOT the secret value (§6 holds).
        "driver_llm",
        # CW-6: the capability-derived file_read page budget (an int, not a secret).
        "read_char_budget",
        # P7: the active contract's starter_kit name (e.g. "app_shell") — a str, not a secret.
        "starter_kit",
        # WF-3: event-log callback for workflow phase transitions; no credential payload.
        "workflow_events",
        # Host-owned callable only; captures secrets outside ToolContext.
        "primitive_live_verifier",
        # Current scope names, for recovery text only.
        "scope_allowed_tools",
        # BF1: bounded, host-owned browser/workspace-coherence metadata (exact
        # ints + a fixed lane label) — not secrets, not model-facing arguments.
        "browser_workspace_epoch",
        "browser_generation",
        "browser_lane",
        # Capability-aware host verifier pixel capture request. Boolean only;
        # it never claims pixels were inspected and carries no model identity.
        "browser_capture_screenshot_b64",
        # WO-C1: host-owned release-intent writer callable. Carries NO secret — the
        # runtime closure captures the store/config, and the tool passes only the
        # conversation id, owner id, and the NAMES-only ReleaseIntent (no values).
        "release_intent_writer",
    }


async def test_release_declare_behavior_is_truthful_and_never_dirties_the_workspace_epoch():
    """Areas 3+4 union coherence: release_declare registers under the fail-closed
    exhaustive-declaration registry with a TRUTHFUL declaration — it persists HOST
    project-store metadata (the intent sidecar, outside workspace/) only through the
    injected writer and never touches a workspace byte the browser/preview serves.
    So (a) the pinned declaration is planner-unsafe with NO reliability-policy
    capability, and (b) a SUCCESSFUL declaration must NOT advance the BF1
    browser/workspace mutation epoch — a false WORKSPACE_MUTATE claim would
    spuriously invalidate browser freshness after every declaration."""
    from disco.tools.builtin import ReleaseDeclareTool

    tool = ReleaseDeclareTool()
    behavior = tool.definition.behavior
    assert behavior is not None
    assert behavior.planner_safe is False  # durable host effect; matches read_only=False
    assert behavior.possible_capabilities == frozenset()  # exhaustive: none apply

    registry = ToolRegistry()  # production-strict: no test-only escape hatch
    registry.register(tool)  # would raise without an exhaustive declaration

    recorded: list[tuple[str, str, object]] = []

    async def writer(conversation_id: str, owner_id: str, intent) -> None:
        recorded.append((conversation_id, owner_id, intent))

    ex = DefaultToolExecutor(
        registry,
        ToolScope(allowed_tools=frozenset({"release_declare"})),
        release_intent_writer=writer,
    )
    result = await ex.execute(call("release_declare", start_cmd=["node", "server.js"]))
    assert result.success, result.error
    assert len(recorded) == 1  # the typed intent reached the HOST writer exactly once
    ctx = await ex._build_context(tool.definition)
    assert ctx.browser_workspace_epoch is None  # the workspace epoch stayed clean
