"""Plan-mode meta tools (Build) — the structured-plan signal + capstone progress.

These are pure CONTROL signals, not workspace actions: they run `in_process` (no
sandbox) and carry no side effects, so they are LOW risk and never gate a build.

- `submit_plan` is how the agent proposes its plan while in PLANNING mode. The agent
  loop INTERCEPTS this call (it is never executed as a tool) and turns it into a
  `PlanEvent` that halts for human approval. The `run()` here is a defensive no-op
  for the (filtered-out) case where it is somehow invoked during execution.
- `plan_step` lets the executing agent report which capstone it is on, so the UI can
  check steps off honestly (agent-driven; an unreported step simply stays pending).

C18 — `PlanStepInput.done_condition` is an OPTIONAL machine-checkable predicate the
agent attaches to a step. When the step is later marked done via `plan_step(idx,
'done')`, the engine evaluates the predicate and emits a visible pass/fail note in
the trace. ADVISORY ONLY: failure does not block the run, does not nudge the agent,
and does not duplicate the C1c finish gate. The predicate REUSES the
`disco.core.dod.DoDPredicate` shape (file_exists / command / http_ok) so the
planner speaks the same vocabulary as the DoD spec — but this check is local and
advisory, not the fresh-context DoD evaluation C1b runs.
"""

from __future__ import annotations

from typing import Literal

from disco.core.dod import DoDPredicate
from pydantic import BaseModel, Field

from ..anatomy import SecurityRisk, ToolContext, ToolDef, ToolOutcome


class PlanStepInput(BaseModel):
    title: str = Field(description="Short, plain-language capstone (one line).")
    detail: str | None = Field(default=None, description="Optional elaboration.")
    # C18 — optional advisory done-condition. The engine evaluates this when the
    # step is marked done via plan_step(idx, 'done') and surfaces a visible
    # pass/fail note in the trace. The shape is the same discriminated union as
    # `disco.core.dod.DoDPredicate` (file_exists / command / http_ok) so the
    # planner reuses the same vocabulary it would for a full DoD spec. Steps
    # WITHOUT a predicate behave exactly as today (back-compat).
    done_condition: DoDPredicate | None = Field(
        default=None,
        description=(
            "STRONGLY RECOMMENDED — a machine-checkable proof this step is done. "
            "For any step that CREATES a file, attach "
            "`{'kind': 'file_exists', 'path': '<the file this step produces>'}` "
            "naming that exact deliverable. These `file_exists` conditions are "
            "checked at FINISH: the build is not complete until the files you "
            "declared actually exist, so finish is refused until you have created "
            "them (this is how the system confirms you built what was asked — "
            "declaring a file you never create will block finish, not pass it). "
            "Other shapes (`{'kind': 'command', 'cmd': ..., 'expect_exit': 0}` | "
            "`{'kind': 'http_ok', 'url': ..., 'expect_status': 200}`) are also "
            "evaluated when you mark the step done and shown in the trace; today "
            "only `file_exists` gates finish. Same discriminated union as "
            "`disco.core.dod.DoDPredicate`."
        ),
    )


class SubmitPlanArgs(BaseModel):
    summary: str = Field(description="One or two sentences: what this plan delivers.")
    steps: list[PlanStepInput] = Field(
        description="Ordered capstones to carry out once the plan is approved."
    )
    context: str = Field(
        default="",
        description=(
            "Optional markdown: WHY this plan, what you learned from exploring the "
            "workspace (file_read/file_list) and the web (search/extract), and any "
            "trade-offs the human should know before approving. Shown to the user above "
            "the steps."
        ),
    )
    # FRICTION FIX: the build agent habitually attaches a `revision` field (a carry-over
    # from revision builds). The arg was harmless to pydantic (extra="ignore" by default)
    # but the executor's `describe_validation_failure` flagged it as an "unexpected
    # argument — not accepted by this tool" whenever the SAME call also tripped another
    # error (e.g. a missing `summary`), polluting the self-correcting message with a red
    # herring and wasting a turn. Declaring it as an explicit OPTIONAL, IGNORED field
    # makes `revision` a known/valid key, so it never shows up as unexpected and the call
    # carrying it simply succeeds. Accepted-but-ignored / deprecated: submit_plan always
    # proposes a fresh plan, so any revision number is a no-op.
    revision: object | None = Field(
        default=None,
        description=(
            "DEPRECATED / accepted-but-ignored. submit_plan always proposes a fresh "
            "plan, so this field has no effect — you do not need to send it."
        ),
    )


class SubmitPlanTool:
    definition = ToolDef(
        name="submit_plan",
        description=(
            "Propose a plan for approval. EXPLORE FIRST: use file_list/file_read to "
            "understand the workspace and search/extract for any web context, THEN call "
            "submit_plan with a short summary, ordered concrete steps, and a markdown "
            "`context` block explaining what you found and why this plan. For every step "
            "that creates a file, attach a `done_condition` of "
            "`{'kind': 'file_exists', 'path': '<that file>'}` — this is how the system "
            "verifies, at finish, that you actually built what was asked. Do not take any "
            "state-changing action until the plan is approved."
        ),
        args_model=SubmitPlanArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,  # emits a plan for approval — no environment mutation
    )

    async def run(self, args: SubmitPlanArgs, ctx: ToolContext) -> ToolOutcome:
        # The loop intercepts submit_plan in PLANNING mode and never calls this. If it
        # is ever reached (e.g. invoked outside planning), it is an inert acknowledgement.
        return ToolOutcome(success=True, content="plan received")


class PlanStepArgs(BaseModel):
    index: int = Field(description="1-based index of the plan step you are reporting on.")
    state: Literal["active", "done"] = Field(
        description="'active' when you START a step, 'done' when you COMPLETE it."
    )


class PlanStepTool:
    # RETIRED (runthru-v2 #3): withheld from every tier — the declarative
    # `update_plan_progress` replaced incremental per-step marks. Kept registered +
    # handled for defensive back-compat (a stray out-of-band call still no-ops cleanly),
    # but never advertised. read_only=False for the same reason as update_plan_progress:
    # it UPDATES plan state (not read-only) and is execution-only (not planner-eligible).
    definition = ToolDef(
        name="plan_step",
        description=(
            "Update the plan tracker: mark a step 'active' when you start it and 'done' "
            "when you finish it. Updates plan state but takes no action in the workspace. "
            "(Retired — prefer update_plan_progress, which rewrites the full step snapshot.)"
        ),
        args_model=PlanStepArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=False,
    )

    async def run(self, args: PlanStepArgs, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome(
            success=True,
            content=f"step {args.index} → {args.state}",
            structured={"index": args.index, "state": args.state},
        )


# runthru-v2 (#3) — the DECLARATIVE progress tool for capable models (assist OFF).
# Replaces the per-step incremental `plan_step` delta with a full-state snapshot the
# model rewrites every time. This is the Claude Code TodoWrite / Cursor design: because
# each call carries the COMPLETE state of every step, a dropped/garbled/skipped update
# self-corrects on the next call (idempotent), rather than leaving a step wrong forever.
# Research (codex/gpt-5.5 + web survey) + Dylan: incremental deltas are fragile across
# ALL model tiers; declarative full-rewrite is what frontier models maintain reliably.
# Small models (assist ON) are NOT offered this — they get an honest no-bookkeeping
# plan (NL + done-at-finish), so the burden never falls on a model that can't carry it.
# A CONTROL signal: in_process, never gates finish. It UPDATES plan state (not read-only)
# and is execution-only (not planner-eligible) — see the read_only=False note below.
class PlanProgressItem(BaseModel):
    index: int = Field(description="1-based index of the plan step.")
    state: Literal["pending", "active", "done"] = Field(
        description="This step's CURRENT state: 'pending' (not started), 'active' "
        "(in progress now), or 'done' (completed)."
    )


class UpdatePlanProgressArgs(BaseModel):
    steps: list[PlanProgressItem] = Field(
        description=(
            "The FULL current state of EVERY plan step, in order — rewrite the WHOLE "
            "list each time (a declarative snapshot, NOT a delta). Include every step "
            "with its current state. Because each call is the complete picture, a "
            "missed update self-corrects the next time you call this."
        )
    )

    # NOTE: no leniency validator here on purpose. Malformed step items (empty
    # strings, non-objects, wrong-shaped entries) must be REJECTED by pydantic so the
    # executor's `describe_validation_failure` can hand the model an ACTIONABLE message
    # (the expected `{index, state}` shape + state enum + a copyable example) and the
    # model self-corrects on the next turn. We used to DROP un-parseable items here.
    # Live diagnosis (provider-side arg dump) showed the empty `steps:[""]` payloads are
    # MODEL-GENUINE — MiniMax-M3 intermittently streams empty placeholder strings (not a
    # transport/assembly truncation) — so the right response is an actionable rejection,
    # NOT a silent drop that would hide the formatting error. Because this is a non-
    # critical bookkeeping tool, its repeated rejections are kept from escalating a build
    # to AWAITING_USER (see signals._NONCRITICAL_FAILURE_TOOLS). A genuinely empty
    # `steps: []` still validates fine (a no-op snapshot).


class UpdatePlanProgressTool:
    definition = ToolDef(
        name="update_plan_progress",
        description=(
            "Update the plan tracker by rewriting the FULL list of step states (a "
            "declarative snapshot). `steps` is a list of OBJECTS, one per plan step: "
            "{\"index\": <1-based step number>, \"state\": \"pending\"|\"active\"|\"done\"} "
            "— e.g. {\"steps\": [{\"index\": 1, \"state\": \"done\"}, "
            "{\"index\": 2, \"state\": \"active\"}]}. Pass EVERY step (mark the one you're "
            "working on 'active', completed ones 'done'). This UPDATES the live progress "
            "UI — it changes plan state, but performs NO action in the workspace (no files, "
            "no commands). Call it as you make progress; each call is the full picture, so "
            "an occasional miss self-corrects on the next call."
        ),
        args_model=UpdatePlanProgressArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        # read_only is this codebase's "no WORKSPACE mutation / planner-eligible" class
        # (read_only=True tools are offered in the PLANNING schema). update_plan_progress
        # does NOT mutate the workspace, but it DOES update plan state, and it is an
        # EXECUTION-only tool (you cannot report progress before a plan is approved) — so
        # it is NOT read-only and must NOT be planner-eligible. (Confirmation is risk-based,
        # not read_only-based, so this adds no approval prompt.)
        read_only=False,
    )

    async def run(self, args: UpdatePlanProgressArgs, ctx: ToolContext) -> ToolOutcome:
        done = sum(1 for s in args.steps if s.state == "done")
        return ToolOutcome(
            success=True,
            content=f"plan progress: {done}/{len(args.steps)} done",
            structured={"steps": [s.model_dump() for s in args.steps]},
        )
