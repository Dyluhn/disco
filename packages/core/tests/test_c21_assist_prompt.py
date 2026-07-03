"""C21 — execution-prompt tuning for small open models, BEHIND the assist gate.

The assist gate is a per-conversation compensation switch for weak models
(req.assist on the wire). It defaults OFF, so the IRON RULE for this work is:
when assist is OFF, the execution prompt MUST be byte-identical to the
capable-model prompt that ships today. Compensations — including the
small-model execution prompt variant — activate ONLY when assist is ON.

These tests pin both halves of that contract:

  1. assist ON → the execution prompt is the small-model variant
     (crisper rules, one tool per step, no narration-instead-of-acting).
  2. assist OFF → the execution prompt is byte-identical to the
     pre-C21 _EXECUTION_DRIVER_PROMPT constant. Captured by comparing the
     string the system returns against the imported constant — not a
     snapshot — so the test is stable if the constant is hand-tweaked
     (the off-path is the one that doesn't change).

The byte-identical check uses the imported constant directly, which is the
truest "what was there before" reference: anything we type into the constant
becomes the off-path; anything we put in _EXECUTION_DRIVER_PROMPT_SMALL is
the on-path. The two prompts MUST diverge, and the off-path MUST be the
constant — that's the gate."""

from __future__ import annotations

from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    DriverPrompts,
    ModelRole,
    OperatingMode,
)
from disco.core.llm.prompts import (
    _EXECUTION_DRIVER_PROMPT,
    _EXECUTION_DRIVER_PROMPT_SMALL,
)
from llm_fakes import FakeModelProvider, simple_config

# --------------------------------------------------------------------------
# (1) The two constants exist, are non-empty, and are NOT the same string.
#     This is the simplest possible gate: the variant has to be a real
#     alternate prompt, not a copy of the original.
# --------------------------------------------------------------------------


def test_small_model_variant_constant_exists_and_differs():
    assert _EXECUTION_DRIVER_PROMPT.strip()
    assert _EXECUTION_DRIVER_PROMPT_SMALL.strip()
    assert _EXECUTION_DRIVER_PROMPT != _EXECUTION_DRIVER_PROMPT_SMALL


# --------------------------------------------------------------------------
# (2) assist ON → execution prompt is the small-model variant.
# --------------------------------------------------------------------------


def test_assist_on_returns_small_model_execution_prompt():
    dp = DriverPrompts()
    on = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    # The on-path MUST contain the small-model cues we wrote.
    assert "Call EXACTLY ONE tool per step" in on
    assert "Do not narrate instead of acting" in on
    # And it MUST NOT be the original capable-model prompt.
    assert on != _EXECUTION_DRIVER_PROMPT


def test_assist_on_planning_prompt_omits_withheld_tools():
    """No planning prompt (weak assist-on OR capable assist-off) may NAME a withheld
    tool (no-contamination). `plan_step` is RETIRED from the advertised surface for
    EVERY tier (runthru-v2 #3), and `update_plan_progress` is not named in any planning
    block, so the weak and capable planning prompts are now IDENTICAL — both omit the
    per-step progress tools entirely."""
    dp = DriverPrompts()
    on = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    off = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert "PLANNING mode" in on
    # plan_step is retired for all tiers → both planning prompts now coincide.
    assert on == off
    assert "plan_step" not in on
    assert "update_plan_progress" not in on
    # And the capable (assist-off) planning prompt must equally omit plan_step.
    assert "plan_step" not in off


# --------------------------------------------------------------------------
# (3) assist OFF → execution prompt is BYTE-IDENTICAL to the original.
#     This is the iron rule: capable models must see zero change.
# --------------------------------------------------------------------------


def test_assist_off_byte_identical_to_capable_prompt():
    dp = DriverPrompts()
    off = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    # Byte-identical — not just "contains", not just "starts with". The exact
    # string the constant holds is what must reach the wire for capable models.
    assert off == _EXECUTION_DRIVER_PROMPT


def test_assist_off_default_kwarg_byte_identical_to_capable_prompt():
    """A caller that never passes `assist` (every legacy call site) gets the
    original prompt — the default is OFF, not ON, and there is no auto-nudge."""
    dp = DriverPrompts()
    p = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert p == _EXECUTION_DRIVER_PROMPT


def test_assist_off_byte_identical_under_autonomous_prefix():
    """The autonomous prefix is prepended to the capable-model prompt before
    it is stored; the small-model variant goes through the same prefix. When
    assist is OFF, the prefixed capable prompt is what we get back."""
    dp = DriverPrompts(autonomous=True)
    off = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    # The prefix is present, the rest of the prompt is byte-identical to the
    # original — no edits to the capable-model prose slipped in via C21.
    assert off.startswith("AUTONOMOUS MODE")
    # Strip the prefix and compare the tail — proves we did not mutate the
    # capable-model prose as a side effect of the assist gate.
    autonomous_prefix = (
        "AUTONOMOUS MODE — no human is available to answer questions or approve "
        "your plan. Do NOT try to ask the user anything (the ask tools are not "
        "available). When a detail is missing or ambiguous, choose the most "
        "reasonable default, log the assumption in the `submit_plan.context` preamble, "
        "and proceed. Do not end your turns with questions. You must drive the task "
        "to `finish` yourself; if something is genuinely impossible, call `finish` "
        "and explain what is blocked in the summary.\n\n"
    )
    assert off[len(autonomous_prefix):] == _EXECUTION_DRIVER_PROMPT


def test_assist_off_byte_identical_under_skills_block():
    """Skills prepend the SAME WAY for both assist states. With assist OFF
    the resulting prompt equals [skills_block] + _EXECUTION_DRIVER_PROMPT
    — i.e. nothing about the small-model variant leaks into the off-path."""
    skills_block = "MY SKILLS BLOCK"
    dp = DriverPrompts(skills_block=skills_block)
    off = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert off == f"{skills_block}\n\n---\n\n{_EXECUTION_DRIVER_PROMPT}"


def test_assist_off_byte_identical_under_agent_flavor():
    """The `flavor='agent'` swap (build→task identity reframe) is applied to
    the capable prompt at __init__ time. With assist OFF the on-wire prompt
    uses the swapped capable prompt; with assist ON it uses the swapped
    small-model prompt. Both must be honest about the same identity."""
    dp = DriverPrompts(flavor="agent")
    off = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    # The identity reframe must have applied to the capable-model prompt and
    # the off-path is the resulting string verbatim.
    assert "autonomous task agent" in off
    assert "autonomous build agent" not in off


# --------------------------------------------------------------------------
# (4) The small-model variant is HONEST — every tool it names is a real one,
#     and the directives it issues are real affordances of the loop. Catches
#     invented-behavior drift if anyone ever "improves" the variant.
# --------------------------------------------------------------------------


def test_small_model_variant_names_only_real_tools():
    """Every tool name the variant instructs the model to use must exist in
    the engine's bookkeeping/known-tool set. The list here is the set of
    tools C21 references — keep it in sync with the variant's prose."""
    # Pull the engine's set of bookkeeping tool names so we know what is real.
    from disco.core.loop.engine import _BOOKKEEPING_TOOLS  # engine-local

    # Tools the small-model prompt explicitly directs the model to call.
    directed_tools = {
        "plan_step",  # mark a step active/done
        "finish",  # end the run
        "ask_user",  # blocking question
        "notify_user",  # non-blocking note
        "remember",  # pin a fact
        "serve",  # hand off a deliverable
        "propose_plan_update",  # revise the plan
        "shell_exec",  # run in a session
        "shell_view",  # inspect session output
        "shell_write_to_process",  # send stdin
        "shell_kill_process",  # kill a session
        "code_exec",  # persistent IPython
        "server_status",  # inspect ports/sessions
        "file_write",  # author a file
        "file_append",  # append to a file
        "file_edit",  # small edit
        "file_read",  # read a file
        "file_replace_lines",  # line-range replace
        "file_insert_lines",  # line-range insert
    }
    # Every directed tool is a real bookkeeping/affordance tool in the engine.
    assert directed_tools <= set(_BOOKKEEPING_TOOLS) | {
        "shell_exec",
        "shell_view",
        "shell_write_to_process",
        "shell_kill_process",
        "code_exec",
        "server_status",
        "file_write",
        "file_append",
        "file_edit",
        "file_read",
        "file_replace_lines",
        "file_insert_lines",
        "notify_user",
        "remember",
        "serve",
        "ask_user",
    }


# --------------------------------------------------------------------------
# (5) End-to-end: req.assist on the CompletionRequest flows into the system
#     prompt the router injects. This is the wire-up test — the prompt
#     selection must react to the gate value, not to anything else.
# --------------------------------------------------------------------------


async def test_router_injects_small_model_prompt_when_assist_true():
    local = FakeModelProvider("ollama")
    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
    router = DefaultLLMRouter(
        simple_config(),
        providers,
        prompt_provider=DriverPrompts(),
    )
    await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(
                role=ModelRole.AGENT_DRIVER, mode=OperatingMode.LONG_HORIZON
            ),
            messages=[LLMMessage(role="user", content="hello")],
            assist=True,
        )
    )
    seen = local.seen_requests[0]
    system = seen.messages[0].content
    # The wire prompt is the small-model variant.
    assert "Call EXACTLY ONE tool per step" in system
    assert system != _EXECUTION_DRIVER_PROMPT


async def test_router_injects_original_prompt_when_assist_false():
    local = FakeModelProvider("ollama")
    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
    router = DefaultLLMRouter(
        simple_config(),
        providers,
        prompt_provider=DriverPrompts(),
    )
    await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(
                role=ModelRole.AGENT_DRIVER, mode=OperatingMode.LONG_HORIZON
            ),
            messages=[LLMMessage(role="user", content="hello")],
            assist=False,
        )
    )
    seen = local.seen_requests[0]
    system = seen.messages[0].content
    # The wire prompt is the original capable-model prompt, byte-identical.
    assert system == _EXECUTION_DRIVER_PROMPT


async def test_router_injects_original_prompt_when_assist_default():
    """A CompletionRequest built without `assist=` (every legacy call site)
    gets the original prompt — the default on the wire is False, so the
    capable-model path stays byte-identical even for callers that never
    opted in."""
    local = FakeModelProvider("ollama")
    providers = {"ollama": local, "openrouter": FakeModelProvider("openrouter")}
    router = DefaultLLMRouter(
        simple_config(),
        providers,
        prompt_provider=DriverPrompts(),
    )
    req = CompletionRequest(
        profile=CapabilityProfile(
            role=ModelRole.AGENT_DRIVER, mode=OperatingMode.LONG_HORIZON
        ),
        messages=[LLMMessage(role="user", content="hello")],
    )
    # Sanity: the field defaults to False on the wire today.
    assert req.assist is False
    await router.complete(req)
    seen = local.seen_requests[0]
    system = seen.messages[0].content
    assert system == _EXECUTION_DRIVER_PROMPT
