"""The agent loop — agent-loop-contract.md §2, §4, §5, §7, §8.

A bounded iteration over `Agent.step()`. The loop's correctness *is* the
product's reliability (BoD §1.1). It is transport-agnostic and drivable headless:
it produces events and consumes control operations, nothing more.

Key invariants (principles §1):
- One action per iteration, observed before the next.
- Status is reconstructed from the log, never held privately.
- A per-conversation FIFO lock guards every status read/mutation; stepping holds
  it EXCEPT during tool execution, so pause/steer/confirm land between steps and
  concurrent user input is never dropped.
- `max_iterations` is the ultimate backstop beneath stuck detection.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ..security import RiskAssessment
    from .boundaries import StreamHook

from ..dod import DoDPredicate, DoDSpec, DoDSpecAlreadySet
from ..dod_evaluator import DoDEvaluator
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    AlternativesEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
    ToolCall,
    ToolResult,
    VerifierVerdictEvent,
)
from ..llm import (
    LLMRouter,
    ModelExecutionPolicy,
    OperatingMode,
)
from ..state import ConversationState
from ..store.base import EventStore
from ..view import Condenser, Summarizer, View
from ..workflow import (
    WorkflowRun,
    workflow_finish_tool_description,
    workflow_finish_tool_schema,
)
from . import signals, view_render
from .boundaries import (
    Agent,
    AgentStep,
    ConfirmationPolicy,
    HostVerifier,
    SecurityAnalyzer,
    StopHook,
    ToolExecutor,
    VerifierJudge,
    is_finish_tool_name,  # P6 — re-exported; canonical def lives in boundaries
)
from .control import Disp
from .driver import (
    _PLANNING_TOOL_REFUSAL_ESCALATE_AT,
    _PLANNING_TOOL_REFUSAL_NARROW_AT,
    Driver,
    planning_tool_refusal_streak,
)
from .finish import (  # noqa: F401 — finish helpers/consts re-exported for back-compat
    _DOD_REFUSAL_CAP,
    _EXECUTION_NUDGE,
    _FINISH_VERIFY_CAP,
    FinishGate,
    _app_verify_command,
    _browser_verified,
    _DoDWorkspaceUnavailable,
    _is_web_deliverable,
    _last_productive_seq,
    _latest_browser_error,
    _static_verify_command,
    host_verify_authoritative_enabled,
)
from .observe import (  # noqa: F401 — _FANOUT_INPUT_MAX_CHARS re-exported for back-compat
    _FANOUT_INPUT_MAX_CHARS,
    Observer,
)
from .plan_conditions import PlanStepConditions
from .planning_harvest import harvest_revision_plan_after_refusal
from .plans import (
    Planner,
    validate_plan_done_conditions,
    validate_raw_plan_done_conditions,
)
from .recitation import (  # noqa: F401 — _RECITATION_SENTINEL re-exported for back-compat
    _RECITATION_SENTINEL,
    RecitationRegrounder,
)
from .signals import (  # noqa: F401 — re-exported for back-compat (moved to signals.py)
    _BOOKKEEPING_TOOLS,
    _NON_PRODUCTIVE_TOOLS,
)
from .stuck import StuckDetector, StuckThresholds
from .turn_control import (  # noqa: F401 — _CONTINUE_OPTION_ID re-exported for back-compat
    _CONTINUE_OPTION_ID,
    MetaToolHandlers,
    Valve,
)
from .view_render import (  # noqa: F401 — re-exported for back-compat (moved to view_render.py)
    _WS_MAX_FILES,
    _WS_PER_FILE_CHARS,
    _WS_READ_TIMEOUT_S,
    _WS_TOTAL_CHARS,
)

_LOG = logging.getLogger("disco.loop")

# _sleep / _DRIVER_RETRY_BACKOFFS_S / _STUCK_ESCAPE_TEMP moved to loop/driver.py
# (with the drive step that uses them).


# `_BOOKKEEPING_TOOLS` + `_NON_PRODUCTIVE_TOOLS` now live in loop/signals.py
# (the pure log-derived signal helpers that use them moved there too).


# _BOOKKEEPING_STREAK_* / _BOOKKEEPING_PLAN_SLACK / _PROPOSE_PLAN_UPDATE_REPEAT_CAP /
# _CONTINUE_OPTION_ID moved to loop/turn_control.py (with the valves + meta-tool
# handlers that use them); _CONTINUE_OPTION_ID re-exported above for back-compat.
# _FINISH_VERIFY_CAP / _DOD_REFUSAL_CAP moved to loop/finish.py (re-exported above).
# _WS_* snapshot budget constants moved to loop/view_render.py with the snapshot fn.

_DEFAULT_VETO_FEEDBACK = (
    "<system-reminder>\n"
    "The goal does not appear complete yet. Continue working toward it — a stop "
    "hook refused the FINISHED transition.\n"
    "</system-reminder>"
)

# Module-level constant so `ModelExecutionPolicy.standard()` is evaluated once
# and the per-parameter call doesn't trip ruff's B008 rule (function call in
# default). The policy is a frozen dataclass — safe to share as a singleton.
_DEFAULT_MODEL_POLICY: ModelExecutionPolicy = ModelExecutionPolicy.standard()

_DESIGN_DIRECTION_SITE_APP_HINTS: tuple[str, ...] = (
    "site",
    "website",
    "web site",
    "landing page",
    "homepage",
    "web page",
    "static page",
    "app",
    "web app",
    "application",
    "dashboard",
    "portal",
    "prototype",
    "pwa",
    "mobile",
    "ios",
    "android",
    "saas",
    "e commerce",
    "store",
    "shop",
    "portfolio",
    "restaurant",
    "event page",
    "form",
    # Broadened so ordinary small-business/site briefs also seed a design direction
    # (palette + art-direction keywords), not just ones that literally say "website".
    # Without this, "a page for my bakery" got NO direction and the art fell flat.
    "page",
    "brand",
    "business",
    "company",
    "agency",
    "studio",
    "blog",
    "gallery",
    "menu",
    "booking",
    "cafe",
    "coffee",
    "bakery",
    "salon",
    "shop page",
    "microsite",
    "product page",
    "marketing",
)
_DESIGN_DIRECTION_DECK_HINTS: tuple[str, ...] = (
    "deck",
    "slide",
    "slides",
    "slide deck",
    "presentation",
    "powerpoint",
    "pptx",
    "keynote",
)

# C6 — recitation cadence (Manus telemetry: re-emitting the plan/objective
# tail-recap on every iteration wastes ~1/3 of actions with no behavior
# change). Borrow the cadence math from smolagents' `planning_interval` ONLY:
# fire on a fixed interval, e.g. `(step_number - 1) % interval == 0`. We do
# NOT borrow smolagents' re-planning/steering behavior — Disco has a STRICT
# no-automatic-nudge invariant (commit c97c1b3) and the recap is a passive
# PASSIVE re-render of the SAME plan/checklist, never new instructions/goals.
# The content (built in view.py:_recitation_message) is unchanged — only the
# FREQUENCY of emission changes here.
_RECITATION_CADENCE_DEFAULT = 5
# _RECITATION_SENTINEL moved to loop/recitation.py (with the gate that uses it);
# re-exported via the import at the top of this module for back-compat.

# HS-03 — scheduled facts re-grounding (assist tier only). A weak model that
# has drifted through dozens of actions can lose track of the stable facts
# (goal, plan state, recently-touched files). The fix mirrors the smolagents
# `planning_interval` pattern: every N actions, re-anchor with a short
# RECAP. The recap is FACTS ONLY — no imperative/steer language — so the
# no-automatic-nudge invariant (commit c97c1b3) survives. Assist OFF
# (capable-model default) is byte-identical to today: the gate is closed
# and the helper is never called. Post-resume the recap fires ONCE on the
# very first step (a pause may have lost context). Cadence thereafter:
# `_HS03_REGROUND_INTERVAL` actions since the last resume. A per-boundary
# guard (the last-emitted action count) prevents re-firing on consecutive
# steps at the same boundary. Interval chosen at 12 (vs. C6's 5) so the
# recaps don't pile up: the C6 recap fires every 5 steps for plan state;
# the HS-03 recap fires every 12 steps and carries the broader
# goal/files/constraints anchors a drifted model most often loses.
_HS03_REGROUND_INTERVAL = 12

# Statuses at which the loop yields control back to the caller at a checkpoint.
_TERMINAL_FOR_NOW = frozenset(
    {
        ConversationStatus.PAUSED,
        ConversationStatus.IDLE,
        ConversationStatus.FINISHED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        # ask_user halts the loop — kicks must not race past the gate. Both the
        # pick-a-card (DECISION) and the free-form (QUESTION) Ask-gates re-kick
        # only on the user's reply.
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.AWAITING_USER_QUESTION,
    }
)

# Injected (as an implicit system-reminder) when the planner answers in prose
# instead of calling submit_plan: in PLANNING mode the terminal moves are to
# PROPOSE a structured plan OR ask the user for a detail you genuinely need
# first. Reads (file_list/file_read/search/extract) fall through and never
# trigger this — only a tool-less prose response does.
_PLAN_NUDGE = (
    "<system-reminder>\n"
    "Still in PLANNING mode — no plan has been proposed yet. To advance, either "
    "(a) call the `submit_plan` tool with a summary, ordered steps, and a markdown "
    "`context` block, or (b) if required details are genuinely missing and you "
    "cannot plan well without them (the user named something only they know — a "
    "color, a credential, a target, a file that isn't here), call `ask_user` with "
    "a clear `question` (for ONE missing detail) or `questions_v2` (for ONE "
    "batched structured intake round, up to four questions) to get them BEFORE "
    "planning. Prefer asking over guessing on "
    "details the user explicitly required. You may also keep reading (file_list, "
    "file_read, search, extract) for more context, but a prose reply alone doesn't "
    "advance the conversation.\n"
    "</system-reminder>"
)

_WORKFLOW_ROUTER_PLAN_NUDGE = (
    "<system-reminder>\n"
    "Still in WORKFLOW ROUTER phase. Your next response must be exactly one tool "
    "call: `enter_workflow`, `needs_input`, or `draft_workflow`. If you are "
    "already inside a workflow run and the selected workflow cannot satisfy the "
    "goal, call `workflow_abort`.\n"
    "</system-reminder>"
)

# Emitted when a REVISION re-plan has been narrated >= _REVISION_FORCE_SUBMIT_K times
# without a submit_plan call (the soft _PLAN_NUDGE was ignored). This is paired with a
# durable StatusEvent(detail="force_submit_plan") marker so the NEXT planning step's tool
# set is narrowed to submit_plan ONLY (driver.tools_for_step reads the marker) — the model
# must submit the plan it has already described instead of looping in prose. The marker is
# in the event log, so a RESUMED build replays it and stays forced (no soft-nudge loop).
_FORCE_SUBMIT_DIRECTIVE = (
    "<system-reminder>\n"
    "Call the `submit_plan` tool NOW with a NON-EMPTY `steps` array — the plan you last "
    "submitted had no steps, which cannot be executed. Give the concrete change(s) as "
    'ordered steps, e.g. submit_plan(summary="…", steps=["Change every call-to-action '
    "button label to 'Get Started'\"]). Even a SINGLE step is enough for a small revision — "
    "one step naming the exact edit is a valid, complete plan. `submit_plan` is the only "
    "available action; do not reply in prose and do not submit an empty steps list.\n"
    "</system-reminder>"
)
# After this many consecutive prose-only nudges during a REVISION re-plan, escalate to the
# forced-submit recovery (narrow tools to submit_plan). Gated default-ON; kill switch
# DISCO_REVISION_FORCE_SUBMIT=0.
_REVISION_FORCE_SUBMIT_K = 2
_INVALID_PLAN_DONE_CONDITION_CAP = 3

# Bug 12 (§11.4) — refusal for a mutating tool that reaches the apply boundary
# AFTER a change/revision steer landed mid-step (the in-flight write-through race).
# The build has been put back into PLANNING; the model must submit a revised plan.
_MIDSTEP_STEER_REFUSAL = (
    "<system-reminder>\n"
    "REFUSED: `{tool}` was not applied. A change request arrived while you were "
    "mid-step, so this action would have landed on the OLD, now-stale plan. The "
    "build has re-entered PLANNING. Fold the new request into a REVISED plan and "
    "call `submit_plan`; once it is approved you can apply the change. You may also "
    "read (file_read/file_list/search/extract) or ask/questions_v2 first.\n"
    "</system-reminder>"
)

# _STUCK_ESCAPE_TEMP moved to loop/driver.py (with the drive step that applies it).


def _planning_tool_refusal_message(
    tool_name: str, *, streak: int, read_calls_remaining: int
) -> str:
    detail = (
        f"REFUSED: `{tool_name}` is not available in PLANNING mode. No workspace "
        "mutation or execution is allowed before plan approval. Call `submit_plan`, "
        "use a safe read tool (file_read/file_list/search/extract), or ask/questions_v2 "
        "if details are missing."
    )
    if streak >= _PLANNING_TOOL_REFUSAL_ESCALATE_AT:
        detail += (
            f"\n\nPlanning-mode refusal {streak}: You have {read_calls_remaining} "
            "read calls remaining. Your ONLY valid next action is `submit_plan` "
            "with your plan as-is - do it now; exploration can continue during "
            "execution."
        )
    if streak >= _PLANNING_TOOL_REFUSAL_NARROW_AT:
        detail += "\nThe next turn will offer only `submit_plan` + `file_read`."
    return f"<system-reminder>\n{detail}\n</system-reminder>"


def _read_churn_nudge_message(path: str, count: int) -> str:
    if count == 5:
        return (
            f"You have read {path} {count} times in small windows without changing "
            "anything. Read it ONCE whole (omit offset/limit — files under the size "
            "cap fit in a single read), then act."
        )
    if count == 10:
        return (
            f"{count} small reads of {path}, still no change. STOP paging through it. "
            f"Your next action must be a whole-file read of {path}, an edit, or a "
            "plain statement of what is blocking you."
        )
    return (
        f"FINAL WARNING: {count} reads of {path} with no action. From the 20th read "
        "on, further small reads of this file count as no-ops and will pause the "
        "run. Act now."
    )


def _hint_text(text: str) -> str:
    return " " + " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split()) + " "


def _has_any_hint(text: str, hints: tuple[str, ...]) -> bool:
    haystack = _hint_text(text)
    return any(_hint_text(hint) in haystack for hint in hints)


def _design_direction_brief_text(plan: PlanEvent, events: list[Event]) -> str | None:
    parts: list[str] = [plan.summary, plan.context or ""]
    for step in plan.steps:
        parts.append(step.title)
        detail = (getattr(step, "detail", "") or "").strip()
        if detail:
            parts.append(detail)
    for event in events:
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            content = event.message.content
            if isinstance(content, str):
                parts.append(content)
    brief = "\n".join(part for part in parts if part.strip())
    if not brief or _has_any_hint(brief, _DESIGN_DIRECTION_DECK_HINTS):
        return None
    if _has_any_hint(brief, _DESIGN_DIRECTION_SITE_APP_HINTS):
        return brief
    return None


# _EXECUTION_NUDGE moved to loop/finish.py (with the execution-nudge gate).

# GAP B turn-taking tools — notify (non-blocking progress / mid-run reply) and
# finish (the explicit, affirmative terminal move). Both are intercepted by the
# loop and never reach the executor.
# The `remember` virtual tool — durable memory. When the agent learns a fact it
# will need LATER in the SAME run (a chosen library version, a discovered build
# command, a constraint the user stated, a dead-end to avoid), it records it
# here. The loop intercepts the call and emits a pinned KnowledgeEvent: pinned
# means the fact is EXEMPT from condensation, so it survives even after the raw
# transcript that taught it is summarized away. This is the standing-memory
# channel that backs MEMORY.md-style recall without a separate store — the View
# already injects KnowledgeEvents back into context every step.
_REMEMBER_DESCRIPTION = (
    "Record a durable fact you will need LATER in this run — a decision you made "
    "(a chosen library/version), something you discovered (the build command, an "
    "API shape, a path), a constraint the user stated, or a dead-end to avoid. "
    "Unlike normal observations, a remembered fact is PINNED: it survives context "
    "condensation, so you won't forget it on a long run. Keep each `fact` to one "
    "or two sentences. Optionally set `scope` to a keyword/path the fact applies "
    "to. This does NOT stop the run — you keep working right after."
)
_REMEMBER_SCHEMA = {
    "type": "object",
    "properties": {
        "fact": {
            "type": "string",
            "description": "The durable fact to remember (one or two sentences).",
        },
        "scope": {
            "type": "string",
            "description": "Optional applicability hint — a keyword or path the fact applies to.",
        },
    },
    "required": ["fact"],
}
# The `serve` virtual tool — the finished-artifact HANDOFF. When the agent has
# produced something the user should see/use (a built site, a running app, a
# generated report/file), it calls `serve` to mark the deliverable. The loop
# intercepts the call and emits a DeliverableEvent that the UI renders as a real
# handoff (Open the live app / Download the files) — the difference between "the
# run ended" and "here is your thing." Non-blocking: the agent keeps working (it
# usually serves, then verifies, then finishes).
_SERVE_DESCRIPTION = (
    "Hand off a finished deliverable to the user. Call this when you've produced "
    "something they should open or download. Set `kind`='app' for a runnable "
    "result they open in the live preview (a built site / running dev server "
    "rooted at `path`) — make sure your server is serving on port 8000 (the "
    "workspace auto-serves the 'preview' session there by default). Set "
    "`kind`='files' for artifacts to download (`path` = the file or folder). "
    "Give a short human `title`. This does NOT end the run — serve the "
    "deliverable, verify it, THEN call finish."
)
_SERVE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Short human label, e.g. 'Landing page' or 'Sales report'.",
        },
        "path": {
            "type": "string",
            "description": "Workspace-relative path: app root (kind=app) or file/folder (files).",
        },
        "kind": {
            "type": "string",
            "enum": ["app", "files"],
            "description": "'app' = open in live preview; 'files' = download. Default 'app'.",
        },
        "url": {
            "type": "string",
            "description": (
                "Optional: the canonical URL the deliverable is reachable at (a deploy "
                "target / tunnel / live address). Surfaced as an 'Open deployed app' link."
            ),
        },
    },
    "required": ["title", "path"],
}
_FINISH_DESCRIPTION = (
    "Declare the task COMPLETE and end the run. Call this ONLY when every plan "
    "step is done and verified — it is the single affirmative way to finish. A "
    "plain message without this tool does NOT end the run. Provide a short "
    "`summary` of what you built / accomplished. STRONGLY PREFERRED: attach a "
    "`verify` shell command that proves the deliverable works (the build "
    "compiles, the tests pass, the server responds) — exit 0 means good. If it "
    "fails you'll see exactly what broke and should fix it before finishing; after "
    "a few failures the run finishes anyway with a warning, so make your check real "
    "and your fix correct rather than relying on a broken verify to pass."
)
_FINISH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Short summary of what was accomplished."},
        "verify": {
            "type": "string",
            "description": (
                "Optional check that proves completion (exit 0 = success). One of: a "
                "shell command (`npm test`, `python -m pytest -q`); for a STATIC page "
                "the literal `static` (checks index.html exists + parses) or "
                "`static:<path>`; or for a RUNNING web app the literal `app` (GETs "
                "http://localhost:8000/ and requires HTTP 200 + a non-empty body) or "
                "`app:<url>` for another address — make sure your server is serving on "
                "port 8000 first. If it fails, the finish is refused and you must fix the problem."
            ),
        },
    },
    "required": [],
}


def _finish_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="finish", description=_FINISH_DESCRIPTION, parameters_schema=_FINISH_SCHEMA
    )


def _finish_alias_tool_spec(alias: str):
    """A FRESH ToolSpec for the contract finalizer alias — same schema/description as
    `finish`, contract-specific name. Built per-alias; never mutates the cached finish
    singleton."""
    from ..llm.types import ToolSpec

    return ToolSpec(name=alias, description=_FINISH_DESCRIPTION, parameters_schema=_FINISH_SCHEMA)


def _workflow_finish_tool_spec(workflow_run: WorkflowRun, *, name: str = "finish"):
    """Build the finish surface approved for this sealed workflow.

    Unlike the generic Build finish tool this is intentionally not cached: the
    presence of ``verify`` is part of each workflow definition's sealed scope.
    """
    from ..llm.types import ToolSpec

    return ToolSpec(
        name=name,
        description=workflow_finish_tool_description(workflow_run.definition),
        parameters_schema=workflow_finish_tool_schema(workflow_run.definition),
    )


def _remember_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="remember", description=_REMEMBER_DESCRIPTION, parameters_schema=_REMEMBER_SCHEMA
    )


def _serve_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(name="serve", description=_SERVE_DESCRIPTION, parameters_schema=_SERVE_SCHEMA)


# _STATIC/_APP verify-command builders moved to loop/finish.py (re-exported above).


_FINISH_TOOL_SPEC = None
_REMEMBER_TOOL_SPEC = None
_SERVE_TOOL_SPEC = None


def _finish_tool_singleton():
    global _FINISH_TOOL_SPEC
    if _FINISH_TOOL_SPEC is None:
        _FINISH_TOOL_SPEC = _finish_tool_spec()
    return _FINISH_TOOL_SPEC


def _remember_tool_singleton():
    global _REMEMBER_TOOL_SPEC
    if _REMEMBER_TOOL_SPEC is None:
        _REMEMBER_TOOL_SPEC = _remember_tool_spec()
    return _REMEMBER_TOOL_SPEC


def _serve_tool_singleton():
    global _SERVE_TOOL_SPEC
    if _SERVE_TOOL_SPEC is None:
        _SERVE_TOOL_SPEC = _serve_tool_spec()
    return _SERVE_TOOL_SPEC


# C20 — `delegate_explore`: a read-only Explore/Plan helper the build/agent
# loop can dispatch+join. A bounded subagent fan-out: the driver calls it,
# the loop dispatches a constrained helper task (read-only tools only —
# file_read / file_list / search / extract), the helper's response is folded
# back as a normal ObservationEvent, and the result is visible to the driver
# on its next turn. The fan-out is BOUNDED by a per-run-segment count cap
# (mirrors the other loop caps — finish_verify / dod_refusal / propose_plan
# repeat / bookkeeping streak). The cap is the single source of truth
# (`_FANOUT_MAX_PER_RUN`); the engine enforces it; the builtin tool stub
# stays cap-agnostic so a regression on the cap is testable on the engine
# side without faking through the tool. NO RECURSION: the helper does not
# itself call `delegate_explore` (its tools are restricted to the read-only
# set, and the engine's interceptor would still apply the cap on a nested
# call). The cap is reset on every `run()` entry (per-segment, like
# `_finish_verify_refusals`) so a resume/steer gets a fresh budget.
_FANOUT_MAX_PER_RUN = 3
# _FANOUT_INPUT_MAX_CHARS moved to loop/observe.py (with run_fanout); re-exported
# at the top of this module for back-compat.
# The reserved option id for the fan-out cap-refusal path: we don't synthesize
# a decision gate (no human in the loop), we emit a system-reminder and
# fall-through to the next step. The cap is enforced by simple refusal +
# feedback, the same shape the (c.3) bookkeeping-stuck nudge uses.
_DELEGATE_EXPLORE_DESCRIPTION = (
    "Dispatch a bounded, read-only Explore/Plan helper task and fold its "
    "result back as an observation on your next turn. Use this when you "
    "need a focused second look at the workspace BEFORE you commit to an "
    "action — e.g. 'which files would the new helper need to import "
    "from?', 'is the function I want to call already defined somewhere in "
    "the codebase?', 'what does the existing test for X look like?'. The "
    "helper sees ONLY: (a) your `question`, (b) the optional `context` you "
    "pass, (c) the read-only tools file_read / file_list / search / "
    "extract. It cannot mutate workspace state, cannot run shell, cannot "
    "write files, and cannot ask back questions. The result is folded "
    "back as a normal tool observation. The dispatch is BOUNDED: a "
    "per-run-segment cap limits how many times you can call this in a "
    "single run (exceeding it is refused with feedback). Use it "
    "sparingly — for ONE focused question at a time, not a full plan."
)
_DELEGATE_EXPLORE_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "One focused question the helper should answer. Keep it "
                "small and well-scoped — the helper has a tight prompt "
                "and cannot ask back."
            ),
        },
        "context": {
            "type": "string",
            "description": (
                "Optional markdown: what the helper needs to know — file "
                "paths to look at, constraints from the user, the shape "
                "of the answer you want. The driver fills this in based "
                "on what it already knows; the helper does not see the "
                "rest of the conversation."
            ),
        },
    },
    "required": ["question"],
}


def _delegate_explore_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="delegate_explore",
        description=_DELEGATE_EXPLORE_DESCRIPTION,
        parameters_schema=_DELEGATE_EXPLORE_SCHEMA,
    )


_DELEGATE_EXPLORE_TOOL_SPEC = None


def _delegate_explore_tool_singleton():
    global _DELEGATE_EXPLORE_TOOL_SPEC
    if _DELEGATE_EXPLORE_TOOL_SPEC is None:
        _DELEGATE_EXPLORE_TOOL_SPEC = _delegate_explore_tool_spec()
    return _DELEGATE_EXPLORE_TOOL_SPEC


# _last_productive_seq / _is_web_deliverable / _browser_verified / _latest_browser_error
# + _DoDWorkspaceUnavailable moved to loop/finish.py (re-exported above).


class AgentLoop:
    """[CONTRACT] The orchestrator. See module docstring + §2 state machine."""

    def __init__(
        self,
        conversation_id: str,
        store: EventStore,
        agent: Agent,
        executor: ToolExecutor,
        router: LLMRouter,
        analyzer: SecurityAnalyzer,
        policy: ConfirmationPolicy,
        condenser: Condenser,
        summarizer: Summarizer,
        *,
        mode: OperatingMode,
        max_iterations: int = 500,
        stop_hooks: list[StopHook] | None = None,
        stuck_thresholds: StuckThresholds | None = None,
        veto_feedback: str = _DEFAULT_VETO_FEEDBACK,
        planning_tools: frozenset[str] = frozenset(),
        plan_tool: str = "submit_plan",
        execution_mode: OperatingMode = OperatingMode.LONG_HORIZON,
        autonomous: bool = False,
        model_policy: ModelExecutionPolicy = _DEFAULT_MODEL_POLICY,
        # P6 — the active Build contract's verification finalizer name (e.g.
        # "ready_for_app_verification"). When set, it is advertised + recognized as a
        # per-kind ALIAS of the `finish` virtual tool, routed through the SAME host-truth
        # finish gate (no self-cert). None ⇒ no contract governs ⇒ only plain `finish`
        # (a plain/CUSTOM build NEVER fabricates a finalizer alias).
        finish_alias: str | None = None,
        # C6 — cadence for the plan/objective tail-recap (default: every 5
        # model turns). Set to 1 to recover the old "recite every step"
        # behavior; 0 / negative are clamped to 1. The smolagents math is
        # `(step_number - 1) % interval == 0` (1-indexed: step 1, 1+N,
        # 1+2N, …). 0-indexed: 0, N, 2N, … — equivalent boundary set.
        recitation_cadence: int = _RECITATION_CADENCE_DEFAULT,
        # HS-03 — cadence for the scheduled facts re-grounding (default:
        # every 12 actions since the last resume). Mirrors the
        # `recitation_cadence` seam: a test or operator can pin a
        # smaller / larger interval without code changes. The brief's
        # "e.g. 12" is the production default; tests inject a smaller
        # value to keep the test loop tight. Set to 1 to recover the
        # "re-ground on every step" behavior (NOT recommended — a
        # re-ground on every step would be a token-bloat disaster; the
        # point of the cadence is the throttling). 0 / negative are
        # clamped to 1.
        reground_cadence: int = _HS03_REGROUND_INTERVAL,
        # C1c — DoD evaluator factory (test seam; see _finish_dod_gate_passed).
        # The default (None) builds a DoDEvaluator over the executor's sandbox
        # workspace_root; tests inject a fake-seamed evaluator.
        dod_evaluator_factory: (
            Callable[[], DoDEvaluator | Coroutine[Any, Any, DoDEvaluator]] | None
        ) = None,
        # REL-1c — host-owned verifier shadow seam. None means no host verifier
        # is available and the existing finish flow is byte-identical.
        host_verifier: HostVerifier | None = None,
        host_verify_timeout_s: float = 30.0,
        host_verifier_verdict_hook: (
            Callable[[VerifierVerdictEvent], Awaitable[None]] | None
        ) = None,
        host_verify_authoritative: bool | None = None,
        verifier_judge: VerifierJudge | None = None,
        verifier_judge_timeout_s: float = 30.0,
        workflow_run: WorkflowRun | None = None,
        quiet: bool = False,
    ) -> None:
        # Autonomous mode (issue A): no human is available to answer questions or
        # approve plans (headless / unattended runs). Default False = today's
        # interactive behavior, fully unchanged. When True: ask_user/clarify are
        # withheld from the tool schema, the plan is auto-approved inline, and the
        # circuit-breaker's "hand off to the user" becomes a clean forfeit (STUCK)
        # instead of an indefinite AWAITING_USER_DECISION stall.
        self._autonomous = autonomous
        self._quiet = quiet
        # P6 — the contract finalizer alias for `finish` (None when no contract governs).
        self._finish_alias = finish_alias
        self._workflow_run = workflow_run
        # Execution policy (T1): the SINGLE source of truth for model-tier execution.
        # `_assist` is a read-only property delegating to `_model_policy.assist`.
        self._model_policy = model_policy
        self.conversation_id = conversation_id
        self.store = store
        self.agent = agent
        self.executor = executor
        self._router = router  # held for wiring symmetry; the Agent wraps it
        self.analyzer = analyzer
        self.policy = policy
        self.condenser = condenser
        self.summarizer = summarizer
        self.mode = mode
        self.max_iterations = max_iterations
        # Plan-mode wiring (Build). Defaults are inert: with no planning_tools the
        # mode filter and the plan intercept never fire, so Research and existing
        # tests behave exactly as before.
        self._planning_tools = planning_tools  # tools visible ONLY while planning
        self._plan_tool = plan_tool  # the structured-plan signal, intercepted
        self._execution_mode = execution_mode  # the mode an approved plan runs in
        self._plan_nudges = 0  # consecutive nudges while planning (safety cap)
        # Forced-submit recovery for revision re-plans (default ON; kill switch).
        self._revision_force_submit_enabled = os.environ.get(
            "DISCO_REVISION_FORCE_SUBMIT", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        self._plan_explore_reads = 0  # (B2/B6) consecutive PLANNING reads w/o a plan
        self._execution_nudges = 0  # consecutive "you must act" nudges in execution
        self._browser_verify_refusals = 0  # consecutive browser-verification refusals
        self._identical_plan_revisions = 0  # C8 (T11): consecutive identical-steps
        # propose_plan_update auto-approvals in
        # autonomous mode. Increments when the
        # new plan's steps match the immediately
        # prior plan's; resets on a different
        # (incl. appended) plan. The cap lives
        # at module-level so tests can pin it.
        self._finish_verify_refusals = 0  # consecutive finish-verify failures (cap-3 release)
        self._finish_verify_strips = 0  # malformed verifies auto-stripped (anti-gaming cap)
        self._workflow_output_contract_refusals = 0
        # C20 — `delegate_explore` count, per run segment. Reset in run() so a
        # resume/steer gets a fresh budget (mirror `_finish_verify_refusals`).
        # The cap is module-level so tests can pin it. The cap bounds the
        # fan-out — a model cannot recurse (the helper is read-only and the
        # call site is always the loop's intercept path).
        self._fanout_count = 0
        self._fanout_max = _FANOUT_MAX_PER_RUN
        # Actionless-step breaker cap (DEFECT-4)
        self._ACTIONLESS_BREAK_CAP: int = 3
        # Steps consumed with NOTHING persisted to the log (empty-args serve,
        # blank remember fact, empty-thought noop, duplicate deliverable) —
        # invisible to every event-derived detector incl. the stuck detector,
        # so an instance counter is the only honest way to see the spin.
        # Reset when a real ActionEvent is built (site h) and at run() entry.
        self._invisible_steps = 0
        # GAP B backstop: max tool-less prose turns in a row before the loop ends
        # the run (a talking-without-acting model can't advance max_iterations).
        self._max_consecutive_noops = 6
        # Circuit breaker (Cluster 2): after this many consecutive failures the
        # harness hands off to the user (AWAITING_USER_DECISION) instead of
        # grinding. Set above StuckDetector's identical-repeat threshold (3) so
        # identical loops still STUCK first; this catches the DISTINCT-failure case.
        self._circuit_breaker_threshold = 4
        # Auto-continue cap: when the agent declares finished but the plan
        # isn't done, the loop re-kicks itself this many times before landing
        # FINISHED with partial-plan detail. The user shouldn't have to poke
        # the model to keep going; this is the harness driving the loop forward.
        self._auto_continue_cap = 3
        self._stop_hooks = list(stop_hooks or [])
        self._stuck = StuckDetector(stuck_thresholds, assist=model_policy.assist)
        # C6/HS-03/C5 collaborator: recitation cadence, scheduled re-grounding,
        # and the MEMORY write-through mirror. Reads the loop's cadence counters.
        self._recit = RecitationRegrounder(self)
        # View materialization collaborator (microcompact → condense → recap
        # gate → F8 → snapshot append). Reads condenser/summarizer + self._recit.
        self._view = view_render.ViewBuilder(self)
        # C18 advisory plan-step done-condition collaborator.
        self._plan_cond = PlanStepConditions(self)
        # Execute-and-observe (§4.1) + §8 hard-reset + C20 fan-out collaborator.
        self._observe = Observer(self)
        # The model-driving step collaborator (tool visibility + agent.step +
        # requery ladder + transient backoff + context-window hard-reset).
        self._driver = Driver(self)
        # The finish pipeline collaborator (verify-on-finish, C1c DoD gate,
        # execution nudge, browser-verify gate, auto-continue finalizer).
        self._finish = FinishGate(self)
        # Turn-taking control collaborators (co-located: both poke the shared
        # _invisible_steps counter). Valve = spin/stuck/circuit-breaker guards;
        # MetaToolHandlers = the virtual meta-tool run-loop intercepts.
        self._valve = Valve(self)
        self._meta = MetaToolHandlers(self)
        # Plan / alternatives builders (submit_plan → PlanEvent + C18 harvest;
        # ask_user options → AlternativesEvent).
        self._planner = Planner(self)
        self._veto_feedback = veto_feedback
        # C1c — DoD evaluator gate (wires the C1b fresh-context judge into the
        # finish branch). The factory returns a fully-configured evaluator; the
        # default builds one over the executor's sandbox workspace_root. Tests
        # inject a fake-seamed evaluator via the same hook. None means "use the
        # default factory" (the evaluator is still wired when a DoD spec exists).
        # When no DoD spec exists for the conversation, the gate is a no-op
        # (legacy byte-identical path) — see _finish_dod_gate_passed.
        self._dod_evaluator_factory: (
            Callable[[], DoDEvaluator | Coroutine[Any, Any, DoDEvaluator]] | None
        ) = dod_evaluator_factory
        self._host_verifier = host_verifier
        self._host_verify_timeout_s = float(host_verify_timeout_s)
        self._host_verifier_verdict_hook = host_verifier_verdict_hook
        self._host_verify_authoritative = (
            host_verify_authoritative_enabled()
            if host_verify_authoritative is None
            else bool(host_verify_authoritative)
        )
        self._verifier_judge = verifier_judge
        self._verifier_judge_timeout_s = float(verifier_judge_timeout_s)
        # Consecutive DoD-refusal streak (telemetry; the gate has no cap — the
        # loop's max_iterations + the user's kill switch are the ultimate exit,
        # same as the browser-verify and execution-nudge gates).
        self._dod_refusals = 0
        # REL-RC-O — consecutive dictated-content finish refusals. The actual
        # literals are event-derived; this counter only bounds refuse/continue.
        self._dictated_content_refusals = 0
        self._lock = asyncio.Lock()
        # WALK-18 — cooperative pause flag. pause() SETS it WITHOUT taking
        # self._lock (so a pause lands while the in-flight turn holds the lock,
        # unlike cancel() which must take the lock to emit IDLE); the run() loop
        # observes it at its next step boundary, emits PAUSED, and returns.
        self._pause_requested = asyncio.Event()
        # F12 — cancel/steer/pause can wake the driver's otherwise idle
        # 10/30/90-second provider backoff without aborting an in-flight model
        # response or tool side effect. Control methods set this before waiting
        # on `_lock`; the driver releases the lock at the next safe checkpoint.
        self._retry_interrupt = asyncio.Event()
        # Watch-it-write sink (optional). The runtime wires this to the store's
        # ephemeral broadcast; when set, the driver's streamed tool-call arg
        # fragments are decoded into growing file-content frames and published
        # live (display-only, never persisted). None → no streaming (tests, CLI).
        self.stream_sink: Callable[[dict], None] | None = None
        # C6 — recitation cadence state. Per-run counters / fingerprints
        # used to decide whether the view.py tail-recap is appended on this
        # iteration. Reset in run() at the start of every run segment so a
        # resume/steer starts a fresh cadence. The signature is a stable
        # hash of (plan + per-step checklist) — DRIFT fires when the plan
        # is replaced OR the agent marks a step done/active.
        self._recitation_cadence = max(1, int(recitation_cadence))
        self._recitation_step_count = 0
        self._recitation_last_signature: str | None = None
        # HS-03 — scheduled facts re-grounding cadence. Mirrors the
        # C6 seam (testable, defaulted to the production constant).
        # The instance attribute is what the predicate inspects so a
        # test can pin a small cadence without monkey-patching the
        # module constant. Clamped to >= 1 (a cadence of 0 would
        # mean "every step" modulo zero, undefined; 1 is the
        # degenerate "fire on every step" case which the brief
        # explicitly disclaims as not the intent).
        self._hs03_reground_cadence = max(1, int(reground_cadence))
        # C18 — per-(plan_revision, step_index) advisory done-condition
        # predicates, populated in `_plan_from_args` from the optional
        # `done_condition` field on each `PlanStepInput`. The map is keyed
        # by (plan_revision, 1-based step index) so a re-plan (new
        # revision) cleanly supersedes the prior predicate set without a
        # stale match. Lookup happens in
        # `_maybe_emit_plan_step_done_condition_note` whenever a
        # `plan_step(idx, 'done')` ActionEvent is observed. Empty by
        # default — steps without a predicate behave exactly as today
        # (back-compat). Cleared on `approve_plan` reset (fresh segment).
        self._plan_step_predicates: dict[tuple[int, int], DoDPredicate] = {}
        # F4 — gated bootstrap observation. Fires at most ONCE per
        # conversation when assist is ON and we're about to make the first
        # model call. The flag persists across run() segments so a resume
        # never re-emits it (the model already saw it; re-firing would
        # be a redundant system reminder and a context-window penalty).
        # Assist OFF (default) leaves this flag dead — the gate is closed
        # before the detector is ever called.
        self._bootstrap_emitted: bool = False
        # HS-03 — scheduled facts re-grounding. Two pieces of state,
        # both assist-gated. Both are reset in run() so a fresh
        # run segment (which is what a resume/restart starts) gets a
        # fresh budget — the brief asks for "once right after a
        # resume" and that "once" is per-resume, not per-conversation.
        #   * _hs03_reground_post_resume_emitted: ONE-SHOT guard for
        #     the post-resume recap. Mirrors _bootstrap_emitted's
        #     shape (a boolean, flipped after the first eligible
        #     step), but RESET in run() (where _bootstrap_emitted is
        #     NOT reset) — a resume IS the moment we want to re-fire.
        #   * _hs03_reground_last_action_count: PER-BOUNDARY guard
        #     keyed off the action count. A boundary is
        #     `actions_since_last_resume % N == 0` (N ==
        #     _HS03_REGROUND_INTERVAL). The guard compares the
        #     CURRENT count to the last one and skips emit when
        #     equal — so two consecutive steps at the same boundary
        #     (e.g. count 12 on two steps in a row, which can happen
        #     if a step is a no-op) never produce two recap messages.
        #     Reset in run() so a fresh segment starts the count
        #     from -1 (no boundary yet).
        self._hs03_reground_post_resume_emitted: bool = False
        self._hs03_reground_last_action_count: int = -1

    # ---- policy accessors ---------------------------------------------------

    @property
    def _assist(self) -> bool:
        """Read-only shim for the legacy weak-model gate. All collaborators
        that read `self._loop._assist` continue to work without changes because
        this property exposes the same name they already access. The SINGLE
        source of truth is `_model_policy.assist`."""
        return self._model_policy.assist

    # ---- emission + small helpers -------------------------------------------

    # Tools whose streamed arguments carry a file body/edit replacement worth
    # watching assemble.
    _STREAMING_WRITE_TOOLS = ("file_write", "file_append", "write_file", "file_edit")
    # Coalesce threshold: don't publish a frame until this many new content chars
    # have accrued (or a newline appears) — keeps the type-out smooth without
    # firing a WS frame per 3-char model token. The trailing remainder below the
    # threshold is delivered by the authoritative ActionEvent, so nothing is lost.
    _STREAM_FLUSH_CHARS = 24

    def _build_stream_hook(self) -> StreamHook | None:
        """Delegates to Driver.build_stream_hook (self._driver). Kept as an
        instance method for tests."""
        return self._driver.build_stream_hook()

    async def _emit(self, event: Event) -> Event:
        return await self.store.append(self.conversation_id, event)

    async def _events(self) -> list[Event]:
        return await self.store.get_events(self.conversation_id)

    async def get_state(self) -> ConversationState:
        return await self.store.get_state(self.conversation_id)

    async def _event_by_id(self, event_id: str) -> Event | None:
        for e in await self._events():
            if e.id == event_id:
                return e
        return None

    def _recent(self, events: list[Event]) -> list[Event]:
        return events[-self._stuck.required_scan_window() :]

    def _workflow_router_phase_active(self) -> bool:
        scope = getattr(self.executor, "_scope", None)
        return getattr(scope, "preset", None) == "workflow_router"

    # ---- plan-mode helpers (Build) ------------------------------------------

    def _effective_mode(self, events: list[Event]) -> OperatingMode:
        return signals.effective_mode(
            events,
            execution=self._execution_mode,
            default=self.mode,
        )

    def _reconcile_mode_from_events(self, events: list[Event]) -> OperatingMode:
        mode = self._effective_mode(events)
        if self.mode != mode:
            _LOG.warning(
                "Reconciling loop mode for %s from %s to %s using event-log markers",
                self.conversation_id,
                self.mode.value,
                mode.value,
            )
            self.mode = mode
        return mode

    def _readonly_tool_names(self) -> frozenset[str] | None:
        """Delegates to Driver.readonly_tool_names (used by the Observer F9 gate)."""
        return self._driver.readonly_tool_names()

    def _tools_for_step(self, *, suppress_meta_tools: bool = False) -> list:
        """Delegates to Driver.tools_for_step. Kept as an instance method for tests."""
        return self._driver.tools_for_step(suppress_meta_tools=suppress_meta_tools)

    def _plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        """Delegates to Planner.plan_from_args (planning gate + propose intercept)."""
        return self._planner.plan_from_args(arguments, events)

    def _alternatives_from_args(
        self, arguments: dict, events: list[Event]
    ) -> AlternativesEvent | None:
        """Delegates to Planner.alternatives_from_args (the ask_user intercept)."""
        return self._planner.alternatives_from_args(arguments, events)

    async def _workspace_snapshot_message(self, events: list[Event]) -> LLMMessage | None:
        """Delegates to view_render.workspace_snapshot_message over the executor's
        sandbox. Kept as an instance method for tests + the ViewBuilder."""
        return await view_render.workspace_snapshot_message(
            getattr(self.executor, "sandbox", None), events
        )

    # ---- C6: recitation cadence + drift gate ---------------------------------

    # ---- C6 / HS-03 delegators → RecitationRegrounder (self._recit) ----------

    def _recitation_signature(self, events: list[Event]) -> str | None:
        return self._recit.recitation_signature(events)

    def _should_emit_recitation(self, events: list[Event]) -> bool:
        return self._recit.should_emit_recitation(events)

    def _gate_recitation(
        self, view: View, events: list[Event], *, context_pack_active: bool = False
    ) -> View:
        return self._recit.gate_recitation(view, events, context_pack_active=context_pack_active)

    def _should_emit_reground(self, events: list[Event]) -> bool:
        return self._recit.should_emit_reground(events)

    async def _maybe_emit_reground(self, events: list[Event]) -> list[Event]:
        return await self._recit.maybe_emit_reground(events)

    async def _materialize_view(self, events: list[Event]) -> View:
        """Delegates to ViewBuilder.build (self._view). Kept as an instance
        method for tests + run()."""
        return await self._view.build(events)

    def _f8_shrink_file_write_args(
        self, messages: list[LLMMessage], events: list[Event]
    ) -> list[LLMMessage]:
        """Delegates to view_render.f8_shrink_file_write_args (pure transform).
        Kept as an instance method for tests + the ViewBuilder."""
        return view_render.f8_shrink_file_write_args(messages, events)

    async def _hard_reset(self, events: list[Event]) -> bool:
        """Delegates to Observer.hard_reset (self._observe). Kept as an instance
        method for the driver-step call site."""
        return await self._observe.hard_reset(events)

    # ---- C5: MEMORY write-through + rehydrate-recovery ---------------------

    # Path of the on-disk mirror that survives a hard filesystem reset. The
    # directory is the harness's own — never written by the agent's tools
    # directly — so a hostile model can't tamper with the View's source of
    # truth by overwriting it.
    _MEMORY_PATH = ".disco/MEMORY.md"  # write target (renamed from legacy .pmx/)
    # A pre-rename workspace (resumed mid-upgrade) may still carry the old path;
    # the read-back + the write's read-existing fall back to it so the standing
    # memory migrates forward instead of being lost.
    _LEGACY_MEMORY_PATH = ".pmx/MEMORY.md"

    async def _write_pmx_memory_fact(self, scope: str, fact: str) -> None:
        await self._recit.write_pmx_memory_fact(scope, fact)

    # ---- execute-and-observe (§4.1) -----------------------------------------

    async def _execute_and_observe(self, action: ActionEvent) -> None:
        """Delegates to Observer.execute_and_observe (self._observe). Kept as an
        instance method for run()/confirm()/pick_alternative()/verify + tests."""
        await self._observe.execute_and_observe(action)

    async def _run_fanout(
        self, args: dict, events: list[Event], *, call_id: str = ""
    ) -> ToolResult:
        """Delegates to Observer.run_fanout. Kept as an instance method so tests
        can override the fan-out seam (`loop._run_fanout = ...`)."""
        return await self._observe.run_fanout(args, events, call_id=call_id)

    # ---- C18: advisory plan-step done-condition ----------------------------

    async def _maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        """Delegates to PlanStepConditions (self._plan_cond). Kept as an instance
        method for the execute-and-observe call site."""
        await self._plan_cond.maybe_emit_plan_step_done_condition_note(action)

    async def _finish_dod_gate_passed(self) -> bool:
        """Delegates to FinishGate.finish_dod_gate_passed (self._finish). Kept as an
        instance method for tests."""
        return await self._finish.finish_dod_gate_passed()

    async def _stop_allowed(self, state: ConversationState, events: list[Event]) -> bool:
        for hook in self._stop_hooks:
            if not await hook.allow_stop(state, events):
                return False
        return True

    # ---- the run loop (§4) --------------------------------------------------

    async def _post_noop_valve(self) -> Disp:
        """Delegates to Valve.post_noop_valve (self._valve). Called by FinishGate
        and the planning-mode gate."""
        return await self._valve.post_noop_valve()

    async def _maybe_apply_read_churn_valve(self, action: ActionEvent) -> Disp:
        if self.mode == OperatingMode.PLANNING:
            return Disp.FALLTHROUGH
        if action.tool_call is None or action.tool_call.tool_name != "file_read":
            return Disp.FALLTHROUGH
        events = await self._events()
        if not any(
            isinstance(event, ObservationEvent)
            and event.action_id == action.id
            and event.tool_result.success
            for event in events
        ):
            return Disp.FALLTHROUGH
        state = signals.read_churn_state(events)
        if state is None:
            return Disp.FALLTHROUGH
        if state.warning_count is not None:
            await self._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=_read_churn_nudge_message(state.path, state.warning_count),
                    ),
                    meta={
                        "diagnostic": signals.READ_CHURN_NUDGE_DIAGNOSTIC,
                        "path": state.path,
                        "count": state.warning_count,
                        "streak": state.warning_count,
                    },
                )
            )
            events = await self._events()
        if state.invisible_noops <= 0:
            return Disp.FALLTHROUGH
        self._invisible_steps = max(self._invisible_steps, state.invisible_noops)
        noops = signals.consecutive_noops(events) + self._invisible_steps
        if await self._valve.actionless_valve(events, noops):
            return Disp.HALT
        return Disp.FALLTHROUGH

    async def _land_blocked(
        self,
        *,
        reason: str,
        guidance: str = "",
        legacy_status: ConversationStatus = ConversationStatus.STUCK,
        legacy_detail: str | None = None,
    ) -> None:
        """Delegate all breaker dead-ends to the shared explain+ask lander."""
        await self._valve.land_blocked(
            reason=reason,
            guidance=guidance,
            legacy_status=legacy_status,
            legacy_detail=legacy_detail,
        )

    async def _maybe_synthesize_finish_after_actionless_pauses(
        self, state: ConversationState, events: list[Event]
    ) -> Disp:
        """REL-RC-P: before another resumed model turn, attempt finish when the
        event log shows repeated actionless pauses after productive work."""
        if self.mode == OperatingMode.PLANNING:
            return Disp.FALLTHROUGH
        if not signals.should_synthesize_finish_after_actionless_pauses(events):
            return Disp.FALLTHROUGH
        return await self._finish.synthetic_finish_after_actionless_pauses(state, events)

    async def _route_plan_approval_gate(self, plan: PlanEvent) -> Disp:
        """Route a newly-emitted PlanEvent through the normal approval gate."""
        if self._autonomous:
            # No human to approve → auto-approve INLINE, emitting the
            # exact same events approve_plan() would, so the event log
            # is identical whether a human or the harness approved.
            self.mode = self._execution_mode
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))
            await self._arm_dod_from_plan()
            await self._seed_context_from_plan()  # CXT-6: same as interactive approve_plan
            return Disp.CONTINUE
        await self._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                detail=plan.id,
            )
        )
        return Disp.HALT

    def _harvest_prose_plan(self, events: list[Event]) -> PlanEvent | None:
        steps = signals.harvest_prose_plan_steps(events)
        if not steps:
            return None
        instruction = signals.current_revision_instruction(events) or ""
        summary = signals.latest_prose_plan_summary(events) or instruction[:120] or "Proposed plan"
        return PlanEvent(
            summary=summary,
            steps=steps,
            revision=signals.next_plan_revision(events),
            context=(
                "Synthesized by REL-RC-N from the assistant's latest prose plan "
                "after the force-submit ladder was exhausted."
            ),
        )

    async def _gate_planning_mode(self, step: AgentStep, events: list[Event]) -> Disp:
        # (e.5) PLAN GATE — in PLANNING mode the planner has THREE valid moves:
        #   1. `submit_plan` → intercepted into a PlanEvent, loop halts for approval.
        #   2. Any OTHER tool in planning_tools (e.g. file_read, file_list, search,
        #      extract) → fall through to the normal action path; the planner explores
        #      before proposing (the Claude-Code-style "Phase 1: gather context").
        #   3. No tool call at all (a prose answer) → nudge back to planning.
        # The plan tool itself is NEVER executed.
        if self._reconcile_mode_from_events(events) == OperatingMode.PLANNING:
            tc = step.tool_call
            if (
                tc is not None
                and self._workflow_run is not None
                and tc.tool_name in ("skip", "needs_input")
            ):
                return Disp.FALLTHROUGH
            if tc is not None and tc.tool_name == self._plan_tool:
                self._plan_explore_reads = 0  # (B2/B6) a plan was proposed
                plan = self._plan_from_args(tc.arguments, events)
                condition_errors = validate_raw_plan_done_conditions(tc.arguments)
                condition_errors.extend(validate_plan_done_conditions(plan))
                if condition_errors:
                    # Do not persist or approve a plan whose machine conditions
                    # become impossible/unsupported immutable finish gates.  The
                    # transient C18 map was populated while parsing, so explicitly
                    # discard this rejected revision before the model retries.
                    self._planner.discard_plan_predicates(plan.revision)
                    start_seq = signals.current_planning_segment_start_seq(events)
                    prior = sum(
                        1
                        for event in events
                        if isinstance(event, StatusEvent)
                        and event.detail == "invalid_plan_done_conditions"
                        and (start_seq is None or (event.seq or 0) > start_seq)
                    )
                    attempt = prior + 1
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING,
                            detail="invalid_plan_done_conditions",
                        )
                    )
                    rendered = "\n".join(f"- {error}" for error in condition_errors)
                    if attempt >= _INVALID_PLAN_DONE_CONDITION_CAP:
                        await self._land_blocked(
                            reason="invalid_plan_done_conditions",
                            guidance=(
                                "The planner repeatedly submitted unsafe Definition-of-Done "
                                f"conditions ({attempt}/{_INVALID_PLAN_DONE_CONDITION_CAP}). "
                                "No plan was approved and no execution began. Correct these "
                                f"conditions before retrying:\n{rendered}"
                            ),
                            legacy_status=ConversationStatus.STUCK,
                            legacy_detail="invalid_plan_done_conditions",
                        )
                        return Disp.HALT
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    "<system-reminder>\n"
                                    "Your proposed plan was NOT accepted because its "
                                    "done_condition values would become immutable external "
                                    "finish gates and are unsafe or internally inconsistent:\n"
                                    f"{rendered}\n\n"
                                    "Submit a corrected plan. Do not replace a required "
                                    "directory with a file and do not guess a local preview "
                                    "port or a future/placeholder host. Use http_ok only "
                                    "with a concrete already-known FQDN/IP. Omit a condition "
                                    "when there is no safe, exact machine-checkable predicate. "
                                    f"Invalid plan attempt {attempt}/"
                                    f"{_INVALID_PLAN_DONE_CONDITION_CAP}.\n"
                                    "</system-reminder>"
                                ),
                            ),
                            meta={"blocking": "invalid_plan_done_conditions"},
                        )
                    )
                    return Disp.CONTINUE
                await self._emit(plan)
                # [REL-RC A1] A ZERO-STEP REVISION plan must NOT auto-approve. MiniMax-M3
                # frequently submits a revision plan with no parseable steps; auto-approving it
                # drops the model into execution with NO tracker → the actionless/monologue
                # breaker → STUCK (the 2/3 revise_after_finish soak failure). Route a zero-step
                # revision into the SAME force-submit recovery used for the prose-no-tool path:
                # escalate ONCE to force_submit_plan (next step's tools narrowed to submit_plan-
                # only + a hard directive demanding concrete steps); if it STILL submits zero
                # steps under force, emit a CONTROLLED, adjudicable terminal (STUCK detail=
                # "revision_no_concrete_steps") — NEVER accept the empty revision plan into
                # stranded execution, never fake an approval. An INITIAL zero-step plan (not a
                # revision) still auto-approves (summary-only is a legitimate simple-build state).
                if (
                    not plan.steps
                    and self._revision_force_submit_enabled
                    and signals.in_planning_for_revision(events)
                ):
                    if not signals.revision_force_submit(events):  # escalate once
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.RUNNING,
                                detail="force_submit_plan",
                            )
                        )
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(role="user", content=_FORCE_SUBMIT_DIRECTIVE),
                            )
                        )
                        self._plan_nudges = 0
                        return Disp.CONTINUE
                    # [REL-RC-F] Already forced + STILL zero steps. LAST-RESORT recovery: rather
                    # than STUCK, synthesize ONE concrete step from the USER's own revision
                    # instruction and proceed. This is NOT a faked approval — the step is the
                    # user's literal, adjudicable ask, and the DoD/finish gate still judges the real
                    # deliverable — and it is NOT a stranded execution — emitting a real 1-step
                    # PlanEvent means the approve path below arms the DoD tracker + seeds context
                    # from it (via _latest_plan), so the actionless/monologue breaker has a concrete
                    # target (the A1 hazard this replaces). Only if the instruction is unrecoverable
                    # do we keep the original controlled STUCK terminal.
                    instruction = signals.current_revision_instruction(events)
                    if instruction:
                        # Construct a FRESH PlanEvent (new id) — NOT plan.model_copy,
                        # which preserves the already-emitted empty plan's id, so
                        # _emit would dedup it (idempotent on (conversation_id, id))
                        # and _latest_plan would still return the EMPTY plan →
                        # stranded execution (Codex code-gate catch).
                        synth = PlanEvent(
                            summary=plan.summary or instruction[:120],
                            steps=[PlanStep(title=instruction[:200])],
                            revision=plan.revision + 1,
                            context=plan.context,
                        )
                        await self._emit(synth)
                        plan = synth  # fall through to the approve path with the 1-step plan
                    else:
                        await self._land_blocked(
                            reason="revision_no_concrete_steps",
                            guidance=(
                                "The planner submitted an empty revision plan after "
                                "the force-submit recovery, and there was no current "
                                "revision instruction to synthesize a concrete step from."
                            ),
                            legacy_status=ConversationStatus.STUCK,
                            legacy_detail="revision_no_concrete_steps",
                        )
                        return Disp.HALT
                return await self._route_plan_approval_gate(plan)
            if tc is None:
                # The planner spoke without calling a tool. PRESERVE the
                # prose first — this is how the agent acknowledges the
                # user's request conversationally before it starts
                # exploring/planning ("Got it — you want X; let me look
                # at the available APIs and think through the
                # architecture."). Without this the acknowledgment was
                # silently discarded and the user heard nothing back.
                # THEN append the nudge to keep it moving toward
                # submit_plan. No cap, no error — the loop's
                # max_iterations and the user's kill switch are the
                # ultimate exits. The counter stays for telemetry.
                if step.thought.strip() and not self._quiet:
                    await self._emit(
                        MessageEvent(
                            source=EventSource.AGENT,
                            message=LLMMessage(role="assistant", content=step.thought),
                        )
                    )
                    events = await self._events()
                if (
                    self._revision_force_submit_enabled
                    and signals.prose_plan_force_submit(events)
                    and not signals.prose_plan_harvested(events)
                    and not signals.plan_submitted_since_current_planning(events)
                ):
                    synth = self._harvest_prose_plan(events)
                    if synth is not None:
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(
                                    role="user",
                                    content=(
                                        "<system-reminder>\n"
                                        "REL-RC-N HARVEST: the planner stayed in"
                                        " PLANNING after the force-submit ladder"
                                        " and still did not call `submit_plan`."
                                        f" Synthesizing fresh PlanEvent revision"
                                        f" {synth.revision} from the latest"
                                        f" assistant prose plan with"
                                        f" {len(synth.steps)} step(s), then"
                                        " routing it through the normal plan"
                                        " approval gate.\n"
                                        "</system-reminder>"
                                    ),
                                ),
                            )
                        )
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.RUNNING,
                                detail="prose_plan_harvested",
                            )
                        )
                        await self._emit(synth)
                        self._plan_explore_reads = 0
                        self._plan_nudges = 0
                        return await self._route_plan_approval_gate(synth)
                # FORCED-SUBMIT RECOVERY (prose planning). When a planning segment
                # has been narrated >= K times without calling submit_plan, the soft
                # _PLAN_NUDGE is being ignored and the actionless valve would pause →
                # STUCK, with resume re-entering the same prose loop. Escalate ONCE to a
                # DURABLE marker (StatusEvent detail="force_submit_plan") + a hard
                # directive: the marker lives in the event log, so driver.tools_for_step
                # narrows the NEXT step's tools to submit_plan ONLY and a RESUMED build
                # replays the marker and stays forced (no soft-nudge loop). REL-RC-N
                # generalizes the marker to initial planning too; once the marker is
                # active, another prose-only turn is harvested into a fresh PlanEvent.
                if (
                    self._revision_force_submit_enabled
                    and signals.plan_nudges_since_current_planning(events)
                    >= _REVISION_FORCE_SUBMIT_K
                    and not signals.prose_plan_force_submit(events)  # escalate once
                    and not signals.plan_submitted_since_current_planning(events)
                ):
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING,
                            detail="force_submit_plan",
                        )
                    )
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(role="user", content=_FORCE_SUBMIT_DIRECTIVE),
                        )
                    )
                    self._plan_nudges = 0  # fresh runway for the forced-submit step
                    return Disp.CONTINUE
                self._plan_nudges += 1
                nudge = (
                    _WORKFLOW_ROUTER_PLAN_NUDGE
                    if self._workflow_router_phase_active()
                    else _PLAN_NUDGE
                )
                await self._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=nudge),
                    )
                )
                if await self._post_noop_valve() is Disp.HALT:
                    return Disp.HALT
                return Disp.CONTINUE
            # tc is a tool call that is NEITHER submit_plan NOR no-tool prose.
            # Only the planning allowlist may run before plan approval. That
            # allowlist is DERIVED FROM THE SAME read-only-capability ∩ name
            # intersection used for tool VISIBILITY (Driver.planning_allowed_tool_
            # names ↔ tools_for_step), so the gate and the advertised tools can
            # never drift: the read/explore tools (file_read/file_list/search/
            # extract — capability-and-allowlist gated), submit_plan (always, even
            # if unadvertised), and the virtual ask_user/questions_v2/clarify escape hatches.
            # ANY OTHER tool (file_write, shell, browser, serve, finish, remember,
            # notify_user, delegate_explore, ...) must NOT mutate the workspace or
            # execute before the plan is approved (spec §11.1/11.3/11.7). REJECT it
            # — never execute — with a RECOVERABLE observation the model sees:
            # record the attempted ActionEvent (so the assistant tool_call stays
            # PAIRED with a tool-role result — KV stability, same discipline as the
            # hard-deny path) + a paired AgentErrorEvent (kept visible by View),
            # then hand the model another turn so it can submit_plan / read / ask.
            # This gate runs BEFORE the per-tool meta/finish handlers (see the run
            # loop) so finish/serve/remember/notify_user/delegate_explore can't slip
            # past to their handlers; and it ALSO closes the revision re-entry leak
            # (enter_planning() puts the loop back in PLANNING).
            planning_virtuals = {"ask_user", "questions_v2", "clarify"}
            # Free planning steps that must NOT count toward the explore-read cap:
            # the ask/intake escape hatches (handled by their own halt handlers,
            # never executed) PLUS `think` — a pure NO-OP reasoning scratchpad. None
            # of these GATHER context, so none should push the planner toward the
            # forced-plan cap; `think` still falls through and EXECUTES (a harmless
            # no-op), it just isn't tallied as a read.
            planning_noncounting = planning_virtuals | {"think"}
            allowed = self._driver.planning_allowed_tool_names()
            if tc.tool_name not in allowed:
                refusal_streak = planning_tool_refusal_streak(events) + 1
                action = ActionEvent(
                    thought=step.thought,
                    tool_call=tc,
                    self_assessed_risk=step.self_assessed_risk,
                    llm_response_id=step.llm_response_id,
                )
                await self._emit(action)
                await self._emit(
                    AgentErrorEvent(
                        error=_planning_tool_refusal_message(
                            tc.tool_name,
                            streak=refusal_streak,
                            read_calls_remaining=(self._driver.force_submit_read_calls_remaining()),
                        ),
                        action_id=action.id,
                        tool_call_id=tc.call_id,
                    )
                )
                if refusal_streak >= _PLANNING_TOOL_REFUSAL_ESCALATE_AT:
                    harvested = await harvest_revision_plan_after_refusal(self)
                    if harvested is not None:
                        return harvested
                return Disp.CONTINUE
            # An allowed planning tool. ask_user/questions_v2/clarify are virtual escape hatches
            # handled by their own halt handlers downstream — do NOT count them as
            # exploration reads. Only an ACTUAL read tool (file_read/file_list/
            # search/extract) counts toward the explore cap + can force a plan at
            # the cap (logic in Planner), then falls through to the normal action
            # path. Phase-1 reads below the cap are unchanged.
            if tc.tool_name not in planning_noncounting:
                await self._planner.note_planning_read_and_maybe_force()
        return Disp.FALLTHROUGH

    async def _gate_hard_deny(self, action: ActionEvent) -> Disp:
        # (h.5) HARD DENY (Cluster 3) — catastrophic commands are refused
        # outright, BEFORE the confirm gate. No approval, policy, or LLM
        # can run them. The agent sees the refusal as an error and adapts.
        deny_reason = signals.hard_deny_reason(action)
        if deny_reason is not None:
            await self._emit(action)  # record the proposed action for audit
            await self._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"REFUSED: that command is hard-denied ({deny_reason}). It "
                        "will never be executed regardless of approval. Choose a "
                        "different, safe approach.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=action.tool_call.call_id if action.tool_call else None,
                )
            )
            return Disp.CONTINUE
        return Disp.FALLTHROUGH

    async def _gate_risk_confirm(self, action: ActionEvent) -> tuple[Disp, ActionEvent]:
        # (i) RISK GATE — assess, then maybe require confirmation (§5).
        # GATE ORDER (appkit-lane live catch 2026-07-10): an action whose tool is
        # NOT in the executor's callable set can never execute, so it must never
        # park on a human confirmation — in an autonomous run nobody can answer
        # and the run dies at the inactivity cap (a barred `file_edit` in strict
        # AppKit hit BlastRadiusConfirm instead of the executor's unknown_tool
        # refusal). Fall through so execute() emits the ONE canonical refusal.
        _callable = getattr(self.executor, "callable_tool_names", None)
        if callable(_callable):
            try:
                if action.tool_call.tool_name not in set(cast(Iterable[str], _callable())):
                    return Disp.FALLTHROUGH, action
            except Exception:  # noqa: BLE001 — introspection failure → normal gating
                pass
        # Audit (security §7): when the analyzer exposes the detailed
        # assessment, stamp it into the action's meta so the security
        # posture (final risk, rationale, contributing analyzers, the
        # self-assessment) is reconstructable from the log. Analyzers that
        # implement only assess() are unaffected.
        detailed = getattr(self.analyzer, "assess_detailed", None)
        if callable(detailed):
            # Duck-typed: analyzers implementing the richer protocol return a
            # RiskAssessment; the getattr(..., None) probe widens it to object.
            assessment = cast("RiskAssessment", detailed(action))
            risk = assessment.risk
            audited_meta = {
                **action.meta,
                "risk_assessment": assessment.model_dump(mode="json"),
            }
            action = action.model_copy(update={"meta": audited_meta})
        else:
            risk = self.analyzer.assess(action)

        # DC-03: obtain WHERE this concrete tool call executes. The call-aware hook
        # lets sandbox-backed tools declare host scope before any HTTP/key work.
        _scope_fn = getattr(self.executor, "tool_scope_for_call", None)
        _tool_scope = "unknown"
        if callable(_scope_fn):
            try:
                _tool_scope = _scope_fn(action.tool_call.tool_name, action.tool_call.arguments)
            except Exception:
                _tool_scope = "unknown"
        else:
            _static_scope_fn = getattr(self.executor, "tool_scope", None)
            if callable(_static_scope_fn):
                try:
                    _tool_scope = _static_scope_fn(action.tool_call.tool_name)
                except Exception:
                    _tool_scope = "unknown"

        # Use should_confirm_action if the policy supports it; fall back to
        # should_confirm(risk) for policies that predate DC-03.
        _sca = getattr(self.policy, "should_confirm_action", None)
        if callable(_sca):
            _gates = _sca(risk, scope=_tool_scope, tool_name=action.tool_call.tool_name)
        else:
            _gates = self.policy.should_confirm(risk)

        # Journal: stamp auto_approved when scope-based exemption overrides
        # what the base risk gate would have decided.
        if not _gates and self.policy.should_confirm(risk):
            action = action.model_copy(
                update={"meta": {**action.meta, "auto_approved": "sandboxed"}}
            )

        if _gates:
            await self._emit(action)  # record the PROPOSED action
            await self._emit(
                StatusEvent(
                    status=ConversationStatus.WAITING_FOR_CONFIRMATION,
                    detail=action.id,
                )
            )
            return Disp.HALT, action
        return Disp.FALLTHROUGH, action

    async def run(self) -> ConversationState:
        """Drive until a terminal-for-now status. Idempotent to call again after
        a pause/confirmation. [CONTRACT] returns the resulting ConversationState."""
        # Fresh run segment → fresh invisible-step accounting (the counter only
        # measures spin WITHIN a segment; a resume/steer is a clean slate).
        self._invisible_steps = 0
        self._finish_verify_refusals = 0  # fresh segment → fresh verify-cap streak
        self._finish_verify_strips = 0
        self._workflow_output_contract_refusals = 0
        # C1c — fresh segment → fresh DoD-refusal streak (telemetry; the gate
        # has no cap, but a resume/steer should not carry a streak across).
        self._dod_refusals = 0
        # REL-RC-O — fresh segment → fresh dictated-content refusal budget.
        self._dictated_content_refusals = 0
        # C20 — fresh segment → fresh fan-out budget. The cap is per-run-
        # segment so a resume/steer gets a fresh budget (a steered user
        # message is a clean slate; the prior segment's helper round-trips
        # are already visible in the log).
        self._fanout_count = 0
        # C6 — fresh segment → fresh recitation cadence. The step counter
        # restarts at 0 (so the first step after resume is on a cadence
        # boundary and re-emits the recap — the model may have lost
        # context across the pause and the goal needs to be visible).
        # The signature is reset to None so a plan that arrived mid-pause
        # OR a checklist mark the model made pre-pause is detected as
        # drift on the first post-resume step (it differs from None).
        self._recitation_step_count = 0
        self._recitation_last_signature = None
        # HS-03 — fresh segment → fresh re-ground cadence. The brief
        # asks for "ONCE immediately after a restart/resume" and a
        # per-segment cadence thereafter. Both flags reset here so a
        # resume (or a steer / first start) gets a fresh post-resume
        # one-shot AND a fresh per-boundary counter (the per-boundary
        # counter is what prevents re-emit on consecutive steps at
        # the same boundary; resetting on every segment makes the
        # cadence scoped to the current run, not the conversation).
        # Contrast with _bootstrap_emitted (F4), which persists across
        # run() segments — the F4 bootstrap is "fire once per
        # conversation" (the model already saw it), while HS-03 is
        # "fire once per resume" (a pause may have lost context, so
        # a resume is exactly when the recap matters).
        self._hs03_reground_post_resume_emitted = False
        self._hs03_reground_last_action_count = -1
        # C18 — NOTE: `_plan_step_predicates` is intentionally NOT reset
        # here. The map is per-(plan_revision, step_index) and is
        # populated by `_plan_from_args` at submit_plan / re-plan time;
        # a re-plan overwrites by revision so stale entries can't match.
        # Clearing on every run() entry would wipe the predicates
        # between planning and the first post-approve execution call,
        # silently disabling the advisory check.
        state = await self.get_state()
        if state.execution_status in _TERMINAL_FOR_NOW and state.execution_status != (
            ConversationStatus.IDLE
        ):
            # Paused/waiting/finished/stuck/error: a fresh run must be re-armed by
            # a control op (resume/confirm) or a new message. IDLE means "ready".
            if state.execution_status in (
                ConversationStatus.PAUSED,
                ConversationStatus.WAITING_FOR_CONFIRMATION,
                ConversationStatus.AWAITING_PLAN_APPROVAL,
            ):
                return state
            # AWAITING_USER_DECISION / AWAITING_USER_QUESTION: the agent
            # voluntarily paused for the user (pick-a-card or free-form question).
            # A new user message (steer / send_message) IS the resume signal —
            # treat it like FINISHED/STUCK below (re-kick if there's fresh work).
            # No special control op needed; the user just typing IS the answer.
            # FINISHED/STUCK/ERROR/AWAITING_USER_*/no new work → idle.
            if not self._has_unprocessed_user_message(await self._events()):
                return state
        # Restore/reconcile the in-memory mode from the event log. `self.mode`
        # is only a cache; phase truth is the latest planning/plan_approved
        # marker so preamble and re-kick paths cannot drift.
        self._reconcile_mode_from_events(await self._events())

        # Bug 12 (§11.4) — FINISHED→followup path. A change/revision follow-up on
        # an approved/finished build re-enters PLANNING here (the planning gate
        # keys on self.mode) so the revision goes through a revised plan rather
        # than a free write on the stale approved plan. Runs AFTER the post-restart
        # mode reconstruction above so self.mode reflects reality. Pure Q&A is
        # exempt (answered in execution mode, no forced re-plan). When it re-enters
        # it already emits RUNNING/planning, so skip the plain RUNNING emit (which
        # would shadow the durable `planning` marker).
        async with self._lock:
            _reentered_planning = await self._maybe_reenter_planning_for_followup(
                await self._events()
            )
        if not _reentered_planning:
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))

        # EXIT INVARIANT (Fix 3) — in-loop defense-in-depth atop the runtime
        # backstop (9c90d7e). run() emits RUNNING above; each of the drive
        # loop's ~15 exits relies on a PRECEDING emit having set a terminal or
        # parked status. A dropped/unparseable model turn can yield a no-event
        # step, and a HALT path can return while still RUNNING — the
        # conversation would then sit RUNNING forever until the supervisor
        # catches it. Funnel every drive-loop exit through one boundary: on a
        # NORMAL return, if the reconstructed status is still RUNNING (i.e. NOT
        # in _TERMINAL_FOR_NOW), terminalize to STUCK with the SAME detail the
        # runtime backstop uses. An exception (-> runtime ERROR) or a
        # CancelledError (deliberate kill) propagates out of _run_drive()
        # untouched — only a clean, silently-non-concluding return is repaired.
        result = await self._run_drive()
        if (await self.get_state()).execution_status not in _TERMINAL_FOR_NOW:
            _LOG.error(
                "run(%s) drive loop returned without reaching a terminal state; "
                "marking STUCK (in-loop exit invariant)",
                self.conversation_id,
            )
            await self._land_blocked(
                reason="loop ended without reaching a terminal state",
                guidance=(
                    "The drive loop returned cleanly while the conversation was "
                    "still RUNNING, so the host could not prove what should happen next."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="loop ended without reaching a terminal state",
            )
            return await self.get_state()
        return result

    async def _run_drive(self) -> ConversationState:
        """The drive loop body, extracted from run() so a single exit funnel
        (the Fix-3 exit invariant) covers ALL of its ~15 return paths. Loops
        forever, yielding only by returning the reconstructed state at a
        checkpoint (or by propagating an exception / CancelledError)."""
        while True:
            action_to_execute: ActionEvent | None = None
            async with self._lock:
                events = await self._events()
                self._reconcile_mode_from_events(events)
                state = ConversationState.reconstruct(
                    self.conversation_id, events, max_iterations=self.max_iterations
                )
                status = state.execution_status

                # WALK-18 — cooperative pause lands here at the step boundary
                # (pause() only SET the flag, without the lock). Emit PAUSED
                # ourselves and return; resume re-kicks (the flag is cleared at
                # run() entry). This is a step-boundary pause — a true
                # mid-model-step interrupt would need driver cooperation.
                if self._pause_requested.is_set():
                    self._pause_requested.clear()
                    self._retry_interrupt.clear()
                    await self._emit(StatusEvent(status=ConversationStatus.PAUSED))
                    return await self.get_state()

                # (a) honor control transitions decided between steps
                if status in (
                    ConversationStatus.PAUSED,
                    ConversationStatus.IDLE,
                    ConversationStatus.FINISHED,
                    ConversationStatus.STUCK,
                    ConversationStatus.ERROR,
                    ConversationStatus.WAITING_FOR_CONFIRMATION,
                    ConversationStatus.AWAITING_PLAN_APPROVAL,
                ):
                    return state

                # (b) iteration ceiling — the ultimate backstop (principle 4)
                if state.iteration >= self.max_iterations:
                    await self._emit(
                        ErrorEvent(
                            code="max_iterations",
                            detail=f"reached {self.max_iterations}",
                        )
                    )
                    return await self.get_state()

                # C5 — drain any recovered MEMORY facts the session has staged
                # from a post-recreate read of `.pmx/MEMORY.md` (SandboxSession
                # sets these on a mid-session box death / hard reset). The
                # in-View channel is the authoritative in-session source; this
                # is the moment the recovered facts are re-emitted as
                # KnowledgeEvents, restoring the View to match the surviving
                # on-disk mirror. Runs once per recreate (the session drains
                # itself on `take_recovered_memory_facts`).
                await self._recit.drain_recovered_memory_facts()

                events = await self._valve.gate_f4_bootstrap(events)

                # HS-03 — scheduled facts re-grounding (assist-tier only), BEFORE
                # stuck-detection so a re-ground + a stuck-escape can co-fire.
                # Re-polls events so the materialized View sees the recap this turn.
                # Full cadence/closure rationale: see _maybe_emit_reground.
                events = await self._maybe_emit_reground(events)

                disp = await self._valve.gate_stuck(events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                # [REL-RC-B] break a FRESH_READ_REQUIRED edit loop (inject one real file_read)
                # BEFORE the circuit breaker hands off / STUCKs.
                disp = await self._valve.gate_fresh_read_autoground(events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                disp = await self._valve.gate_circuit_breaker(events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                # WALK-19 — semantic no-progress breaker (failure-independent)
                disp = await self._valve.gate_no_progress(events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                disp, events = await self._valve.gate_bookkeeping_streak(events)
                if disp is Disp.HALT:
                    return await self.get_state()

                # Bug 12 (§11.4) — RUNNING→steer path. A change/revision follow-up
                # can arrive WHILE the task is already RUNNING; runtime.kick()
                # returns early when a task is active, so the run() intake above
                # never saw it. Re-enter PLANNING here too so a mutating mid-run
                # steer goes through a revised plan, not a write on the old plan.
                # Lock is held here (caller frame). Pure Q&A is exempt.
                if await self._maybe_reenter_planning_for_followup(events):
                    events = await self._events()

                disp = await self._maybe_synthesize_finish_after_actionless_pauses(state, events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                # (d) build the model-facing View, condensing if triggered (§8)
                view = await self._materialize_view(events)

                step, disp = await self._driver.drive_step(view, events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()
                # drive_step returns a None step ONLY paired with CONTINUE/HALT
                # (handled above); a fall-through disp always carries a real step.
                assert step is not None

                # (e.3) PLANNING PHASE GATE — runs BEFORE the per-tool meta/finish
                # handlers below so NOTHING outside the planning allowlist reaches
                # its handler or execution while in PLANNING. submit_plan is
                # intercepted, a no-tool prose turn is nudged, read/explore tools +
                # ask_user/questions_v2/clarify fall through to their normal paths, and EVERY
                # other tool (finish/serve/remember/notify_user/delegate_explore/
                # file_write/shell/browser/…) gets a recoverable rejection — closing
                # the bypass where a finish/serve/remember could run before plan
                # approval. In EXECUTION mode this is an immediate no-op fall-through,
                # so the meta/finish handlers behave exactly as before.
                disp = await self._gate_planning_mode(step, events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                # (e.4) TURN-TAKING NORMALIZATION (GAP B fix). Completion is now
                # AFFIRMATIVE: the agent ends a run only by calling the `finish`
                # tool, never by emitting a tool-less prose turn (which the old
                # code mis-read as "done"). Two virtual tools the loop intercepts:
                #   - `notify_user`: non-blocking progress / a mid-run reply →
                #     record an agent message and CONTINUE (the safe talk-back
                #     channel that replaces the dangerous tool-less-prose one).
                #   - `finish`: the explicit terminal move → normalize to a
                #     finished, tool-less step so the existing finish-path gate
                #     (stop-hook veto, plan-completeness, auto-continue) runs.
                if step.tool_call is not None and step.tool_call.tool_name == "notify_user":
                    if await self._meta.handle_notify_user(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if step.tool_call is not None and step.tool_call.tool_name == "remember":
                    if await self._meta.handle_remember(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if step.tool_call is not None and step.tool_call.tool_name == "serve":
                    if await self._meta.handle_serve(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if step.tool_call is not None and step.tool_call.tool_name in (
                    "skip",
                    "needs_input",
                ):
                    disp = await self._meta.handle_workflow_control(step, events)
                    if disp is Disp.CONTINUE:
                        continue
                    if disp is Disp.HALT:
                        return await self.get_state()
                if step.tool_call is not None and step.tool_call.tool_name == "delegate_explore":
                    if await self._meta.handle_delegate_explore(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if step.tool_call is not None and is_finish_tool_name(
                    step.tool_call.tool_name, self._finish_alias
                ):
                    # P6 — a contract finalizer alias (ready_for_*_verification) IS the
                    # finish signal: normalize the name to "finish" so the host-truth gate
                    # + every downstream reader behaves byte-identically regardless of the
                    # contract-specific name (the gate is name-agnostic; this is defensive).
                    requested_verification = step.requested_verification or (
                        self._finish_alias is not None
                        and step.tool_call.tool_name == self._finish_alias
                    )
                    if (
                        step.tool_call.tool_name != "finish"
                        or requested_verification != step.requested_verification
                    ):
                        step = step.model_copy(
                            update={
                                "requested_verification": requested_verification,
                                "tool_call": step.tool_call.model_copy(
                                    update={"tool_name": "finish"}
                                ),
                            }
                        )
                    step, disp = await self._finish.normalize_finish_step(step, events)
                    if disp is Disp.CONTINUE:
                        continue

                # Any productive step (planning read OR execution action) resets the
                # nudge counter so a recovered loop gets a fresh budget next time.
                # (The planning phase gate above already intercepted/rejected the
                # non-fall-through cases; a step reaching here is a real action or an
                # allowlist planning read.)
                self._plan_nudges = 0

                # (e.6) TRUNCATION (W-31). The provider cut the assistant message
                # off mid-sentence (finish_reason=="length") with no tool call —
                # worst right after a re-steer, when a long <think> preamble eats
                # the output budget. The agent already forced finished=False, so
                # this never reaches the finish path; intercept BEFORE the generic
                # no-op handler to inject a "continue where you left off" reminder
                # (vs silently recording a fragment) and re-step.
                if step.truncated and step.tool_call is None:
                    disp = await self._meta.handle_truncated_step(step, events)
                    if disp is Disp.HALT:
                        return await self.get_state()
                    if disp is Disp.CONTINUE:
                        continue

                # (f) finish path — subject to stop-hook veto (§7.4)
                if step.finished and step.tool_call is None:
                    disp = await self._finish.handle_finish_path(step, state, events)
                    if disp is Disp.CONTINUE:
                        continue
                    if disp is Disp.HALT:
                        return await self.get_state()

                # (g) no-op step (thought only) — record and continue. GAP B
                # backstop: a tool-less prose turn emits a MessageEvent (not an
                # ActionEvent), so `iteration`/max_iterations never advances on
                # it. Without a guard, a model that keeps talking without acting
                # would loop forever. Count consecutive no-ops since the last
                # real action / user message; nudge, then hard-stop.
                if step.tool_call is None:
                    if await self._meta.handle_noop_step(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue

                # (g.5) Model-chosen escape hatches: ask_user / questions_v2 /
                # clarify (halt for human input), propose_plan_update
                # (re-plan + re-approval). Each is
                # intercepted by its handler below; the loop never nudges the model
                # toward them. First, the fresh-session backstop: a hallucinated
                # ask/intake/propose before any real action this session is refused
                # with actionable feedback (see _gate_ask_fresh_session).
                disp = await self._meta.gate_ask_fresh_session(step, events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                # AUTONOMOUS HEADLESS-STALL GUARD. _tools_for_step withholds
                # ask_user/questions_v2/clarify from the schema in autonomous mode, but a weak
                # model can still hallucinate the NAME (prefill, imitation of
                # training data). The first-action guard above only fires before any
                # real work; once work has happened a hallucinated ask/intake
                # would fall through to the handlers below and halt at
                # AWAITING_USER_QUESTION — a silent headless stall, since there is no
                # user to answer. Convert it into a self-directed nudge: record the
                # proposed call (so the assistant tool_call stays PAIRED with a tool
                # result — KV stability, same discipline as the hard-deny path),
                # then feed back a system-reminder that there's no user and it must
                # decide and continue. Gated on self._autonomous (default OFF) so the
                # interactive path is byte-for-byte untouched.
                disp = await self._meta.gate_autonomous_ask_stall(step, events)
                if disp is Disp.CONTINUE:
                    continue

                if step.tool_call is not None and step.tool_call.tool_name == "propose_plan_update":
                    disp = await self._meta.handle_propose_plan_update(step, events)
                    if disp is Disp.CONTINUE:
                        continue
                    if disp is Disp.HALT:
                        return await self.get_state()

                if step.tool_call is not None and step.tool_call.tool_name == "questions_v2":
                    if await self._meta.handle_questions_v2(step, events) is Disp.HALT:
                        return await self.get_state()

                if step.tool_call is not None and step.tool_call.tool_name == "clarify":
                    if await self._meta.handle_clarify(step, events) is Disp.HALT:
                        return await self.get_state()

                if step.tool_call is not None and step.tool_call.tool_name == "ask_user":
                    if await self._meta.handle_ask_user(step, events) is Disp.HALT:
                        return await self.get_state()

                # Bug 12 (§11.4) — close the in-flight steer write-through race: a
                # change steer that landed DURING this drive_step (after the
                # top-of-loop re-plan check) must not get one mutating call through
                # on the OLD plan. Re-poll + re-enter PLANNING + defer the write.
                disp = await self._gate_midstep_steer_replan(step)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                # (h) build the ActionEvent
                # A real action is being taken — the invisible-step streak is over.
                self._invisible_steps = 0
                action = ActionEvent(
                    thought=step.thought,
                    tool_call=step.tool_call,
                    self_assessed_risk=step.self_assessed_risk,
                    llm_response_id=step.llm_response_id,
                )

                disp = await self._gate_hard_deny(action)
                if disp is Disp.CONTINUE:
                    continue

                disp, action = await self._gate_risk_confirm(action)
                if disp is Disp.HALT:
                    return await self.get_state()

                action_to_execute = action

            # (j) EXECUTE outside the lock (long-running; lock only guards state).
            # Bug 12 (§11.4) POST-LOCK recheck — a revision steer can flip mode to
            # PLANNING via `send_message` AFTER the in-lock gates passed but BEFORE we
            # execute here (the lock is dropped for the long-running execute). Re-acquire
            # the lock + re-check: a MUTATING action selected on the now-stale plan must
            # NOT run. Closes the ingest→execute window the top-of-loop + mid-step gates
            # miss. A planning-allowed read/think/explore proceeds (harmless).
            async with self._lock:
                steer_refused = (
                    self.mode == OperatingMode.PLANNING
                    and action_to_execute.tool_call is not None
                    and action_to_execute.tool_call.tool_name
                    not in self._driver.planning_allowed_tool_names()
                )
            await self._emit(action_to_execute)
            if steer_refused and action_to_execute.tool_call is not None:
                # Paired refusal (mirrors _gate_midstep_steer_replan): action_id +
                # tool_call_id keep the assistant tool_call PAIRED with a tool-role
                # result the View keeps, so the refusal is recoverable, not orphaned.
                await self._emit(
                    AgentErrorEvent(
                        error=_MIDSTEP_STEER_REFUSAL.format(
                            tool=action_to_execute.tool_call.tool_name
                        ),
                        action_id=action_to_execute.id,
                        tool_call_id=action_to_execute.tool_call.call_id,
                    )
                )
                continue
            await self._execute_and_observe(action_to_execute)
            if await self._maybe_apply_read_churn_valve(action_to_execute) is Disp.HALT:
                return await self.get_state()
            # loop continues

    # _has_unprocessed_user_message delegates to signals (external callers + run()).
    _has_unprocessed_user_message = staticmethod(signals.has_unprocessed_user_message)

    # ---- control operations (§7) — map from the wire frames -----------------

    async def send_message(self, text: str, *, steer: bool = False) -> ConversationState:
        """Append a USER message. Never dropped; picked up at the next iteration
        (the next View includes it). If the conversation had FINISHED/STUCK, it
        reopens to IDLE (§2). `steer` differs only in UI intent (BoD §13.4)."""
        self._retry_interrupt.set()
        try:
            async with self._lock:
                state = await self.get_state()
                await self._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=text),
                        meta={"steer": True} if steer else {},
                    )
                )
                self._reconcile_mode_from_events(await self._events())
                if state.execution_status in (
                    ConversationStatus.FINISHED,
                    ConversationStatus.STUCK,
                ):
                    await self._emit(StatusEvent(status=ConversationStatus.IDLE))
                # DURABLE NO_REPLAN fix — deterministic re-plan at steer INGEST. A
                # scope-adding/revision steer on an APPROVED plan must re-enter PLANNING the
                # MOMENT it arrives, so the next write is gated by `_gate_planning_mode` until
                # a revised plan is approved (Manus-UX: a mid-run scope change surfaces a
                # VISIBLE re-plan boundary, not a silent build on the stale plan). The two
                # polling guards (`_maybe_reenter_planning_for_followup` + the mid-step gate)
                # are POLLING-based and RACE with an in-flight turn whose response buries the
                # unprocessed-user marker; doing it at ingest is race-free. SKIP while a
                # confirmation / plan-approval is pending (those control-pending states are
                # owned by confirm/reject/approve). Q&A is exempt (`is_revision_intent`).
                if (
                    steer
                    and self.mode != OperatingMode.PLANNING
                    and state.execution_status
                    not in (
                        ConversationStatus.WAITING_FOR_CONFIRMATION,
                        ConversationStatus.AWAITING_PLAN_APPROVAL,
                    )
                    and signals.is_revision_intent(text)
                ):
                    evs = await self._events()
                    if not signals.current_blocked_question_landing(evs) and any(
                        isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in evs
                    ):
                        await self._enter_revision_planning(text)
            return await self.get_state()
        finally:
            self._retry_interrupt.clear()

    async def steer(self, text: str) -> ConversationState:
        return await self.send_message(text, steer=True)

    async def confirm(self) -> ConversationState:
        """Phase 2 of the confirmation gate: execute EXACTLY the pending action
        (no re-ask to the model), then resume (§5)."""
        async with self._lock:
            state = await self.get_state()
            if (
                state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION
                or state.pending_action_id is None
            ):
                return state
            pending = await self._event_by_id(state.pending_action_id)
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        if isinstance(pending, ActionEvent):
            await self._execute_and_observe(pending)  # outside the lock
        return await self.get_state()

    async def reject(self, reason: str = "rejected by user") -> ConversationState:
        """Deny the pending action: record the denial (so the model sees it next
        View) and resume to RUNNING without executing (§5).

        The rejection is recorded as an `AgentErrorEvent` whose content is wrapped
        in an implicit `<system-reminder>` — the action did not produce an error,
        the human declined it. Paired with the proposed action's call_id so the
        provider adapter sees a properly-correlated tool result."""
        async with self._lock:
            state = await self.get_state()
            if state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION:
                return state
            pending = (
                await self._event_by_id(state.pending_action_id)
                if state.pending_action_id
                else None
            )
            call_id: str | None = None
            if isinstance(pending, ActionEvent) and pending.tool_call:
                call_id = pending.tool_call.call_id
            reminder = (
                "<system-reminder>\n"
                f"The user reviewed the proposed action and did not approve it "
                f"({reason}). The action was NOT executed. Pick a different "
                "approach.\n"
                "</system-reminder>"
            )
            await self._emit(
                AgentErrorEvent(
                    error=reminder,
                    action_id=state.pending_action_id,
                    tool_call_id=call_id,
                )
            )
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        return await self.get_state()

    async def _arm_dod_from_plan(self) -> None:
        """C1c WIRING — on plan approval, arm the (previously dark) Definition-of-Done
        finish gate from the plan's OWN committed, machine-checkable done_conditions,
        so `finish` is blocked until the files, commands, and HTTP checks the model
        declared are satisfied. This closes "declare a step then skip it" by
        construction (the strongest anti-gaming the agentic-builder literature found that
        needs no human — Devin/Kiro/Terminal-Bench all anchor 'done' to externally-checkable
        state, never to the model's free-text self-assessment).

        Safety contract:
          * Pre-approval validation rejects cross-step file/directory contradictions and
            loopback HTTP preview guesses. Command/http_ok have an explicit infra-vs-task
            channel; unverifiable evidence remains visible and fail-closed.
          * Empty-guard: no accepted predicate ⇒ DO NOT set a spec. An empty spec makes the
            evaluator return passed=False (a deterministic block loop), so a plan that declared
            no checkable deliverable must leave the gate dark, not armed-and-failing.
          * Write-once (by store design): the FIRST approved plan captures the DoD; a later
            revision's re-arm raises DoDSpecAlreadySet and is swallowed. Extending the DoD with
            steer-added scope is a separate MONOTONIC slice (may only ADD predicates, never
            weaken) — not done here, so we never let a revision relax its own acceptance gate.
        Only the build loop reaches this (PLANNING + submit_plan); research/chat loops never
        approve a plan, so the gate stays inert for them."""
        # Read the predicates off the PERSISTED PlanEvent (resume-durable), NOT the
        # in-memory `_plan_step_predicates` map: a restart between submit_plan and approval
        # loses the map, but the PlanEvent — with `done_condition` now persisted on each
        # step — survives, so the gate still arms after a crash (codex backstop P1).
        from ..view import _latest_plan

        events = await self._events()
        plan = _latest_plan(events)
        if plan is None or not getattr(plan, "steps", None):
            return
        # v1 armed file_exists only; v2.1 includes command + http_ok now that the evaluator
        # has an infra-vs-task channel. A denied command / unprobeable server is visibly
        # UNVERIFIABLE and remains fail-closed rather than weakening the acceptance bar;
        # command/http_ok add real build/test + runtime-serve verification.
        _GATEABLE_KINDS = ("file_exists", "command", "http_ok")
        # Narrow via a local (pyright cannot narrow through getattr) so the list is
        # typed list[DoDPredicate], not list[DoDPredicate | None]. (pre-existing
        # reportAssignmentType, fixed while touching this file for CXT-6.)
        gateable_preds: list[DoDPredicate] = []
        for s in plan.steps:
            dc = getattr(s, "done_condition", None)
            if dc is not None and getattr(dc, "kind", None) in _GATEABLE_KINDS:
                gateable_preds.append(dc)
        if not gateable_preds:
            return
        try:
            # replace_dod_spec = write-once BOOTSTRAP on the first arm + MONOTONIC
            # extension on a revision: a steer that ADDS scope (e.g. "also add a
            # Contact page") tightens the bar to require contact.html; a revision that
            # would DROP a committed deliverable (without an explicit rename) is
            # rejected store-side and the existing, stronger bar holds.
            await self.store.replace_dod_spec(
                self.conversation_id,
                DoDSpec(predicates=gateable_preds),
                actor="system:plan_approval",
            )
        except DoDSpecAlreadySet:
            # The revised plan would WEAKEN the DoD (a committed deliverable was
            # dropped without an explicit rename). Keep the stronger existing bar —
            # the model cannot relax its own acceptance criteria mid-build.
            _LOG.warning(
                "DoD revision for %s rejected as a weakening; existing acceptance bar preserved.",
                self.conversation_id,
            )

    async def approve_plan(self) -> ConversationState:
        """Approve the pending plan: flip into execution mode (full tools restored)
        and resume to RUNNING. The caller then re-runs the loop. The per-action
        risk gate still governs the build that follows (defense in depth)."""
        async with self._lock:
            state = await self.get_state()
            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
                return state
            self.mode = self._execution_mode
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))
            await self._arm_dod_from_plan()
            await self._seed_context_from_plan()
        return await self.get_state()

    async def _seed_context_from_plan(self) -> None:
        """CXT-6/CXT-7 — seed durable context from the approved plan: current_goal.md
        (= plan.summary) and todo.md (the step checklist), the live execution memory.
        Best-effort: a seeding error never blocks approval. A revised-plan re-approval
        re-seeds (so the durable context tracks the current contract); minor in-build
        updates are the agent's via the context_memory tool."""
        sbx = getattr(self.executor, "sandbox", None)
        if sbx is None:
            return
        try:
            from ..context import ArtifactMemoryStore
            from ..design import (
                direction_tokens_css,
                pick_direction,
                render_design_direction,
            )
            from ..view import _latest_plan
            from .context_builder import render_plan_as_todo_markdown

            events = await self._events()
            plan = _latest_plan(events)
            if plan is None or not getattr(plan, "steps", None):
                return
            store = ArtifactMemoryStore(sbx)
            if plan.summary:
                await store.write_goal(plan.summary)
            await store.seed_todo(render_plan_as_todo_markdown(plan))
            brief = _design_direction_brief_text(plan, events)
            if brief:
                direction = pick_direction(brief, self.conversation_id)
                await store.write_design_direction(render_design_direction(direction))
                # Mechanical bridge: also emit a ready-to-use tokens.css so
                # conformance is copy-paste (never blocks approval — same try/except).
                await store.write_design_direction_tokens(direction_tokens_css(direction))
        except Exception:
            _LOG.warning(
                "CXT-7 context seed from plan failed for %s", self.conversation_id, exc_info=True
            )

    async def pick_alternative(self, option_id: str) -> ConversationState:
        """Resume from AWAITING_USER_DECISION by selecting one of the agent's
        proposed alternatives. Pulls the option's ToolCall, synthesizes an
        ActionEvent under EventSource.AGENT (the loop "speaks for" the agent
        here — the option WAS the agent's own proposal, the user is just
        choosing which one), and transitions to RUNNING. The loop's next
        iteration sees the dangling ActionEvent and executes it via the normal
        _execute_and_observe path (including the risk gate).

        Idempotent: a pick on a non-pending state is a no-op."""
        async with self._lock:
            state = await self.get_state()
            if state.execution_status != ConversationStatus.AWAITING_USER_DECISION:
                return state
            # Find the latest AlternativesEvent + the picked option.
            events = await self._events()
            alt: AlternativesEvent | None = None
            for e in reversed(events):
                if isinstance(e, AlternativesEvent):
                    alt = e
                    break
            if alt is None:
                # No alternatives event but status said pending — log a
                # reminder + clear to RUNNING so the user isn't trapped.
                await self._emit(
                    StatusEvent(status=ConversationStatus.RUNNING, detail="alternatives_missing")
                )
                return await self.get_state()
            # D2 BYPASS — "Continue anyway": the user lets the agent keep going with
            # its own judgment. RESET the failure streak (a USER message resets
            # _count_recent_failures) and resume RUNNING; the agent picks its own next
            # step instead of one of the proposed alternatives. This is the escape
            # hatch the crude breaker lacked.
            if option_id == _CONTINUE_OPTION_ID:
                await self._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "Continue — keep working. I've reviewed the failures and "
                                "want you to proceed with your own best next step. Don't just "
                                "repeat the exact action that was failing; adjust your approach."
                            ),
                        ),
                    )
                )
                await self._emit(
                    StatusEvent(status=ConversationStatus.RUNNING, detail="continue_anyway")
                )
                return await self.get_state()
            option = next((o for o in alt.options if o.id == option_id), None)
            if option is None:
                # Bad pick (stale id?) — record + return without resuming so
                # the UI can re-prompt with the same gate.
                await self._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"User picked an unknown alternative id ({option_id!r}). "
                                "Re-emit the gate or ask for direction.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                return await self.get_state()
            # BW-03 — a LABEL-ONLY pick is the user's ANSWER, not a tool to run.
            # An option with no tool_name carries no executable action (the model
            # enumerated a plain-language path like "Use approach A"). Picking it
            # must be treated as the user's reply to the agent's ask_user
            # question: inject the option's label as the user's text so the model
            # continues WITH that answer. Synthesizing an ActionEvent here would
            # dispatch a tool call with an empty tool_name, which errors / does
            # nothing — so this no-tool case resumes via the user-reply path
            # (mirroring the _CONTINUE_OPTION_ID resume above) instead.
            emitted: Event | None = None
            if not option.tool_name.strip():
                reply = option.title.strip()
                if option.description.strip():
                    reply = (
                        f"{reply}. {option.description.strip()}"
                        if reply
                        else option.description.strip()
                    )
                await self._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=reply),
                    )
                )
                await self._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail=f"alternative_picked:{option.id}",
                    )
                )
            else:
                # Record the human's pick as a user message — keeps the log honest
                # (the audit trail shows "user chose X") AND lands in the LLM View
                # so a follow-up turn has the context.
                await self._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(
                            role="user",
                            content=(
                                f"Try the alternative approach: “{option.title}”. "
                                f"{option.description}"
                            ),
                        ),
                    )
                )
                # Synthesize the ActionEvent. The thought records WHY we're
                # running it (the option's description) so the trace stays
                # self-explanatory.
                action = ActionEvent(
                    source=EventSource.AGENT,
                    thought=f"User picked alternative: {option.title}. {option.description}",
                    tool_call=ToolCall(
                        tool_name=option.tool_name,
                        arguments=option.arguments,
                    ),
                )
                # SECURITY — a picked RUNNABLE alternative is gated EXACTLY like a
                # direct tool call of the same tool, in the SAME order the run-loop
                # call site uses (engine.py ~:1341): hard-deny FIRST, then
                # risk-confirm. The user's PICK selects an action; it is NOT a
                # licence to bypass either gate.
                #
                # (1) HARD DENY — a catastrophic command (e.g. `rm -rf /`) offered
                # as an option must NEVER run, regardless of the pick. Mirror the
                # run loop's `Disp.CONTINUE` branch: _gate_hard_deny has already
                # emitted the PROPOSED action + the refusal (AgentErrorEvent); we
                # must NOT execute. The run loop `continue`s so the agent sees the
                # refusal and adapts — here we do the same by resuming RUNNING and
                # re-running the loop (emitted stays None → run() after the lock;
                # run() itself takes self._lock, so the resume cannot happen while
                # we still hold it).
                disp = await self._gate_hard_deny(action)
                if disp is not Disp.CONTINUE:
                    # (2) RISK CONFIRM — a confirm-required tool (HIGH-risk /
                    # BlastRadiusConfirm publish-guard / unknown-scope) must still
                    # hit the risk-confirm gate. Mirror the run-loop call site
                    # (engine.py ~:1345) under the same lock BEFORE executing.
                    #   - HALT  → the gate has already emitted the PROPOSED action
                    #             and the WAITING_FOR_CONFIRMATION status
                    #             (pending_action_id now points at it). Stop here
                    #             without executing or transitioning to RUNNING;
                    #             confirm()/reject() drive it from the gate,
                    #             identical to a direct call.
                    #   - FALLTHROUGH → not gated: emit the action + RUNNING and
                    #             execute below (preserving the existing pick UX
                    #             for non-risky tools). The scope-aware analysis
                    #             uses the SAME injected analyzer/policy the normal
                    #             path uses — no tool list is hardcoded in core.
                    disp, action = await self._gate_risk_confirm(action)
                    if disp is Disp.HALT:
                        return await self.get_state()
                    emitted = await self._emit(action)
                # Resume RUNNING for both survivors: a hard-denied pick (emitted
                # stays None → the loop re-runs below so the agent adapts to the
                # refusal) AND a non-risky/fell-through risky pick (emitted set →
                # executed below). A risk-confirm HALT already returned above.
                await self._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail=f"alternative_picked:{option.id}",
                    )
                )
        # Label-only pick: no tool to run — the injected user reply already
        # resumed RUNNING, so just continue the loop with that answer in view.
        if emitted is None:
            return await self.run()
        # Execute the synthesized action directly so the option actually runs
        # before returning to the main loop (analogous to confirm()'s
        # post-gate execute). The loop's next call to run() then proceeds
        # with the freshly-emitted observation in view.
        # `emitted` is the persisted copy of the ActionEvent above (store.append
        # returns the same event type with `seq` filled); _emit's return is typed
        # as the broad Event union.
        await self._execute_and_observe(cast("ActionEvent", emitted))
        return await self.run()

    async def enter_planning(self, text: str = "") -> ConversationState:
        """(Re-)enter PLANNING mode — the entry point for the first plan AND for
        re-planning after a build, so focused, diff-style changes are articulated
        and re-approved rather than free-form steered. `text` is the user's
        instruction for what to (re)plan. Reopens from FINISHED/STUCK."""
        async with self._lock:
            if text.strip():
                await self._emit(
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=text),
                    )
                )
            self.mode = OperatingMode.PLANNING
            self._plan_explore_reads = 0  # (B2/B6) fresh planning segment
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
            # (B2/B6) On a revision (prior plan_approved) with a concrete new
            # instruction, frame the turn so the model RE-plans instead of
            # free-building against the OLD plan (logic in Planner).
            await self._planner.emit_replan_framing_if_revision(text)
        return await self.get_state()

    async def _maybe_reenter_planning_for_followup(self, events: list[Event]) -> bool:
        """Bug 12 (§11.4) — when a CHANGE/REVISION follow-up arrives on an
        approved/finished build (FINISHED→followup OR a mid-run RUNNING→steer),
        re-enter PLANNING so the next model turn runs with planning tools only and
        a write is REJECTED by `_gate_planning_mode` until a revised plan is
        submitted + approved. Without this the loop stays in execution mode and the
        model free-builds against the STALE approved plan (NO_REPLAN_AFTER_REVISION).

        Supplies the same `planning` marker `enter_planning()`/`request_plan()` emit
        (so `signals.in_planning_for_revision` + the actionless valve + the
        post-restart mode reconstruction all engage); the existing revision
        machinery (`Planner.plan_from_args` → revision = prev+1) does the rest.

        Lock-free (caller holds `self._lock`). Does NOT re-append the user message —
        it is already in the log. Returns True iff it re-entered planning.

        Conservative: fires ONLY for a plan-gated conversation already in execution
        mode, with a fresh unprocessed user turn whose intent is a CHANGE
        (`signals.is_revision_intent`). A pure Q&A follow-up ("what font did you
        use?") is exempt — it is answered without a forced re-plan."""
        self._reconcile_mode_from_events(events)
        # Already (re)planning → nothing to do (also guards against re-firing on
        # the same follow-up once we've emitted the planning marker below).
        if self.mode == OperatingMode.PLANNING:
            return False
        # Plan-gated only: a plan must have been approved at some point. Non-build
        # surfaces (Research) never emit plan_approved, so this never fires there.
        if not any(isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in events):
            return False
        # A reply to the shared blocked lander is the answer to the agent's
        # pending question, not a host-side revision steer. Deliver it in the
        # next model context and let the model call propose_plan_update if it
        # decides the answer changes scope.
        if signals.latest_user_answers_blocked_question(events):
            return False
        # PRIMARY (live WS/kernel steer): a durable, sequence-stable
        # `revision_steer_pending` marker the kernel ingress appended. Consumed
        # UNCONDITIONALLY here, BEFORE the unprocessed-text predicate — an in-flight
        # ActionEvent/ObservationEvent (the seq N+k write) invalidates
        # `has_unprocessed_user_message`, so the text path below MISSES the steer (the
        # live NO_REPLAN race). The sequence-stable marker cannot be masked that way.
        if signals.pending_revision_steer(events):
            await self._enter_revision_planning(signals.latest_user_text(events) or "")
            return True
        # FALLBACK (non-kernel / direct send_message ingress): text-based detection.
        # First use the usual unprocessed-user predicate. If pickup activity after a
        # terminal/idle status has already masked that predicate, fall back to the
        # terminal-idle scoped signal so INACTIVE_TIMEOUT/IDLE follows the same
        # revised-plan gate as FINISHED/STUCK/ERROR.
        text = signals.latest_unprocessed_user_text(events)
        if text is None:
            text = signals.latest_terminal_idle_followup_user_text(events)
        if text is None:
            return False
        if not signals.is_revision_intent(text):
            return False  # pure Q&A — answerable without a forced re-plan
        await self._enter_revision_planning(text)
        return True

    async def _enter_revision_planning(self, text: str) -> None:
        """Shared re-plan transition (lock-free; caller holds self._lock): flip to
        PLANNING + reset the planning segment + emit the `planning` marker + revision
        framing. ONE transition shared by the steer-INGEST path (send_message), the
        top-of-loop follow-up check, and the mid-step gate — so they cannot diverge."""
        self.mode = OperatingMode.PLANNING
        self._plan_explore_reads = 0  # (B2/B6) fresh planning segment
        await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
        await self._planner.emit_replan_framing_if_revision(text)

    async def _gate_midstep_steer_replan(self, step: AgentStep) -> Disp:
        """Bug 12 (§11.4) — close the IN-FLIGHT steer write-through race. The
        top-of-loop re-plan check (`_run_drive`) runs BEFORE `drive_step()`; a
        change/revision steer that lands WHILE the model is mid-turn is therefore
        missed by it, and the in-flight step may be a WRITE against the OLD plan.

        This apply-time gate runs AFTER `drive_step()` returns, just before the
        ActionEvent is built/executed. It RE-POLLS the log for a fresh unprocessed
        CHANGE follow-up since the last approval — for ANY tool, read/think/explore
        INCLUDED. The re-poll-on-reads matters: if it only fired for mutating
        tools, a READ in flight when the steer lands would proceed and emit its
        ActionEvent AFTER the steer, burying the steer's unprocessed marker — so
        neither the next top-of-loop check NOR a later apply-gate would see it, and
        a subsequent write would slip through on the stale plan (codex-found
        residual window).

        On detecting a pending change steer it RE-ENTERS PLANNING immediately
        (sets `mode=PLANNING` + the `planning` marker + replan framing) regardless
        of the current tool. THEN, for the CURRENT tool:
          * MUTATING (anything the planning gate would reject) → REJECT it
            recoverably (record the ActionEvent so the assistant tool_call stays
            PAIRED with a tool-role result, then a paired AgentErrorEvent the View
            keeps) — no write lands on the stale plan.
          * an ALLOWED read/think/explore tool → let it PROCEED (harmless); the
            invariant is preserved because PLANNING is now set, so EVERY subsequent
            tool this run — including the model's next write — is rejected by
            `_gate_planning_mode` until a revised plan is approved.

        A pure Q&A follow-up is exempt (`signals.is_revision_intent`). Lock-free
        (caller holds `self._lock`). Returns CONTINUE when it deferred a mutating
        call, else FALLTHROUGH (a harmless read OR the common no-steer case — zero
        behavior change for a normal execution turn)."""
        if self.mode == OperatingMode.PLANNING:
            return Disp.FALLTHROUGH  # the planning gate already governs writes
        tc = step.tool_call
        if tc is None:
            return Disp.FALLTHROUGH
        # Re-poll for a steer that may have landed DURING the just-finished
        # drive_step — for ANY tool, so a read-in-flight re-enters planning BEFORE
        # its ActionEvent buries the steer marker (no read-then-write window).
        fresh = await self._events()
        if not await self._maybe_reenter_planning_for_followup(fresh):
            return Disp.FALLTHROUGH  # no pending change follow-up (or Q&A) — proceed
        # PLANNING is now re-entered. A read/think/explore tool (anything the
        # planning gate allows) may proceed harmlessly — _gate_planning_mode now
        # governs every subsequent tool. A MUTATING tool is deferred here so it
        # never lands on the stale plan; the model must submit a revised plan.
        if tc.tool_name in self._driver.planning_allowed_tool_names():
            return Disp.FALLTHROUGH
        action = ActionEvent(
            thought=step.thought,
            tool_call=tc,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        await self._emit(action)
        await self._emit(
            AgentErrorEvent(
                error=_MIDSTEP_STEER_REFUSAL.format(tool=tc.tool_name),
                action_id=action.id,
                tool_call_id=tc.call_id,
            )
        )
        return Disp.CONTINUE

    async def pause(self) -> ConversationState:
        """WALK-18 — cooperative pause. Deliberately does NOT take self._lock
        (unlike cancel): the running turn holds the lock across its model step,
        so taking it here would block until the step finishes — exactly the
        "pause does nothing until I steer" bug. Instead set a flag the run()
        loop observes at its next step boundary, where it emits PAUSED."""
        self._pause_requested.set()
        self._retry_interrupt.set()
        return await self.get_state()

    async def resume(self) -> ConversationState:
        self._pause_requested.clear()  # WALK-18 — resume cancels a pending pause
        self._retry_interrupt.clear()
        async with self._lock:
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        return await self.run()

    async def cancel(self) -> ConversationState:
        """Cooperative stop (distinct from the network-level kill switch, §7.3).
        Emits a terminal IDLE; the loop returns at its next checkpoint."""
        self._retry_interrupt.set()
        try:
            async with self._lock:
                await self._emit(StatusEvent(status=ConversationStatus.IDLE, detail="cancelled"))
            return await self.get_state()
        finally:
            self._retry_interrupt.clear()
