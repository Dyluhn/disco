# ruff: noqa: F401
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
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ..security import RiskAssessment
    from .boundaries import StreamHook

from ..dod import DoDPredicate, predicate_fingerprints
from ..dod_evaluator import DoDEvaluator
from ..effects import EffectCapability, ToolBehavior
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
    PlanVerificationTransition,
    StatusEvent,
    ToolCall,
    ToolResult,
    VerifierVerdictEvent,
    WorkspaceMutationEvent,
    agent_view_consistent_events,
    current_workspace_agent_view_id,
    event_matches_current_workspace_view,
    latest_workspace_run_intent,
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
    SealabilityProbe,
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
from .plan_revisions import (
    PlanRevisionWeakeningError,
    assert_plan_revision_approvable,
    preflight_plan_revision,
    reject_plan_weakening,
    route_plan_approval_gate,
)
from .planning_harvest import harvest_revision_plan_after_refusal
from .plans import (
    Planner,
    validate_plan_conditions,
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


def _virtual_behavior(
    *capabilities: EffectCapability,
    planner_safe: bool = False,
) -> ToolBehavior:
    return ToolBehavior(
        planner_safe=planner_safe,
        possible_capabilities=frozenset(capabilities),
    )


_FINISH_BEHAVIOR = _virtual_behavior(
    EffectCapability.RUN_FINALIZE,
    EffectCapability.ARTIFACT_VERIFY,
    EffectCapability.OPAQUE_EXECUTE,
)
_FINALIZE_ONLY_BEHAVIOR = _virtual_behavior(EffectCapability.RUN_FINALIZE)
_REMEMBER_BEHAVIOR = _virtual_behavior(
    EffectCapability.RUN_CONTROL,
    EffectCapability.WORKSPACE_MUTATE,
)
_DELEGATE_BEHAVIOR = _virtual_behavior(
    EffectCapability.WORKSPACE_CONTENT_READ,
    EffectCapability.WORKSPACE_INVENTORY_READ,
    EffectCapability.EXTERNAL_OBSERVE,
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

def _default_veto_feedback() -> str:
    """The injectable DEFAULT text for a stop-hook veto — not the emit seam.

    Constraint 4: the host may replace this wholesale, so it cannot carry run
    state; the surface that fires more than once is `finish/finalize.py`'s veto
    emission and that is where the repetition-aware rendering lives.
    """
    return (
        "<system-reminder>\n"
        "The goal does not appear complete yet. Continue working toward it — a stop "
        "hook refused the FINISHED transition.\n"
        "</system-reminder>"
    )


_DEFAULT_VETO_FEEDBACK = _default_veto_feedback()

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
_PLAN_NUDGE_DIAGNOSTIC = "plan_nudge"
_WORKFLOW_ROUTER_NUDGE_DIAGNOSTIC = "workflow_router_plan_nudge"


# 2026-08-07j — the count `_plan_nudge` receives is now whole-RUN rather than
# per-segment, so its wording says "run". It previously read "of this segment",
# which was accurate for a counter that reset at every re-entry into planning —
# and that reset is precisely what let this surface render the identical body
# twice in one run (`p4_ff_node_restart@99603`, seqs 11/86). A segment-scoped
# count cannot satisfy a constraint scoped to the run. The caller
# (`planning_gates._nudge_planner`) owns the fix; this renderer only had to
# stop describing a scope it no longer receives.
#
# Written as a comment rather than in the docstring deliberately: the budget
# gate counts multiline-string content as logical source and this module sits
# at 697 of its 700-line cap, so design narration lives in `#` blocks here —
# which is already this file's dominant idiom (see the C6 and HS-03 blocks).
def _plan_nudge(repeats: int = 1) -> str:
    """The planning-mode nudge, REPETITION-AWARE (constraint 4).

    `repeats` is read from the durable log by the caller. The FIRST firing is
    byte-identical to the historical constant on purpose: event logs written
    before 2026-08-07b carry that exact content and the nudge counters still
    recognise those events by it.
    """
    prefix = (
        ""
        if repeats <= 1
        else (
            f"This is planning nudge {repeats} in this run — the previous "
            f"{repeats - 1} did not produce a plan, and a prose-only reply "
            "cannot advance the run.\n"
        )
    )
    return (
        "<system-reminder>\n"
        f"{prefix}"
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


def _workflow_router_plan_nudge(repeats: int = 1) -> str:
    """The workflow-router phase nudge, repetition-aware (constraint 4).

    Carries its OWN diagnostic: the planning-nudge counters have never counted
    the router nudge, and distinct labels preserve that exactly.
    """
    prefix = (
        ""
        if repeats <= 1
        else (
            f"This is router nudge {repeats} of this phase — the previous "
            f"{repeats - 1} did not produce one of the required tool calls.\n"
        )
    )
    return (
        "<system-reminder>\n"
        f"{prefix}"
        "Still in WORKFLOW ROUTER phase. Your next response must be exactly one tool "
        "call: `enter_workflow`, `needs_input`, or `draft_workflow`. If you are "
        "already inside a workflow run and the selected workflow cannot satisfy the "
        "goal, call `workflow_abort`.\n"
        "</system-reminder>"
    )


# The FIRST firing's text, kept as a module value because durable logs written
# before 2026-08-07b carry it verbatim and the nudge counters recognise those
# events by content. Computed from the renderer above so there is one owner.
_PLAN_NUDGE = _plan_nudge(1)
_WORKFLOW_ROUTER_PLAN_NUDGE = _workflow_router_plan_nudge(1)

# Emitted when a REVISION re-plan has been narrated >= _REVISION_FORCE_SUBMIT_K times
# without a submit_plan call (the soft _PLAN_NUDGE was ignored). This is paired with a
# durable StatusEvent(detail="force_submit_plan") marker so the NEXT planning step's tool
# set is narrowed to submit_plan ONLY (driver.tools_for_step reads the marker) — the model
# must submit the plan it has already described instead of looping in prose. The marker is
# in the event log, so a RESUMED build replays it and stays forced (no soft-nudge loop).
_FORCE_SUBMIT_DIAGNOSTIC = "force_submit_plan_directive"


def _force_submit_directive(repeats: int = 1) -> str:
    """The forced-submit directive, repetition-aware (constraint 4).

    Reachable from two seams, so one run can see it more than once.
    """
    prefix = (
        ""
        if repeats <= 1
        else (
            f"You have now been forced to submit a plan {repeats} times in this "
            "run and each previous attempt still arrived with no steps.\n"
        )
    )
    return (
        "<system-reminder>\n"
        f"{prefix}"
        "Call the `submit_plan` tool NOW with a NON-EMPTY `steps` array — the plan you last "
        "submitted had no steps, which cannot be executed. Give the concrete change(s) as "
        'ordered steps, e.g. submit_plan(summary="…", steps=["Change every call-to-action '
        "button label to 'Get Started'\"]). Even a SINGLE step is enough for a small revision — "
        "one step naming the exact edit is a valid, complete plan. `submit_plan` is the only "
        "available action; do not reply in prose and do not submit an empty steps list.\n"
        "</system-reminder>"
    )


_FORCE_SUBMIT_DIRECTIVE = _force_submit_directive(1)


def _force_submit_repeats(events: list[Event]) -> int:
    """Forced-submit directives already emitted this run, plus one.

    Label first, first-firing content second, so pre-2026-08-07b events still
    count and a resumed run never restarts the escalation.
    """
    return 1 + sum(
        1
        for event in events
        if isinstance(event, MessageEvent)
        and (
            event.meta.get("diagnostic") == _FORCE_SUBMIT_DIAGNOSTIC
            or (event.message is not None and event.message.content == _FORCE_SUBMIT_DIRECTIVE)
        )
    )
# After this many consecutive prose-only nudges during a REVISION re-plan, escalate to the
# forced-submit recovery (narrow tools to submit_plan). Gated default-ON; kill switch
# DISCO_REVISION_FORCE_SUBMIT=0.
_REVISION_FORCE_SUBMIT_K = 2
_INVALID_PLAN_DONE_CONDITION_CAP = 3

# Bug 12 (§11.4) — refusal for a mutating tool that reaches the apply boundary
# AFTER a change/revision steer landed mid-step (the in-flight write-through race).
# The build has been put back into PLANNING; the model must submit a revised plan.
def _midstep_steer_refusal(tool: str) -> str:
    """Mid-step steer refusal, rendered from the refused call (constraint 1)."""
    return (
        "<system-reminder>\n"
        f"REFUSED: `{tool}` was not applied. A change request arrived while you were "
        "mid-step, so this action would have landed on the OLD, now-stale plan. The "
        "build has re-entered PLANNING. Fold the new request into a REVISED plan and "
        "call `submit_plan`; once it is approved you can apply the change. You may also "
        "read (file_read/file_list/search/extract) or ask/questions_v2 first.\n"
        "</system-reminder>"
    )


def _out_of_phase_submit_plan_refusal(revision: int | None) -> str:
    approved = (
        f"Plan revision {revision} is already APPROVED and execution is active. "
        if revision is not None
        else "The loop is in EXECUTION mode, not PLANNING mode. "
    )
    return (
        "<system-reminder>\n"
        "REFUSED: `submit_plan` had no effect. "
        f"{approved}Do not retry `submit_plan`; continue the approved work with "
        "execution tools. If new user input or execution evidence proves the plan "
        "itself must change, use `propose_plan_update` instead.\n"
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
    "selected by the entry file at `path`) — make sure your server is serving on port 8000 (the "
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
            "description": "Workspace-relative path: entry file (app) or file/folder (files).",
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
        name="finish",
        description=_FINISH_DESCRIPTION,
        parameters_schema=_FINISH_SCHEMA,
        behavior=_FINISH_BEHAVIOR,
    )


def _finish_alias_tool_spec(alias: str):
    """A FRESH ToolSpec for the contract finalizer alias — same schema/description as
    `finish`, contract-specific name. Built per-alias; never mutates the cached finish
    singleton."""
    from ..llm.types import ToolSpec

    return ToolSpec(
        name=alias,
        description=_FINISH_DESCRIPTION,
        parameters_schema=_FINISH_SCHEMA,
        behavior=_FINISH_BEHAVIOR,
    )


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
        behavior=_FINISH_BEHAVIOR,
    )


def _remember_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="remember",
        description=_REMEMBER_DESCRIPTION,
        parameters_schema=_REMEMBER_SCHEMA,
        behavior=_REMEMBER_BEHAVIOR,
    )


def _serve_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="serve",
        description=_SERVE_DESCRIPTION,
        parameters_schema=_SERVE_SCHEMA,
        behavior=_FINALIZE_ONLY_BEHAVIOR,
    )


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
        behavior=_DELEGATE_BEHAVIOR,
    )


_DELEGATE_EXPLORE_TOOL_SPEC = None

TerminalCommitHook = Callable[[StatusEvent], Awaitable[Event]] | None
ControlFenceFactory = Callable[[], AbstractAsyncContextManager[None]] | None
_ACTIVE_AGENT_VIEW_ID: ContextVar[str | None] = ContextVar(
    "disco_active_agent_view_id", default=None
)


class AgentViewSuperseded(RuntimeError):
    """This worker lost durable ownership of the model view it was driving."""


@asynccontextmanager
async def _optional_async_fence(factory: ControlFenceFactory):
    if factory is None:
        yield
        return
    async with factory():
        yield


async def _admit_run_intent_for_view(
    store: EventStore,
    conversation_id: str,
    events: list[Event],
) -> tuple[list[Event], str | None]:
    """Publish and verify one fresh generation for the exact model view."""

    intent = latest_workspace_run_intent(events)
    if intent is None:
        return events, None
    # A newer atomic USER+intent or competing view may land between our append
    # and re-read. Only call the provider when our exact marker is still latest.
    for _attempt in range(16):
        view_id = f"aview_{uuid.uuid4().hex}"
        await store.append(
            conversation_id,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                agent_view_id=view_id,
                run_intent_id=intent.id,
                run_protocol_version=1,
            ),
        )
        events = await store.get_events(conversation_id)
        newest = latest_workspace_run_intent(events)
        if newest is not None and newest.id == intent.id:
            if current_workspace_agent_view_id(events) == view_id:
                return events, view_id
            raise AgentViewSuperseded("another worker admitted the same run intent")
        if newest is None:
            return events, None
        intent = newest
    raise RuntimeError("agent view admission could not stabilize on the current intent")


def _delegate_explore_tool_singleton():
    global _DELEGATE_EXPLORE_TOOL_SPEC
    if _DELEGATE_EXPLORE_TOOL_SPEC is None:
        _DELEGATE_EXPLORE_TOOL_SPEC = _delegate_explore_tool_spec()
    return _DELEGATE_EXPLORE_TOOL_SPEC


# _last_productive_seq / _is_web_deliverable / _browser_verified / _latest_browser_error
# + _DoDWorkspaceUnavailable moved to loop/finish.py (re-exported above).





def _new_retry_interrupt() -> asyncio.Event:
    """Create the event shared by provider backoff and cooperative controls."""
    return asyncio.Event()


async def _route_event(
    store: EventStore,
    conversation_id: str,
    event: Event,
    terminal_commit_hook: TerminalCommitHook = None,
) -> Event:
    """Route Build status transitions through the workspace coordinator.

    FINISHED becomes an immutable commit pipeline; other statuses share its
    serialization lock so a run cannot become active during a host mutation.
    Non-status events and loops without the hook retain the ordinary store path.
    """
    if terminal_commit_hook is not None and isinstance(event, StatusEvent):
        return await terminal_commit_hook(event)
    return await store.append(conversation_id, event)


