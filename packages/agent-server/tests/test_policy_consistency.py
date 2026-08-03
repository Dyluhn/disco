"""Anti-desync regression — the consistency gate (Order D).

For BOTH a weak (assist=True) and a standard (assist=False) policy, every surface
that expresses the execution tier — badge, loop, executor, tool scope, prompt, and
wire request — must agree. This is the integration gate described in
docs/disco-assist-policy-refactor-plan.md § "ACCEPTANCE GATES — Consistency".

WHEN TO RUN: at integration, after Orders A (loop threading), B (tools/executor
threading), and C (runtime threading) have landed. The interfaces under test —
AgentLoop.model_policy, DefaultToolExecutor.model_policy, agent_scope(model_policy=),
runtime._effective_policy() — are added by those orders. Do NOT run against the
pre-refactor tree (AgentLoop/DefaultToolExecutor still accept `assist: bool` there).

Structure:
  §1 — weak tier: loop / executor / available_tools / prompt / wire request all
       report assist=True.
  §2 — standard tier: same surfaces all report assist=False.
  §3 — no-contamination: weak prompt doesn't mention withheld tools; weak scope
       doesn't advertise them.
  §4 — runtime threading: when the runtime composes a build conversation loop,
       runtime.is_assist() / loop._model_policy / executor._model_policy all agree.
  §5 — constructor sweep: no production AgentLoop / DefaultToolExecutor construction
       in the runtime compose path uses the legacy `assist=` kwarg.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.llm.prompts import DriverPrompts
from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def weak_policy() -> ModelExecutionPolicy:
    """Weak (assist) tier — no anchored-edit, progress snapshots withheld."""
    return ModelExecutionPolicy(tier="weak", anchored_edit=False)


@pytest.fixture
def standard_policy() -> ModelExecutionPolicy:
    """Standard (capable) tier — no weak-model compensations."""
    return ModelExecutionPolicy.standard()


def _make_loop(policy: ModelExecutionPolicy):
    """Construct a bare AgentLoop with `model_policy=` (Order A interface)."""
    from disco.core.loop.engine import AgentLoop

    return AgentLoop(
        conversation_id="test-cid",
        store=MagicMock(),
        agent=MagicMock(),
        executor=MagicMock(),
        router=MagicMock(),
        analyzer=MagicMock(),
        policy=MagicMock(),
        condenser=MagicMock(),
        summarizer=MagicMock(),
        mode=MagicMock(),
        model_policy=policy,  # Order A: replaces `assist: bool`
    )


def _make_executor(policy: ModelExecutionPolicy):
    """Construct a bare DefaultToolExecutor with `model_policy=` (Order B interface)."""
    from disco.tools.builtin import build_default_registry
    from disco.tools.executor import DefaultToolExecutor
    from disco.tools.registry import agent_scope

    # Order B: agent_scope takes model_policy= (not model_caps=)
    scope = agent_scope(model_policy=policy)
    # Order B: DefaultToolExecutor takes model_policy= (not assist: bool)
    return DefaultToolExecutor(
        build_default_registry(),  # real registry so available_tools() resolves correctly
        scope,
        model_policy=policy,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §1 — Weak tier: every surface reports assist=True
# ──────────────────────────────────────────────────────────────────────────────


def test_weak_policy_assist_property(weak_policy: ModelExecutionPolicy) -> None:
    """The resolved policy's own .assist gate must be True for the weak tier."""
    assert weak_policy.assist is True


def test_weak_loop_model_policy(weak_policy: ModelExecutionPolicy) -> None:
    """AgentLoop constructed with a weak policy stores _model_policy.assist=True."""
    loop = _make_loop(weak_policy)
    # Order A: loop stores self._model_policy (not a bare bool)
    assert loop._model_policy.assist is True


def test_weak_loop_assist_property(weak_policy: ModelExecutionPolicy) -> None:
    """AgentLoop._assist is a property returning _model_policy.assist (Order A)."""
    loop = _make_loop(weak_policy)
    assert loop._assist is True  # noqa: SLF001 — intentional private-attribute test


def test_weak_executor_model_policy(weak_policy: ModelExecutionPolicy) -> None:
    """DefaultToolExecutor constructed with a weak policy stores _model_policy.assist=True."""
    executor = _make_executor(weak_policy)
    # Order B: executor stores self._model_policy; ctx.assist = _model_policy.assist
    assert executor._model_policy.assist is True


def test_weak_available_tools_excludes_plan_step(weak_policy: ModelExecutionPolicy) -> None:
    """Weak executor's available_tools() must NOT advertise plan_step."""
    executor = _make_executor(weak_policy)
    names = {t.name for t in executor.available_tools()}
    assert "plan_step" not in names, (
        "weak tier: plan_step is a bookkeeping tool that small models drop — "
        "withholding it from the advertised set prevents the model from seeing "
        "a tool it won't use reliably"
    )


def test_weak_available_tools_excludes_update_plan_progress(
    weak_policy: ModelExecutionPolicy,
) -> None:
    """Weak executor's available_tools() must NOT advertise update_plan_progress."""
    executor = _make_executor(weak_policy)
    names = {t.name for t in executor.available_tools()}
    assert "update_plan_progress" not in names, (
        "weak tier: update_plan_progress is withheld — small models use the "
        "natural-language plan recap instead of per-step progress bookkeeping"
    )


def test_weak_prompt_selects_small_model_variant(weak_policy: ModelExecutionPolicy) -> None:
    """DriverPrompts.system_prompt() with assist=True must return the small-model variant."""
    prompts = DriverPrompts()
    prompt = prompts.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=weak_policy.assist,
    )
    # The small-model prompt opens with a compact rules block rather than prose;
    # the distinctive header is the reliable discriminator between the two variants.
    assert "CORE RULES" in prompt, (
        "weak tier must select _EXECUTION_DRIVER_PROMPT_SMALL (the tighter "
        "small-model variant), not the capable-model prose prompt"
    )


def test_weak_completion_request_assist_true(weak_policy: ModelExecutionPolicy) -> None:
    """CompletionRequest.assist is True when built from a weak policy."""
    profile = CapabilityProfile(role=ModelRole.AGENT_DRIVER)
    req = CompletionRequest(profile=profile, messages=[], assist=weak_policy.assist)
    assert req.assist is True


# ──────────────────────────────────────────────────────────────────────────────
# §2 — Standard tier: every surface reports assist=False
# ──────────────────────────────────────────────────────────────────────────────


def test_standard_policy_assist_property(standard_policy: ModelExecutionPolicy) -> None:
    assert standard_policy.assist is False


def test_standard_loop_model_policy(standard_policy: ModelExecutionPolicy) -> None:
    loop = _make_loop(standard_policy)
    assert loop._model_policy.assist is False


def test_standard_loop_assist_property(standard_policy: ModelExecutionPolicy) -> None:
    loop = _make_loop(standard_policy)
    assert loop._assist is False  # noqa: SLF001


def test_standard_executor_model_policy(standard_policy: ModelExecutionPolicy) -> None:
    executor = _make_executor(standard_policy)
    assert executor._model_policy.assist is False


def test_standard_available_tools_excludes_plan_step(
    standard_policy: ModelExecutionPolicy,
) -> None:
    """Standard executor must NOT advertise plan_step — it is RETIRED for ALL tiers.

    runthru-v2 #3: incremental per-step `plan_step` caused plan-state drift and failed
    across capable AND small models alike, so it is withheld from the advertised surface
    for every tier (the declarative `update_plan_progress` replaced it). The engine still
    handles stray plan_step calls defensively, but it is never advertised.
    """
    executor = _make_executor(standard_policy)
    names = {t.name for t in executor.available_tools()}
    assert "plan_step" not in names, (
        "plan_step is retired from the advertised surface for ALL tiers (state-drift); "
        "standard must not advertise it"
    )


def test_standard_available_tools_includes_update_plan_progress(
    standard_policy: ModelExecutionPolicy,
) -> None:
    """Standard executor MUST advertise update_plan_progress."""
    executor = _make_executor(standard_policy)
    names = {t.name for t in executor.available_tools()}
    assert "update_plan_progress" in names, (
        "standard tier must advertise update_plan_progress; capable models use "
        "this to write the full live-checklist snapshot on every step"
    )


def test_standard_prompt_selects_capable_variant(standard_policy: ModelExecutionPolicy) -> None:
    """DriverPrompts.system_prompt() with assist=False returns the capable-model variant."""
    prompts = DriverPrompts()
    prompt = prompts.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=standard_policy.assist,
    )
    # The capable-model prompt explicitly instructs update_plan_progress usage.
    assert "update_plan_progress" in prompt, (
        "standard tier must select _EXECUTION_DRIVER_PROMPT (the capable-model "
        "variant), which instructs the model to call update_plan_progress"
    )


def test_standard_completion_request_assist_false(standard_policy: ModelExecutionPolicy) -> None:
    profile = CapabilityProfile(role=ModelRole.AGENT_DRIVER)
    req = CompletionRequest(profile=profile, messages=[], assist=standard_policy.assist)
    assert req.assist is False


# ──────────────────────────────────────────────────────────────────────────────
# §3 — No-contamination: weak policy neither advertises NOR names withheld tools
# ──────────────────────────────────────────────────────────────────────────────


def test_no_contamination_weak_prompt_no_plan_step(weak_policy: ModelExecutionPolicy) -> None:
    """The small-model execution prompt must NOT mention plan_step by name.

    Contamination means the model would see a tool name in its prompt but not in
    available_tools() — guaranteed confusion and spurious call attempts.
    """
    prompts = DriverPrompts()
    prompt = prompts.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    assert "plan_step" not in prompt, (
        "weak-tier prompt must NOT name `plan_step`: it's withheld from "
        "available_tools() and mentioning it would cause spurious calls"
    )


def test_no_contamination_weak_prompt_no_update_plan_progress(
    weak_policy: ModelExecutionPolicy,
) -> None:
    """The small-model execution prompt must NOT mention update_plan_progress."""
    prompts = DriverPrompts()
    prompt = prompts.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    assert "update_plan_progress" not in prompt, (
        "weak-tier prompt must NOT name `update_plan_progress`: it's withheld "
        "from available_tools() for the weak tier"
    )


def test_no_contamination_weak_scope_excludes_withheld_tools(
    weak_policy: ModelExecutionPolicy,
) -> None:
    """agent_scope(model_policy=weak) must NOT advertise any withheld tool.

    The withheld_tools set is the single place the tool surface is narrowed —
    if scope.advertised_tools contains a withheld tool, the two policies disagree.
    """
    from disco.tools.registry import agent_scope

    scope = agent_scope(model_policy=weak_policy)
    if scope.advertised_tools is not None:
        for tool_name in weak_policy.withheld_tools:
            assert tool_name not in scope.advertised_tools, (
                f"weak-tier scope must NOT advertise withheld tool {tool_name!r}; "
                f"withheld_tools={weak_policy.withheld_tools!r}"
            )


def test_no_contamination_standard_scope_withholds_nothing(
    standard_policy: ModelExecutionPolicy,
) -> None:
    """Standard anchored policy advertises the whole remaining agent scope."""
    assert standard_policy.withheld_tools == frozenset()


def test_no_contamination_non_anchored_standard_withholds_exact_replace() -> None:
    """Non-anchored standard withholds exact_replace only."""
    non_anchored = ModelExecutionPolicy(tier="standard", anchored_edit=False)
    assert non_anchored.withheld_tools == frozenset({"exact_replace"})


# ──────────────────────────────────────────────────────────────────────────────
# §4 — Runtime threading: badge ↔ loop ↔ executor all agree
#
# Composes a build conversation through the runtime (the _compose_build_loop
# path) and verifies that the objects the runtime stores agree with is_assist().
# This is the ANTI-STALENESS gate: if _compose_build_loop still uses the old
# `assist=self._effective_assist()` instead of `model_policy=self._effective_policy()`,
# the loop and executor will have the correct top-level value but the policy
# object won't be threaded through — which is exactly the desync the refactor
# closes.
# ──────────────────────────────────────────────────────────────────────────────


async def test_runtime_threads_policy_through_to_executor_and_loop_weak(tmp_path) -> None:
    """When a weak conversation is composed, executor._model_policy.assist == True
    and loop._model_policy.assist == True, both agreeing with runtime.is_assist()."""
    from unittest.mock import patch

    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SqliteEventStore
    from disco.core.llm import (
        CompletionResponse,
        DefaultLLMRouter,
        ModelEntry,
        RouterConfig,
        StreamChunk,
        TokenUsage,
    )
    from disco.tools import ProcessSandboxService

    class _FinishProvider:
        name = "fake"

        async def complete(self, req, *, model):
            return CompletionResponse(
                text="done",
                tool_calls=[],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="stop",
                model_used=model,
                request_id=req.request_id,
                routing=None,
            )

        async def stream_complete(self, req, *, model):
            yield StreamChunk(done=True, final=await self.complete(req, model=model))

        def supports(self, _req, *, model):  # noqa: ARG002
            return True

    cfg = RouterConfig(
        models={
            "m": ModelEntry(
                model_id="m",
                provider="fake",
                base_url="http://127.0.0.1:18080/v1",  # local → heuristic weak
                context_window=8192,
            )
        },
        default_model="m",
    )
    cid = "weak-cid"
    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        store = SqliteEventStore(":memory:")
        router = DefaultLLMRouter(cfg, {"fake": _FinishProvider()})
        rt = ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())
        # Explicit weak override (don't rely on the heuristic in a test)
        rt.set_assist(cid, True)
        store.create_conversation(cid, owner_id="local")
        rt.set_surface(cid, "build")

        # Drive the loop to compose (_compose_build_loop stores the executor)
        from disco.core import LLMMessage
        from disco.core import MessageEvent as CoreMessageEvent

        await store.append(
            cid,
            CoreMessageEvent(
                id="msg-1",
                source="user",
                message=LLMMessage(role="user", content="build something"),
            ),
        )
        with patch(
            "disco.agent_server.runtime._probe_live_model",
            return_value={"model_id": None, "n_ctx": None},
        ):
            rt.run_controller.kick(cid)

        # Wait for the loop to compose the executor (it runs in a background task)
        import asyncio

        for _ in range(20):
            if rt._run_resources.has_executor(cid):
                break
            await asyncio.sleep(0.05)

        # Badge source
        assert rt.is_assist(cid) is True

        # Executor threading (Order B): executor._model_policy is the resolved policy
        executor = rt._run_resources.executor(cid)
        assert executor is not None, "executor must be stored by _compose_build_loop"
        assert executor._model_policy.assist is True, (
            f"executor._model_policy.assist={executor._model_policy.assist!r} "
            f"but runtime.is_assist={rt.is_assist(cid)!r} — policy not threaded"
        )

        # Tool surface agrees: retired plan_step is absent and weak progress snapshots are hidden.
        advertised = {t.name for t in executor.available_tools()}
        assert "plan_step" not in advertised
        assert "update_plan_progress" not in advertised

        # Loop threading (Order A): loop._model_policy is the resolved policy
        loop = rt._loop_registry.loop(cid)
        if loop is not None:
            assert loop._model_policy.assist is True, (
                f"loop._model_policy.assist={loop._model_policy.assist!r} "
                f"but runtime.is_assist={rt.is_assist(cid)!r} — policy not threaded"
            )


async def test_runtime_threads_policy_through_to_executor_and_loop_standard(tmp_path) -> None:
    """When a standard conversation is composed, executor._model_policy.assist == False."""
    from unittest.mock import patch

    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SqliteEventStore
    from disco.core.llm import (
        CompletionResponse,
        DefaultLLMRouter,
        ModelEntry,
        RouterConfig,
        StreamChunk,
        TokenUsage,
    )
    from disco.tools import ProcessSandboxService

    class _FinishProvider:
        name = "fake"

        async def complete(self, req, *, model):
            return CompletionResponse(
                text="done",
                tool_calls=[],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="stop",
                model_used=model,
                request_id=req.request_id,
                routing=None,
            )

        async def stream_complete(self, req, *, model):
            yield StreamChunk(done=True, final=await self.complete(req, model=model))

        def supports(self, _req, *, model):  # noqa: ARG002
            return True

    cfg = RouterConfig(
        models={
            "m": ModelEntry(
                model_id="m",
                provider="fake",
                # cloud URL → heuristic standard; explicit override below is authoritative
                base_url="https://api.anthropic.com/v1",
                context_window=8192,
            )
        },
        default_model="m",
    )
    cid = "standard-cid"
    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        store = SqliteEventStore(":memory:")
        router = DefaultLLMRouter(cfg, {"fake": _FinishProvider()})
        rt = ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())
        # Explicit standard override
        rt.set_assist(cid, False)
        store.create_conversation(cid, owner_id="local")
        rt.set_surface(cid, "build")

        from disco.core import LLMMessage
        from disco.core import MessageEvent as CoreMessageEvent

        await store.append(
            cid,
            CoreMessageEvent(
                id="msg-1",
                source="user",
                message=LLMMessage(role="user", content="build something"),
            ),
        )
        with patch(
            "disco.agent_server.runtime._probe_live_model",
            return_value={"model_id": None, "n_ctx": None},
        ):
            rt.run_controller.kick(cid)

        import asyncio

        for _ in range(20):
            if rt._run_resources.has_executor(cid):
                break
            await asyncio.sleep(0.05)

        assert rt.is_assist(cid) is False
        executor = rt._run_resources.executor(cid)
        assert executor is not None
        assert executor._model_policy.assist is False, (
            "executor must carry standard policy when assist=False is set"
        )

        # Standard tier advertises update_plan_progress but NOT the retired plan_step.
        advertised = {t.name for t in executor.available_tools()}
        assert "plan_step" not in advertised
        assert "update_plan_progress" in advertised


# ──────────────────────────────────────────────────────────────────────────────
# §5 — Constructor sweep: no production compose path uses legacy assist=
#
# These grep-based tests catch regressions where someone adds a new AgentLoop or
# DefaultToolExecutor construction in the runtime without threading model_policy=.
# They are SOURCE-LEVEL checks, not behavioral — intentionally brittle so any
# slip in the compose path surfaces immediately.
# ──────────────────────────────────────────────────────────────────────────────


def test_sweep_no_legacy_agent_loop_assist_kwarg_in_runtime() -> None:
    """No production AgentLoop construction in runtime.py uses the legacy `assist=` kwarg.

    After Order A, `AgentLoop.__init__` drops the `assist: bool` param entirely;
    a grep for `AgentLoop(` ... `assist=` in runtime.py must return nothing
    (the compose paths should all pass `model_policy=`).
    """
    import re
    from pathlib import Path

    runtime_path = (
        Path(__file__).parents[3]
        / "packages"
        / "agent-server"
        / "src"
        / "disco"
        / "agent_server"
        / "runtime.py"
    )
    source = runtime_path.read_text()

    # Find every AgentLoop( call block and check for the legacy assist= kwarg.
    # We look for multi-line constructions: AgentLoop( ... assist= within a
    # reasonable window (200 chars) — enough to span any realistic constructor call.
    # This deliberately fails if someone passes assist= instead of model_policy=.
    loop_calls = re.finditer(r"AgentLoop\(", source)
    for match in loop_calls:
        window = source[match.start() : match.start() + 400]
        # The new interface is model_policy=; assist= is the old (now-deleted) param.
        # Quick pre-filter: skip windows with no assist= at all; inner loop checks
        # each line to exclude comment lines.
        if re.search(r"\bassist\s*=", window):
            # Narrow: is the `assist=` on a comment line?
            for line in window.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"\bassist\s*=", line):
                    raise AssertionError(
                        f"runtime.py contains a legacy `assist=` kwarg in an "
                        f"AgentLoop() constructor call. After Order A this must be "
                        f"`model_policy=`. Offending context:\n{window[:200]}"
                    )


def test_sweep_no_legacy_executor_assist_kwarg_in_runtime() -> None:
    """No production DefaultToolExecutor construction in runtime.py uses `assist=`.

    After Order B, `DefaultToolExecutor.__init__` drops `assist: bool`; the
    compose path must pass `model_policy=` exclusively.
    """
    import re
    from pathlib import Path

    runtime_path = (
        Path(__file__).parents[3]
        / "packages"
        / "agent-server"
        / "src"
        / "disco"
        / "agent_server"
        / "runtime.py"
    )
    source = runtime_path.read_text()

    executor_calls = re.finditer(r"DefaultToolExecutor\(", source)
    for match in executor_calls:
        window = source[match.start() : match.start() + 400]
        for line in window.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bassist\s*=", line):
                raise AssertionError(
                    f"runtime.py contains a legacy `assist=` kwarg in a "
                    f"DefaultToolExecutor() constructor call. After Order B this "
                    f"must be `model_policy=`. Offending context:\n{window[:200]}"
                )


def test_sweep_no_legacy_agent_scope_model_caps_in_runtime() -> None:
    """No call to agent_scope() in runtime.py uses the legacy `model_caps=` kwarg.

    After Order B, agent_scope() only accepts `model_policy=`.
    """
    import re
    from pathlib import Path

    runtime_path = (
        Path(__file__).parents[3]
        / "packages"
        / "agent-server"
        / "src"
        / "disco"
        / "agent_server"
        / "runtime.py"
    )
    source = runtime_path.read_text()

    scope_calls = re.finditer(r"agent_scope\(", source)
    for match in scope_calls:
        window = source[match.start() : match.start() + 200]
        if "model_caps=" in window:
            raise AssertionError(
                f"runtime.py contains a legacy `model_caps=` kwarg in an "
                f"agent_scope() call. After Order B this must be `model_policy=`. "
                f"Offending context:\n{window}"
            )


def test_sweep_resolve_policy_is_called_in_runtime_settings() -> None:
    """runtime_settings.py must call resolve_policy() — the single resolution point.

    This verifies the Order 0 contract: _effective_policy() delegates to
    resolve_policy() rather than reimplementing tier logic inline.
    """
    from pathlib import Path

    settings_path = (
        Path(__file__).parents[3]
        / "packages"
        / "agent-server"
        / "src"
        / "disco"
        / "agent_server"
        / "runtime_settings.py"
    )
    source = settings_path.read_text()
    assert "resolve_policy(" in source, (
        "runtime_settings.py must call resolve_policy() inside _effective_policy(); "
        "inlining the tier logic breaks the single-source guarantee"
    )
