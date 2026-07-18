"""`think` — a NO-OP reasoning-dump tool.

Gives a small model a cheap, structured place to dump reasoning so it does not
degenerate prose into action slots (e.g. emitting a stray `file_write` call
just to "say something"). Patterned after pi's and OpenHands' `ThinkTool`.

Pure NO-OP: no sandbox, no file/state writes, no side effects. `run()` simply
acknowledges that the thought was received. The `thought` argument is
deliberately NOT echoed into the ToolOutcome (neither `content` nor
`structured`) so it can never leak into the deliverable set / model-visible
result trail.
"""

from __future__ import annotations

import json

from disco.core import SecurityRisk
from pydantic import BaseModel, Field, field_validator

from ..anatomy import ToolContext, ToolDef, ToolOutcome
from ..behavior import declares


class ThinkArgs(BaseModel):
    thought: str = Field(
        description=(
            "Free-form reasoning scratchpad. Use this when you need to work "
            "through a problem, weigh trade-offs, or plan a sequence of actions "
            "before committing to one. The content is not executed or stored — "
            "it is discarded after the call returns."
        )
    )

    @field_validator("thought", mode="before")
    @classmethod
    def _coerce_thought(cls, v: object) -> str:
        # ROBUSTNESS (bake-off issue #3): a streamed arg can mangle `thought` into a
        # non-string (e.g. {"tool_call": ""} when an arg-delta drops). think is a pure
        # NO-OP whose value is DISCARDED, so coerce anything to a string rather than
        # hard-fail validation → retry → repeated_action_error → STUCK. Same family as #1.
        if isinstance(v, str):
            return v
        if v is None:
            return ""
        return json.dumps(v) if isinstance(v, dict | list) else str(v)


class ThinkTool:
    definition = ToolDef(
        name="think",
        description=(
            "NO-OP reasoning scratchpad. Use to think out loud between actions "
            "without taking any side effect on the workspace. Ideal for decomposing "
            "a problem or sequencing the next few steps ONCE — then act. The thought "
            "is recorded in the run history. Do NOT chain think calls or use it in "
            "place of real work: consecutive bookkeeping-only turns (think / plan "
            "updates) count toward the run's stall limits."
        ),
        args_model=ThinkArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,  # pure observation of the model's own reasoning — no mutation
        behavior=declares(planner_safe=True),
    )

    async def run(self, args: ThinkArgs, ctx: ToolContext) -> ToolOutcome:
        # NO-OP: explicitly do nothing with `args.thought`. Do NOT echo it
        # back in `content` or `structured` — that would leak the reasoning
        # into the deliverable set / result trail, which would defeat the
        # point of a quiet scratchpad and bloat the model's context.
        return ToolOutcome(success=True, content="thought acknowledged")
