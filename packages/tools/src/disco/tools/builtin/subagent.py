"""C20 — `delegate_explore`: a read-only Explore/Plan helper the build/agent
loop can dispatch+join (a BOUNDED subagent fan-out).

The loop is the ORCHESTRATOR; a single driver model does the work. Some plans
benefit from a focused, read-only second look before the driver commits — e.g.
"given this 800-line file, where should the new method go?" or "is the API
shape we picked consistent with the rest of the codebase?" `delegate_explore`
is the affordance for that: the driver calls it, the loop dispatches a
constrained helper task (read-only tools only — file_read, file_list,
search, extract, plus the question itself), and the helper's response is
folded back as a normal `ObservationEvent` so the driver sees it on its
next turn.

This module is a REGISTRY-LEVEL stub: it defines the tool's args_model, risk
hint, and read-only flag, and provides a defensive no-op `run()` (the loop
intercepts `delegate_explore` the same way it intercepts `notify_user`,
`remember`, `serve`, `finish`, `ask_user`, `propose_plan_update`, etc. — the
real dispatch+join lives in `core/loop/engine.py`). The defensive no-op
keeps a direct registry call (a tool-surface test, an outside-the-loop
harness, a regression case) honest: it returns a "not configured" result
rather than a fake subagent answer.

The fan-out is BOUNDED. The count cap + per-segment reset live in the engine
(see `_FANOUT_MAX_PER_RUN` + `self._fanout_count`); this module is
deliberately cap-agnostic — it does not know or care that the cap exists,
so the loop's enforcement is the single source of truth (and tests can pin
it on the engine side without faking through this tool).

The helper is READ-ONLY: a delegate_explore task cannot mutate workspace
state, cannot run shell, cannot write files. The constraint is enforced
UPSTREAM (the loop offers `delegate_explore` only with read-only tools in
its schema; the subagent can use no others), and documented in the
description so the driver model knows what the helper will and won't do.

SCOPE — `delegate_explore` is EXECUTION-only (the tool is in
`agent_scope()`'s allowed set, but the loop NEVER offers it to a
PLANNING agent; see `core/loop/engine.py:_tools_for_step` PLANNING
branch). The call dispatches a subagent, which is an action that
yields an observation, not a pure read — so the tool's
`read_only=False` flag is intentional, and the PLANNING branch
keeps the singleton out of the planning virtuals. A planner gathers
context with the read-only tools it already has (file_read /
file_list / search / extract) and proposes a plan; the fan-out
helper is available to the driver in execution mode only.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from disco.core import SecurityRisk

from ..anatomy import ToolContext, ToolDef, ToolOutcome


class DelegateExploreArgs(BaseModel):
    question: str = Field(
        description=(
            "One focused question the helper should answer (e.g. 'Where in "
            "src/foo.py should the new helper land?'). The helper sees only "
            "your question + the workspace it can read; it cannot ask back."
        )
    )
    context: str = Field(
        default="",
        description=(
            "Optional markdown: what the helper needs to know — file paths "
            "to look at, constraints from the user, the shape of the "
            "answer you want. The driver fills this in based on what it "
            "already knows; the helper does not see the rest of the "
            "conversation."
        ),
    )


class DelegateExploreTool:
    """C20 — a read-only Explore/Plan helper. The loop INTERCEPTS this call
    (it is never executed as a normal tool): the engine validates the cap,
    dispatches a constrained subagent task, and folds the result back as
    an `ObservationEvent` so the driver sees it on its next turn.

    `read_only=False` is intentional: the call DISPATCHES a subagent (it
    is an action that yields an observation, not a pure read). This means
    the PLANNING agent's capability backstop (engine._tools_for_step +
    executor.readonly_tool_names) correctly excludes it, and the engine
    itself never appends the singleton to the planning tool set — the
    helper is EXECUTION-only. The BOUNDED count cap (engine cap) prevents
    a model from saturating context with helper round-trips.

    The defensive `run()` exists for the case where the call somehow
    reaches the executor (e.g. an outside-the-loop harness, a registry-
    level test). It returns a structured, model-readable result naming
    "not configured" so a misrouted call never produces a fake answer."""

    definition = ToolDef(
        name="delegate_explore",
        description=(
            "Dispatch a bounded, read-only Explore/Plan helper task and "
            "fold its result back as an observation on your next turn. "
            "Use this when you need a focused second look at the workspace "
            "BEFORE you commit to an action — e.g. 'which files would the "
            "new helper need to import from?', 'is the function I want to "
            "call already defined somewhere in the codebase?', 'what does "
            "the existing test for X look like?'. The helper sees ONLY: "
            "(a) your `question`, (b) the optional `context` you pass, "
            "(c) the read-only tools file_read / file_list / search / "
            "extract. It cannot mutate workspace state, cannot run shell, "
            "cannot write files, and cannot ask back questions. The result "
            "is folded back as a normal tool observation. The dispatch is "
            "BOUNDED: a per-run-segment cap limits how many times you can "
            "call this (exceeding it is refused with feedback). Use it "
            "sparingly — for one focused question, not a full plan."
        ),
        args_model=DelegateExploreArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=False,  # C20: dispatching a subagent is an ACTION, not a pure read
    )

    async def run(self, args: DelegateExploreArgs, ctx: ToolContext) -> ToolOutcome:
        # The loop intercepts delegate_explore in engine.py and never invokes
        # this `run()`. If a caller reaches it directly (registry-level test,
        # outside-the-loop harness), return a structured "not configured" so
        # the answer is honest about the gap rather than fabricated.
        return ToolOutcome(
            success=False,
            content=(
                "delegate_explore: no subagent dispatcher is wired on this "
                "executor. The loop's intercept path (engine.py) is the only "
                "supported entry point — call from a model step inside the "
                "loop, not from a direct tool invocation."
            ),
            error="no_dispatcher",
        )
