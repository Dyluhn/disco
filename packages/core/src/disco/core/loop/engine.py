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
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm import StreamChunk
    from .boundaries import StreamHook

from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    predicate_from_obj,
)
from ..dod_evaluator import DoDEvaluator
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    AlternativeOption,
    AlternativesEvent,
    ConversationStatus,
    DeliverableEvent,
    ErrorEvent,
    Event,
    EventSource,
    KnowledgeEvent,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from ..llm import (
    Difficulty,
    LLMContextWindowExceeded,
    LLMError,
    LLMRouter,
    LLMTransientError,
    OperatingMode,
    OverflowSignal,
)
from ..state import ConversationState
from ..store.base import EventStore
from ..view import Condenser, Summarizer, View, _latest_plan, microcompact
from .bootstrap import _detect_project_bootstrap
from .boundaries import Agent, ConfirmationPolicy, SecurityAnalyzer, StopHook, ToolExecutor
from .control import Disp
from .dedup import (
    _F8_PREFIX_CHARS,
    _F8_TRUNCATION_MARKER_TEMPLATE,
    _f8_confirmed_file_writes,
    _f9_dedupable_read,
)
from .fc_kit import _nearest_tool_name
from .messages import (
    _describe_llm_error,
    _hs03_reground_message,
    _stuck_escape_reminder,
    _workspace_paths_from_events,
)
from .stream_extract import extract_partial_string_field
from .stuck import StuckDetector, StuckThresholds
from .tool_specs import (
    _ask_user_tool_singleton,
    _clarify_tool_singleton,
    _notify_user_tool_singleton,
    _propose_plan_update_tool_singleton,
)

_LOG = logging.getLogger("disco.loop")

_sleep = asyncio.sleep
_DRIVER_RETRY_BACKOFFS_S: tuple = (10.0, 30.0, 90.0)


# Plan/meta tools that mutate bookkeeping state but do no real work. Excluded
# from "did the agent act?" accounting everywhere (valve taxonomy + the
# actionless streak) so a model can't look productive by shuffling plan state.
_BOOKKEEPING_TOOLS = frozenset({"submit_plan", "propose_plan_update", "plan_step", "finish"})




# plan_step-spam guard (issue C). plan_step cycling through different step indices
# evades BOTH the StuckDetector (each call's args differ → not "identical") AND the
# noop valve (which deliberately skips bookkeeping tools). So it gets its own
# trailing-streak guard, symmetric to _plan_step_lag_signal (which nudges the
# OPPOSITE case — real work done but not checked off). Soft nudge first, hard halt
# well below the 15-31× spam observed live; thresholds are conservative so a weak
# model doing a small legitimate bookkeeping burst is never penalized.
_BOOKKEEPING_STREAK_NUDGE_AT = 3
_BOOKKEEPING_STREAK_HALT_AT = 6
# Slack added to the active plan's step count when sizing the HALT cap, so a
# model that legitimately marks each plan step done (one `plan_step` per step)
# plus a couple of over-corrections/verifications isn't penalized. T7 (E3):
# without this, a model emitting N plan_step calls on an N-step plan tripped
# the cap and halted the run mid-way through the legitimate burst.
_BOOKKEEPING_PLAN_SLACK = 2

# C8 (T11): bound the autonomous `propose_plan_update` loop. A weak model in
# autonomous mode can hammer the same plan revision over and over, never
# realizing there's no human to approve it. When the proposed plan's steps
# are byte-identical to the immediately-prior plan (ignoring summary; appends
# count as different) for >= this many consecutive auto-approved revisions,
# feed the existing bookkeeping-stuck valve so the run halts (STUCK) rather
# than looping. The existing (c.3) bookkeeping valve emits `bookkeeping_only`
# — we reuse that signal instead of inventing a new one.
_PROPOSE_PLAN_UPDATE_REPEAT_CAP = 3

# finish-verify cap (issue B). The model-authored `verify` gate was the ONLY uncapped
# gate in the loop — a broken/always-failing verify command could refuse `finish`
# forever. Mirror the existing _browser_verify_refusals release valve: after N REAL
# failures, finish anyway with a LOUD warning (the failure stays visible — important
# for weak local models — rather than grinding to max_iterations). A MALFORMED verify
# (SyntaxError / command-not-found) is a broken check, not a failed task, so it is
# auto-stripped (bounded by _finish_verify_strips so it can't be gamed as a free finish).
_FINISH_VERIFY_CAP = 3

# C1c DoD-gate refusal cap. The external Definition-of-Done gate refuses `finish`
# while the spec is unmet, but — exactly like the verify cap above — it MUST be
# bounded: an agent that cannot satisfy the external DoD would otherwise be
# trapped in an unbounded refuse-and-continue loop, accumulating events without
# end (this is the OOM the uncapped first cut caused). After N consecutive
# refusals the gate RELEASES (finish lands) with a LOUD warning; the prior
# refusal events remain the visible audit trail.
_DOD_REFUSAL_CAP = 3

_WS_MAX_FILES = 8  # cap the snapshot breadth (most-recently-touched first)
_WS_PER_FILE_CHARS = 6_000  # per-file cap; larger files head/tail-truncate with a marker
_WS_TOTAL_CHARS = 16_000  # total snapshot budget (~4k tokens), bounded vs the condenser
_WS_READ_TIMEOUT_S = 2.0  # per-file read cap — the snapshot runs under the conversation
#                           lock, so a hung sandbox read must never freeze control ops


# D2: the reserved AlternativesEvent option id for "Continue anyway" — the bypass the
# user can always pick at the circuit-breaker gate to reset the failure streak and let
# the agent keep going. The frontend renders it as a distinct button; pick_alternative
# special-cases it (reset streak + resume) rather than running a tool.
_CONTINUE_OPTION_ID = "__continue__"

_DEFAULT_VETO_FEEDBACK = (
    "<system-reminder>\n"
    "The goal does not appear complete yet. Continue working toward it — a stop "
    "hook refused the FINISHED transition.\n"
    "</system-reminder>"
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
# The view.py tag for the tail recitation. We look at the last rendered
# message to decide whether to keep it — if it starts with this sentinel it
# IS the recap, otherwise there is no plan to recite yet (no recap to gate).
_RECITATION_SENTINEL = "<current-objective>"

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
    "a clear `question` (for ONE missing detail) or `clarify` (for SEVERAL) to get "
    "them BEFORE planning. Prefer asking over guessing on "
    "details the user explicitly required. You may also keep reading (file_list, "
    "file_read, search, extract) for more context, but a prose reply alone doesn't "
    "advance the conversation.\n"
    "</system-reminder>"
)

# A hard temperature jitter for the single stuck-escape retry step. When the loop
# detects a repeating action→error/obs rut it gives the model ONE retry at this
# temperature (vs. the driver's small anti-fewshot default) to break the
# self-imitation chain, BEFORE declaring STUCK. Pairs with a rotating reminder
# pool (C7) so consecutive escape attempts differ in bytes as well as in
# sampling temperature — otherwise a model that has internalized the previous
# reminder would echo it back verbatim and the escape wouldn't break the
# self-imitation chain (the test on test_loop_stuck.py locks this in).
_STUCK_ESCAPE_TEMP = 0.9

# Symmetric to _PLAN_NUDGE on the execution side: a hard gate that refuses FINISHED
# until the agent has done productive work since the most recent plan approval.
# Small open models sometimes echo the plan as prose and declare "done" without
# touching anything — the gate catches that and re-enters the loop. Phrased as a
# `<system-reminder>` (ambient, implicit) rather than a user-tone scolding: the
# model sees an automated environment notification, not a confrontation.
_EXECUTION_NUDGE = (
    "<system-reminder>\n"
    "The approved plan has not been executed yet — no workspace files have been "
    "written, edited, or run since approval. Continue by calling a tool "
    "(file_write, file_edit, shell, code_exec, …) to carry out the plan's steps "
    "in order. The plan is in your context above.\n"
    "</system-reminder>"
)
# The reminder keeps firing as long as the agent tries to declare done without
# acting, BUT the gate is NOT uncapped: a nudge-only turn emits no ActionEvent,
# so `max_iterations` (reconstructed from action events) never advances on it —
# it is NOT the backstop here. The gate routes each actionless finish-attempt
# through `_actionless_valve` (the same circuit breaker the (g) no-op path uses),
# which lands the run cleanly after `_max_consecutive_noops` turns. The
# `_execution_nudges` counter is telemetry so tests can observe firing.

# The tool names that don't count as "productive work" for the execution gate:
# meta tools + READ-ONLY/inspection tools that don't change workspace state. The
# build-finish gate must require a STATE-CHANGING action since plan approval — a
# model that only READ the files (file_read/list/search/extract/preview/browser)
# and then declared done has delivered nothing (caught live: a macOS-clone
# iteration "finished" after 5 file_reads with zero edits). plan_step is
# informational; the planning tool would have been intercepted upstream.
_NON_PRODUCTIVE_TOOLS = frozenset(
    {
        "submit_plan",
        "plan_step",
        "ask_user",
        "propose_plan_update",
        "notify_user",
        "finish",
        "remember",  # bookkeeping — recording a fact isn't task progress on its own
        "serve",  # a handoff marker, not task work itself
        # read-only / inspection: gather context but never change the deliverable
        "file_read",
        "file_list",
        "search",
        "extract",
        "server_status",
        "shell_view",
        "shell_wait",
        "browser",
    }
)


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


def _remember_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="remember", description=_REMEMBER_DESCRIPTION, parameters_schema=_REMEMBER_SCHEMA
    )


def _serve_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(name="serve", description=_SERVE_DESCRIPTION, parameters_schema=_SERVE_SCHEMA)


# E4 — static-site verify. A static deliverable shouldn't have to curl a running
# server to prove it's good; the honest post-condition is "the file exists and is
# parseable HTML." The agent signals this with verify="static" (default index.html)
# or verify="static:<path>". We translate it to a server-free python3 check that
# runs through the SAME safe gate as any verify command (it assesses LOW — no
# confirm). exit 0 ⇔ the page exists, is non-trivial, and parses.
_STATIC_VERIFY_PREFIX = "static"


def _static_verify_command(path: str) -> str:
    p = (path or "index.html").strip().strip("'\"") or "index.html"
    # single-quote the path safely for the shell, then hand to python3 -c
    safe = p.replace("'", "'\\''")
    script = (
        "import sys,os.path,html.parser as H;"
        f"p='{safe}';"
        "(os.path.isfile(p) or sys.exit('missing '+p));"
        "d=open(p,encoding='utf-8',errors='replace').read();"
        "(len(d.strip())>=20 or sys.exit('empty '+p));"
        "t=[];pr=H.HTMLParser();pr.handle_starttag=lambda n,a:t.append(n);pr.feed(d);"
        "print('OK '+p+' tags='+str(len(t)));"
        "sys.exit(0 if t else 'no html tags in '+p)"
    )
    return f'python3 -c "{script}"'


# verify="app" / "app:<url>" — SERVER-AWARE self-verification (the verify_app
# gap). Where `static` only proves a file exists + parses, `app` proves the
# RUNNING deliverable actually serves: it GETs the URL inside the sandbox and
# requires HTTP 200 + a non-trivial body. Portable (python3/urllib — no curl/
# chromium dependency) so it runs on every backend; routes through the same safe
# verify gate. A full pixel screenshot needs a browser in the sandbox image (not
# present on the process backend) — this is the honest server-up post-condition.
_APP_VERIFY_PREFIX = "app"


def _app_verify_command(url: str) -> str:
    u = (url or "http://localhost:8000/").strip().strip("'\"") or "http://localhost:8000/"
    safe = u.replace("'", "'\\''")
    # No try/except (a `-c` one-liner can't carry the block): a connection failure
    # raises URLError → nonzero exit + a traceback the agent reads as "not serving".
    script = (
        "import sys,urllib.request as U;"
        f"u='{safe}';"
        "r=U.urlopen(u,timeout=10);"
        "code=getattr(r,'status',None) or r.getcode();"
        "(code==200 or sys.exit('HTTP '+str(code)+' from '+u));"
        "b=r.read().decode('utf-8','replace');"
        "(len(b.strip())>=20 or sys.exit('empty body from '+u));"
        "print('OK '+u+' '+str(code)+' bytes='+str(len(b)))"
    )
    return f'python3 -c "{script}"'


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
# The helper's input is the driver's `question` + `context` joined into one
# prompt. Bound the size of each so a driver cannot grow the helper's input
# unboundedly within a single segment — the result is folded back into the
# View, which the condenser manages; keeping the helper's prompt bounded
# keeps the post-fold View growth bounded. Symmetric to the search/extract
# length budget (`packages/tools/.../retrieval.py:_EXTRACT_CHAR_BUDGET`).
_FANOUT_INPUT_MAX_CHARS = 4_000
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


def _last_productive_seq(events: list[Event]) -> int:
    """Seq of the last state-changing action (not in _NON_PRODUCTIVE_TOOLS).
    If no productive action found, returns 0."""
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            if ev.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
                return ev.seq or 0
    return 0


def _is_web_deliverable(events: list[Event]) -> bool:
    """Web deliverable if index.html was written/edited OR port 8000 owned by non-preview.
    Derived from events to keep the check pure (event-list in, verdict out)."""
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            if ev.tool_call.tool_name in (
                "file_write",
                "file_edit",
                "file_append",
                "file_replace_lines",
                "file_insert_lines",
            ):
                path = ev.tool_call.arguments.get("path")
                # Canonical workspace-root paths
                if path in ("index.html", "./index.html"):
                    return True
        elif isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "server_status":
            content = ev.tool_result.content
            # "  - 8000: OWNED by pid 123 (python) [session: dev]"
            for line in content.splitlines():
                if "8000: OWNED" in line and "[session: " in line:
                    session = line.split("[session: ")[1].split("]")[0]
                    if session != "preview":
                        return True
    return False


def _browser_verified(events: list[Event], since_seq: int) -> tuple[bool, str | None]:
    """Scan browser observations since since_seq. Returns (ok, first_error_line).
    An observation is valid if it's from the browser tool, against port 8000,
    and has zero console errors. If not ok, returns the first error from the
    LATEST qualifying observation."""
    valid_obs = []
    for ev in events:
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser":
            res = ev.tool_result
            if res.success and res.structured:
                url = str(res.structured.get("url", ""))
                if url.startswith("http://127.0.0.1:8000") or url.startswith(
                    "http://localhost:8000"
                ):
                    valid_obs.append(res.structured)

    if not valid_obs:
        return False, None

    # ok = at least one observation with zero console errors
    ok = any(
        not any(c.get("level") == "error" for c in obs.get("console", [])) for obs in valid_obs
    )

    # first_error_line = from the LATEST qualifying-URL observation that has error-level entries
    first_error_line = None
    for obs in reversed(valid_obs):
        errors = [
            str(c.get("text", "")) for c in obs.get("console", []) if c.get("level") == "error"
        ]
        if errors:
            first_error_line = errors[0]
            break

    return ok, first_error_line


def _latest_browser_error(events: list[Event]) -> str | None:
    """First error-level console line of the LATEST qualifying browser observation
    (any seq — full history). None if the agent never browsed :8000 or its last
    look was clean. This feeds the gate's human-facing messages: the verdict is
    scoped to since-last-edit (_browser_verified), but "last console errors"
    must report what was actually last SEEN — a post-browse edit moves since_seq
    past the observation and would otherwise erase a real, observed error."""
    for ev in reversed(events):
        if not (isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser"):
            continue
        res = ev.tool_result
        if not (res.success and res.structured):
            continue
        url = str(res.structured.get("url", ""))
        if not (
            url.startswith("http://127.0.0.1:8000") or url.startswith("http://localhost:8000")
        ):
            continue
        errors = [
            str(c.get("text", ""))
            for c in res.structured.get("console", [])
            if c.get("level") == "error"
        ]
        return errors[0] if errors else None
    return None


class _DoDWorkspaceUnavailable(Exception):
    """The C1c gate cannot resolve a workspace_root (no sandbox, no
    `workspace_path` attribute on the executor). Raised by
    `_build_dod_evaluator` and caught by `_finish_dod_gate_passed` to
    degrade the gate to a no-op (logged, never raised past the gate).

    Distinct from a predicate failure: the spec is set but the engine
    has no evidence surface to grade against. Refusing the finish in
    that state would be a silent fail — the agent would loop forever
    on a gate that cannot run. Logging + skipping is the honest
    behavior; the audit trail sees the log line."""


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
        assist: bool = False,
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
        dod_evaluator_factory: Callable[[], DoDEvaluator] | None = None,
    ) -> None:
        # Autonomous mode (issue A): no human is available to answer questions or
        # approve plans (headless / unattended runs). Default False = today's
        # interactive behavior, fully unchanged. When True: ask_user/clarify are
        # withheld from the tool schema, the plan is auto-approved inline, and the
        # circuit-breaker's "hand off to the user" becomes a clean forfeit (STUCK)
        # instead of an indefinite AWAITING_USER_DECISION stall.
        self._autonomous = autonomous
        # Assist mode (T1): signals weak-model assist tier
        self._assist = assist
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
        self._stuck = StuckDetector(stuck_thresholds)
        self._veto_feedback = veto_feedback
        # C1c — DoD evaluator gate (wires the C1b fresh-context judge into the
        # finish branch). The factory returns a fully-configured evaluator; the
        # default builds one over the executor's sandbox workspace_root. Tests
        # inject a fake-seamed evaluator via the same hook. None means "use the
        # default factory" (the evaluator is still wired when a DoD spec exists).
        # When no DoD spec exists for the conversation, the gate is a no-op
        # (legacy byte-identical path) — see _finish_dod_gate_passed.
        self._dod_evaluator_factory: Callable[[], DoDEvaluator] | None = dod_evaluator_factory
        # Consecutive DoD-refusal streak (telemetry; the gate has no cap — the
        # loop's max_iterations + the user's kill switch are the ultimate exit,
        # same as the browser-verify and execution-nudge gates).
        self._dod_refusals = 0
        self._lock = asyncio.Lock()
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

    # ---- emission + small helpers -------------------------------------------

    # Tools whose streamed arguments carry a file body worth watching assemble.
    _STREAMING_WRITE_TOOLS = ("file_write", "file_append", "write_file")
    # Coalesce threshold: don't publish a frame until this many new content chars
    # have accrued (or a newline appears) — keeps the type-out smooth without
    # firing a WS frame per 3-char model token. The trailing remainder below the
    # threshold is delivered by the authoritative ActionEvent, so nothing is lost.
    _STREAM_FLUSH_CHARS = 24

    def _build_stream_hook(self) -> StreamHook | None:
        """Per-step watch-it-write hook (or None if no sink is wired). Decodes the
        driver's streamed tool-call arg fragments into growing file-content frames
        and publishes them live via `self.stream_sink`. The frontend appends each
        `delta` to a per-path buffer and reconciles against the final, authoritative
        ActionEvent when it lands (which supersedes the streamed text)."""
        sink = self.stream_sink
        if sink is None:
            return None
        # Per-step, per-tool-index accumulator (fresh each step → no stale state).
        state: dict[int, dict] = {}

        async def _hook(chunk: StreamChunk) -> None:
            st = state.setdefault(
                chunk.tool_index, {"args": "", "sent": 0, "path": None, "tool": ""}
            )
            st["args"] += chunk.tool_args_delta
            if chunk.tool_name:
                st["tool"] = chunk.tool_name
            if st["tool"] not in self._STREAMING_WRITE_TOOLS:
                return  # only stream tools that carry a file body
            if not st["path"]:
                # Require the WHOLE path (closing quote present) so a frame never
                # shows a half-typed filename like "styles" for "styles.css".
                p = extract_partial_string_field(st["args"], "path", require_complete=True)
                if p:
                    st["path"] = p
            content = extract_partial_string_field(st["args"], "content")
            if content is None:
                return
            new = content[st["sent"] :]
            if len(new) < self._STREAM_FLUSH_CHARS and "\n" not in new:
                return  # coalesce — wait for more
            st["sent"] = len(content)
            sink(
                {
                    "type": "file_stream",
                    "tool": st["tool"],
                    "path": st["path"] or "",
                    "index": chunk.tool_index,
                    "delta": new,
                }
            )

        return _hook

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
        return events[-self._stuck.t.scan_window :]

    def _overflow_signal(self, events: list[Event]) -> OverflowSignal:
        """Derive the router's overflow inputs from the log: trailing tool errors
        feed router rule 4 (stuck recovery) so a struggling step can auto-escalate
        to overflow *before* STUCK is declared (§6 note)."""
        consecutive = 0
        for e in reversed(events):
            if isinstance(e, AgentErrorEvent):
                consecutive += 1
            elif isinstance(e, ActionEvent | ObservationEvent | MessageEvent):
                break
        return OverflowSignal(difficulty=Difficulty.ROUTINE, consecutive_tool_errors=consecutive)

    @staticmethod
    def _estimate_tokens(view: View) -> int:
        # ~4 chars/token heuristic. A-S1 fix: the old version summed only message
        # `.content`, ignoring tool-call argument JSON, tool schemas, and the
        # system prompt — so the real prompt was LARGER than estimated yet the
        # trigger still fired. Now we also count serialized tool-call args and add
        # a fixed allowance for the system prompt + tool-schema prefix.
        chars = 0
        for m in view.messages:
            chars += len(m.content or "")
            for tc in getattr(m, "tool_calls", None) or []:
                # tool_calls may be dicts or pydantic models; stringify defensively.
                chars += len(str(getattr(tc, "arguments", None) or tc))
        # System prompt + tool-schema prefix allowance (~2k tokens), counted so the
        # trigger reflects the true prompt size, not just the transcript tail.
        return chars // 4 + 2_000

    # ---- plan-mode helpers (Build) ------------------------------------------

    def _readonly_tool_names(self) -> frozenset[str] | None:
        """The executor's set of read-only tool names, or None if this executor
        can't report it (older/fake executors). None → the capability backstop
        is skipped and only the name allowlist governs (legacy behavior); a real
        DefaultToolExecutor always reports, so the backstop is live in prod."""
        fn = getattr(self.executor, "readonly_tool_names", None)
        if fn is None:
            return None
        try:
            return frozenset(fn())
        except Exception:  # noqa: BLE001 — never let tool-listing crash the loop
            return None

    def _tools_for_step(self, *, suppress_meta_tools: bool = False) -> list:
        """Mode-scoped tool visibility. With no planning_tools configured this is a
        pass-through (Research / default). While PLANNING the agent sees ONLY the
        planning tool(s); while executing it sees everything else.

        suppress_meta_tools (Phase-B re-runs #4/#5, 2026-06-10): until the
        first real action of a session (post-resume or conversation start),
        ALL virtuals except finish — notify_user, remember, serve, ask_user,
        propose_plan_update — are WITHHELD from the offered set. FOUR
        distinct post-resume degenerations in a row (prose spam,
        serve-before-work, remember spam, ask_user-as-narration) each
        escaped through whatever meta channel remained; shaping the action
        space beats refusing after the fact (the model can't pick what
        isn't offered). The first turn of a session must be a real tool
        call. Asking/re-planning become available after one real attempt —
        an evidence-backed question beats a preemptive one. finish stays
        (verify-on-finish gates it).

        In execution mode the loop also appends a VIRTUAL `ask_user` tool — a
        clean escape hatch the model can call when it (in its own reasoning)
        decides it needs human input. The loop intercepts the call (the tool
        is never executed by the executor); see the ASK-USER GATE in the run
        loop. This is the Claude Code pattern: the tool is *available*, the
        model *discovers and chooses* it, the harness does not nudge it."""
        tools = self.executor.available_tools()
        if self.mode == OperatingMode.PLANNING:
            # The PLANNING agent is READ-ONLY (Claude-Code plan-mode parity): it
            # gathers context and proposes a plan; writes/exec are off the table
            # until approval. TWO independent, fail-safe guards:
            #   (1) capability backstop (ALWAYS, when the executor can report it):
            #       drop any tool not marked read_only — so even a misconfigured
            #       name allowlist that names a write tool can't leak it.
            #   (2) name allowlist (when configured): restrict further to the
            #       operator's curated set.
            readonly = self._readonly_tool_names()  # frozenset | None (None=unknown)
            allow = self._planning_tools

            def _planner_ok(name: str | None) -> bool:
                if readonly is not None and name not in readonly:
                    return False  # capability backstop — never a mutating tool
                if allow:
                    return name in allow  # allowlist restricts further
                return True

            planner_tools = [t for t in tools if _planner_ok(getattr(t, "name", None))]
            # Append the VIRTUAL ask_user + clarify even while planning: an under-specified
            # task most needs clarification BEFORE a plan is committed (the user
            # named a detail only they know). ask_user is read-only-safe — the loop
            # intercepts it (never executes it against the sandbox) and halts at the
            # Ask-gate, same as in execution. clarify is the MULTI-QUESTION variant
            # for when several specifics are missing. Without this the planner is forced
            # to guess and bury the unknown in the plan instead of just asking.
            #
            # C20 — `delegate_explore` is intentionally ABSENT from the planning
            # tool set. It is an EXECUTION-only tool: the call DISPATCHES a
            # subagent (an action that yields an observation, not a pure
            # read). The planner gathers context with the read-only tools it
            # already has (file_read, file_list, search, extract) and proposes
            # a plan; the fan-out helper is available to the driver in
            # execution mode only. Belt-and-suspenders: the tool def is also
            # `read_only=False`, so even a misconfigured readonly backstop
            # would exclude it from the planning branch.
            if self._autonomous:
                # Autonomous/headless: WITHHOLD the ask gates — there is no human
                # to answer, so the autonomous planner proceeds with the
                # documented "assume + proceed" default instead of stalling on an
                # ask. (Mirrors the execution-branch withholding at the
                # `if not self._autonomous` gate below; required by
                # test_autonomous_withholds_ask_gates_in_planning.)
                return planner_tools
            return planner_tools + [
                _ask_user_tool_singleton(),
                _clarify_tool_singleton(),
            ]
        if self._planning_tools:
            tools = [t for t in tools if getattr(t, "name", None) not in self._planning_tools]
        # Append the virtual ask_user + clarify + propose_plan_update tools in execution
        # mode. All are documented so the model decides WHEN to use them;
        # neither is injected by reminder. propose_plan_update is the model's
        # auto-recovery affordance: when its current plan is wrong, it proposes
        # a revision and the user accepts/refines via the plan-approval gate.
        virtuals = [_finish_tool_singleton()]
        if not suppress_meta_tools:
            # Autonomous mode withholds the ask gates (no human to answer); the model
            # is told to assume + proceed. The rest stay — propose_plan_update is the
            # model's self-correction affordance (auto-approved when autonomous).
            if not self._autonomous:
                virtuals += [_ask_user_tool_singleton(), _clarify_tool_singleton()]
            virtuals += [
                _propose_plan_update_tool_singleton(),
                _notify_user_tool_singleton(),
                _remember_tool_singleton(),
                _serve_tool_singleton(),
                # C20 — read-only Explore/Plan helper dispatch+join. Available
                # in execution mode ONLY (the PLANNING branch of
                # _tools_for_step intentionally does NOT append it — see the
                # PLANNING branch for the rationale; `delegate_explore` is an
                # EXECUTION-only tool because the call dispatches a subagent
                # and is therefore an ACTION, not a pure read). Cap is
                # enforced at the call site, not in the schema (a model that
                # calls it past the cap is refused with a system-reminder,
                # the same shape the (c.3) bookkeeping-stuck nudge uses).
                _delegate_explore_tool_singleton(),
            ]
        return list(tools) + virtuals

    def _plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        """Build a PlanEvent from a `submit_plan` tool call. Defensive against the
        model's shape drift (steps as dicts or bare strings); revision counts prior
        plans so a re-plan is visibly the next iteration. `context` carries any
        markdown rationale / exploration findings the planner included.

        C18 — also harvest each step's optional `done_condition` (an advisory
        DoDPredicate) into `self._plan_step_predicates` keyed by
        (revision, 1-based index). The predicate is later evaluated when the
        agent emits `plan_step(idx, 'done')`. Malformed predicates (wrong
        `kind`, missing fields, non-dict) are silently dropped — the step
        degrades to "no predicate" exactly like a step that omitted the
        field in the first place. C18 is advisory, not a gate, so a bad
        predicate never blocks the plan from being approved."""
        steps: list[PlanStep] = []
        # Re-seed per-plan-revision (a re-plan supersedes the prior map; we
        # don't keep stale predicates from an obsolete revision).
        revision = 1 + sum(1 for e in events if isinstance(e, PlanEvent))
        for s in arguments.get("steps") or []:
            if isinstance(s, dict):
                title = str(s.get("title") or s.get("step") or s.get("name") or "").strip()
                detail = s.get("detail") or s.get("description")
                if title:
                    steps.append(PlanStep(title=title, detail=str(detail) if detail else None))
            elif isinstance(s, str) and s.strip():
                steps.append(PlanStep(title=s.strip()))
        if not steps:
            steps = [PlanStep(title="(the planner returned no concrete steps)")]
        summary = str(arguments.get("summary") or "").strip() or "Proposed plan"
        context = str(arguments.get("context") or arguments.get("rationale") or "").strip()
        # C18 — harvest the predicates (revision-scoped). We walk the raw
        # args (not the rebuilt `steps`) so we can preserve the 1-based
        # step index even when the title/format was leniently coerced.
        raw_steps = arguments.get("steps") or []
        for one_based, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict):
                continue
            cond = raw.get("done_condition")
            if not isinstance(cond, dict):
                # Optional field, absent by default — back-compat: steps
                # without a predicate behave exactly as today.
                continue
            try:
                predicate = predicate_from_obj(cond)
            except Exception:  # noqa: BLE001 — malformed predicate is advisory-only
                # A bad shape is logged once at WARNING (not ERROR — a
                # broken advisory note is not a run failure) and the
                # step silently drops the predicate.
                _LOG.warning(
                    "C18: malformed done_condition on plan step %d (revision %d); "
                    "ignoring (advisory only): %r",
                    one_based, revision, cond,
                )
                continue
            self._plan_step_predicates[(revision, one_based)] = predicate
        return PlanEvent(summary=summary, steps=steps, revision=revision, context=context)

    def _alternatives_from_args(
        self, arguments: dict, events: list[Event]
    ) -> AlternativesEvent | None:
        """Build an AlternativesEvent from an `ask_user` tool call's options.
        Defensive against the model's shape drift — options may come as dicts
        with various key names. Correlates with the most recent ActionEvent so
        the UI can show which step the alternatives are answering.

        Returns None when the args are too malformed to produce a useful gate;
        the loop falls back to a free-form question (see the ASK-USER GATE)."""
        raw_options = arguments.get("options") or arguments.get("alternatives") or []
        options: list[AlternativeOption] = []
        for i, opt in enumerate(raw_options):
            if not isinstance(opt, dict):
                continue
            tool_name = str(
                opt.get("tool_name")
                or (opt.get("tool_call") or {}).get("name")
                or (opt.get("tool_call") or {}).get("tool_name")
                or ""
            ).strip()
            if not tool_name:
                continue
            args = (
                opt.get("arguments")
                or (opt.get("tool_call") or {}).get("arguments")
                or {}
            )
            if not isinstance(args, dict):
                continue
            title = str(opt.get("title") or opt.get("label") or f"Option {i + 1}").strip()
            description = str(opt.get("description") or opt.get("why") or "").strip()
            option_id = str(opt.get("id") or f"opt_{i + 1}")
            options.append(
                AlternativeOption(
                    id=option_id,
                    title=title,
                    description=description,
                    tool_name=tool_name,
                    arguments=args,
                )
            )
        # `ask_user` semantics: the question text is the primary signal; options
        # are optional clickable alternatives. With no options, the loop emits
        # the question as a model message instead of building an AlternativesEvent
        # (handled by the caller — see the ASK-USER GATE).
        if not options:
            return None
        summary = str(
            arguments.get("question")
            or arguments.get("summary")
            or arguments.get("goal")
            or ""
        ).strip()
        if not summary:
            summary = "the agent is asking which path to take"
        # Correlate with the most recent failed action (the immediate predecessor).
        failed_action_id = ""
        for e in reversed(events):
            if isinstance(e, ActionEvent):
                failed_action_id = e.id
                break
        # D2 — always append the "Continue anyway" bypass so the user is never trapped
        # at a decision gate: pick it to reset the failure streak and let the agent
        # keep going with its own judgment (pick_alternative special-cases this id).
        options = [
            *options,
            AlternativeOption(
                id=_CONTINUE_OPTION_ID,
                title="Continue anyway",
                description="Let the agent keep working with its own best next step.",
                tool_name="",  # not a tool — the loop intercepts this id
                arguments={},
            ),
        ]
        return AlternativesEvent(
            failed_action_id=failed_action_id,
            summary=summary,
            options=options,
        )

    @staticmethod
    def _productive_action_since_approval(events: list[Event]) -> bool:
        """Has the agent done any state-changing or information-gathering work since the
        most recent plan approval? Used to gate the execution-mode FINISHED transition:
        if False, the loop refuses to finish (the model would be declaring done without
        having acted). If there is no plan_approved marker (plan-mode never entered, or
        not yet approved), the gate is inert — return True so the regular finish path
        runs untouched."""
        # find the seq of the most recent plan-approval marker
        approval_seq: int | None = None
        for e in reversed(events):
            if isinstance(e, StatusEvent) and e.detail == "plan_approved":
                approval_seq = e.seq
                break
        if approval_seq is None:
            return True  # no plan-first lifecycle here; don't gate the finish
        # any ActionEvent after that point whose tool is NOT a meta tool counts
        for e in events:
            if (e.seq or 0) <= approval_seq:
                continue
            if isinstance(e, ActionEvent) and e.tool_call is not None:
                if e.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
                    return True
        return False

    @staticmethod
    def _hard_deny_reason(action: ActionEvent) -> str | None:
        """Cluster 3: a non-negotiable command-level refusal. Returns a reason if
        the action is a hard-denied shell command (mkfs, raw-device write, fork
        bomb, rm -rf /), else None. Refused outright before the confirm gate."""
        from ..security.analyzers import hard_deny_reason

        tc = action.tool_call
        if tc is None or tc.tool_name not in ("shell", "shell_exec", "code_exec"):
            return None
        command = str(tc.arguments.get("command") or tc.arguments.get("code") or "")
        return hard_deny_reason(command)

    @staticmethod
    def _count_recent_failures(events: list[Event]) -> int:
        """Consecutive AgentErrorEvents walking back from the tail. Reset by a
        successful ObservationEvent or a USER message (a fresh instruction).
        Interleaved ActionEvents and agent/env messages do NOT reset. Drives the
        Cluster 2 circuit breaker."""
        streak = 0
        for e in reversed(events):
            if isinstance(e, AgentErrorEvent):
                streak += 1
            elif isinstance(e, ObservationEvent):
                break
            elif isinstance(e, MessageEvent) and e.source == EventSource.USER:
                break
        return streak

    @staticmethod
    def _recovery_requested_since_reset(events: list[Event]) -> bool:
        """D2 guard: True if a circuit-breaker recovery was ALREADY requested in the
        current failure streak (a `recovery_requested` StatusEvent before the streak's
        reset boundary — a USER message or successful Observation). Stops the breaker
        from re-asking every iteration; the second time through it falls to the step
        (agent proposes) or, if it failed again, the hard halt."""
        for e in reversed(events):
            if isinstance(e, StatusEvent) and e.detail == "recovery_requested":
                return True
            if isinstance(e, ObservationEvent) and e.tool_result.success:
                return False
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                return False
        return False

    @staticmethod
    def _stuck_escape_seq(events: list[Event]) -> int | None:
        """The seq of the most recent `stuck_escape` marker since the last USER
        message, else None. Reset only on a USER message — NOT on a successful
        observation: pattern-1 stuck (the same *succeeding* no-op action repeated)
        would otherwise reset every cycle and reframe forever. One reframe escape per
        user turn; a second stall in the same turn halts."""
        for e in reversed(events):
            if isinstance(e, StatusEvent) and e.detail == "stuck_escape":
                return e.seq
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                return None
        return None

    @staticmethod
    def _stuck_escape_attempt_count(events: list[Event]) -> int:
        """C7 — total number of `stuck_escape` markers emitted so far in this
        conversation. Used to select the rotating reminder text (the model's
        view of the previous escape's reminder, if byte-identical, would
        invite self-imitation). Counts ALL markers (not just since-last-user
        message) so the rotation is global within a run: a model that has
        seen reminder index 0 in a prior turn will see a different index on
        the next escape, even after a user message resets the loop state.
        Returns 0 when no escape has happened yet (the first escape gets
        index 0, the second gets index 1, ...)."""
        n = 0
        for e in events:
            if isinstance(e, StatusEvent) and e.detail == "stuck_escape":
                n += 1
        return n

    @staticmethod
    def _consecutive_noops(events: list[Event]) -> int:
        """Count trailing agent steps consumed WITHOUT a real executed action:
        prose MessageEvents, DeliverableEvents (the serve intercept), and
        duplicate-`remember` ActionEvents (the dedup pair). Resets on any real
        ActionEvent or a USER message; plan bookkeeping and fresh
        KnowledgeEvents are neutral (neither break nor count).

        The original version counted only prose messages and broke on ANY
        ActionEvent — so a model spamming `serve` emitted 50+ DeliverableEvents
        through the intercept `continue` path and never tripped a valve
        (Phase-B re-run, 2026-06-10); only the stuck detector, 95 events
        later, stopped it."""
        count = 0
        for e in reversed(events):
            if isinstance(e, ObservationEvent):
                continue  # paired with its action — judge the action instead
            if isinstance(e, ActionEvent):
                tool = e.tool_call.tool_name if e.tool_call is not None else None
                if tool == "remember":
                    count += 1  # only the duplicate path emits remember actions
                    continue
                if tool in _BOOKKEEPING_TOOLS:
                    continue  # plan shuffling: neither real work nor spam signal
                break
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                break
            if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
                count += 1
                continue
            if isinstance(e, DeliverableEvent):
                count += 1
                continue
        return count

    @staticmethod
    def _auto_continue_attempts(events: list[Event]) -> int:
        """Count auto-continue events fired since the most recent USER message.
        The cap resets every time the user sends a fresh prompt — each new
        instruction gets its own auto-continue budget. The harness uses this
        to keep the loop moving without infinite-looping."""
        count = 0
        for e in reversed(events):
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                return count
            if (
                isinstance(e, StatusEvent)
                and e.detail
                and e.detail.startswith("auto_continue:")
            ):
                count += 1
        return count

    @staticmethod
    def _plan_is_incomplete(events: list[Event]) -> tuple[bool, list[int]]:
        """Is the most recent plan only partially done? Returns (incomplete, missing_idxs).

        Walks the log to find the latest PlanEvent and the plan_step(index, state)
        ActionEvents that report per-step progress. A step is "complete" iff the
        agent emitted plan_step(idx, state="done") for it. If there's no plan at
        all, returns (False, []) — nothing to gate on.

        This is the truth-source for the FINISHED gate: if a plan exists and any
        step is unmarked or stuck "active", the loop refuses to transition to
        FINISHED and falls back to STUCK — honest about the work being incomplete
        rather than lying about completion."""
        # The latest plan (a re-plan supersedes prior).
        plan: PlanEvent | None = None
        for e in events:
            if isinstance(e, PlanEvent):
                if plan is None or e.revision >= plan.revision:
                    plan = e
        if plan is None or not plan.steps:
            return (False, [])
        # Only count plan_step marks made AFTER the current plan (a re-plan starts a
        # fresh checklist — a prior plan's "done" marks must not satisfy the new gate).
        plan_seq = plan.seq or 0
        done: set[int] = set()
        for e in events:
            if not isinstance(e, ActionEvent) or e.tool_call is None:
                continue
            if e.tool_call.tool_name != "plan_step":
                continue
            if (e.seq or 0) < plan_seq:
                continue
            try:
                idx = int(e.tool_call.arguments.get("index"))  # type: ignore[arg-type]
                state = str(e.tool_call.arguments.get("state"))
            except (TypeError, ValueError):
                continue
            if state == "done":
                done.add(idx)
        total = len(plan.steps)
        missing = [i + 1 for i in range(total) if (i + 1) not in done]
        return (bool(missing), missing)

    @staticmethod
    def _actions_since_last_resume(events: list[Event]) -> int:
        """Count ActionEvents (excluding meta/bookkeeping tools and the
        verify-on-finish probe) since the last
        StatusEvent(RUNNING, detail="resumed") or since start."""
        count = 0
        for e in reversed(events):
            if (
                isinstance(e, StatusEvent)
                and e.status == ConversationStatus.RUNNING
                and e.detail == "resumed"
            ):
                break
            if isinstance(e, ActionEvent) and e.tool_call is not None:
                if e.meta.get("verify_probe"):
                    # The finish gate's own probe — running it is not evidence
                    # the AGENT did real work (re-run #6 leak).
                    continue
                if e.tool_call.tool_name not in _BOOKKEEPING_TOOLS:
                    count += 1
        return count

    async def _actionless_valve(self, events: list[Event], noops: int) -> bool:
        """Shared circuit-breaker ladder for steps that consumed a model turn
        without doing real work — the tool-less noop path AND the non-blocking
        intercepts (notify_user / remember / serve). Returns True when the run
        was landed (PAUSED/FINISHED) — the caller must
        `return await self.get_state()`. Emits the warning nudge in place and
        returns False otherwise.

        The Phase-B re-run (2026-06-10) is why the intercepts must share this:
        their bare `continue` skipped the noop bookkeeping entirely, so ~50
        serve spams and ~20 prose messages sailed past every DC-05a cap until
        the stuck detector fired ~95 events later."""
        incomplete, _ = self._plan_is_incomplete(events)
        if incomplete and noops >= self._ACTIONLESS_BREAK_CAP:
            await self._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "The agent produced 3 consecutive responses"
                            " without doing any real work while plan steps"
                            " remain undone — pausing instead of burning"
                            " tokens. Resume to continue."
                        ),
                    ),
                )
            )
            await self._emit(
                StatusEvent(status=ConversationStatus.PAUSED, detail="actionless")
            )
            return True

        if noops >= self._max_consecutive_noops:
            # The model is spinning without acting and won't stop — end
            # cleanly rather than burn. (A real run resumes on a user steer;
            # the prompt steers toward finish/act.)
            actions_since = self._actions_since_last_resume(events)
            if incomplete and actions_since == 0:
                await self._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n⚠ finishing was"
                                " blocked: plan steps remain undone and"
                                " no work happened in this run segment."
                                "\n</system-reminder>"
                            ),
                        ),
                    )
                )
                await self._emit(
                    StatusEvent(status=ConversationStatus.PAUSED, detail="noop_limit")
                )
            else:
                await self._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"You have produced {noops} turns in a row without "
                                "performing real work. Ending the run. To continue, "
                                "the user can send a new instruction; otherwise call "
                                "a tool to act or `finish` to complete.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                await self._emit(
                    StatusEvent(status=ConversationStatus.FINISHED, detail="noop_limit")
                )
            return True

        if noops == self._max_consecutive_noops - 1:
            await self._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            "You've sent several messages without acting. Call a "
                            "tool to make progress, or `finish` if the task is "
                            "complete.\n"
                            "</system-reminder>"
                        ),
                    ),
                )
            )
        return False

    @staticmethod
    def _plan_step_lag_signal(events: list[Event]) -> bool:
        """The 'auditor' for the soft plan-step nudge. Returns True when the
        agent has done substantial productive work but the capstone tracker is
        clearly lagging — the classic 'did the work, forgot to check it off'
        failure. The nudge that follows is SOFT (a gentle suggestion, not a
        gate), and fires at most once per lag episode (no re-fire until the
        agent marks another step or the lag clears).

        Heuristic (all derivable from the log, stateless):
          - a plan exists with steps, and we're past plan approval
          - productive (state-changing) actions since approval >= total steps
            (i.e. enough work has happened that *something* should be marked)
          - fewer than half the steps are marked done (tracker is behind)
          - no soft-lag nudge has fired since the last plan_step action
            (so we nudge once per episode, not every iteration)
        """
        # Latest plan.
        plan: PlanEvent | None = None
        approval_seq: int | None = None
        for e in events:
            if isinstance(e, PlanEvent):
                if plan is None or e.revision >= plan.revision:
                    plan = e
            if isinstance(e, StatusEvent) and e.detail == "plan_approved":
                approval_seq = e.seq
        if plan is None or not plan.steps or approval_seq is None:
            return False
        total = len(plan.steps)

        productive = 0
        steps_done: set[int] = set()
        last_plan_step_seq = -1
        last_lag_nudge_seq = -1
        for e in events:
            seq = e.seq or 0
            if seq <= approval_seq:
                continue
            if isinstance(e, ActionEvent) and e.tool_call is not None:
                name = e.tool_call.tool_name
                if name == "plan_step":
                    last_plan_step_seq = seq
                    try:
                        if str(e.tool_call.arguments.get("state")) == "done":
                            steps_done.add(int(e.tool_call.arguments.get("index")))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        pass
                elif name not in _NON_PRODUCTIVE_TOOLS:
                    productive += 1
            elif (
                isinstance(e, MessageEvent)
                and e.source == EventSource.ENVIRONMENT
                and e.message is not None
                and "plan-step tracker" in e.message.content
            ):
                last_lag_nudge_seq = seq

        if productive < total:
            return False  # not enough work yet to expect check-offs
        if len(steps_done) * 2 >= total:
            return False  # tracker is keeping up (>= half done)
        # Only nudge once per episode: skip if a lag nudge already fired more
        # recently than the last plan_step action (the agent hasn't checked
        # anything off since we last reminded it — no point repeating).
        if last_lag_nudge_seq > last_plan_step_seq:
            return False
        return True

    @staticmethod
    def _bookkeeping_streak_len(events: list[Event]) -> int:
        """Trailing count of consecutive BOOKKEEPING-only actions (plan_step /
        submit_plan / propose_plan_update — NOT `finish`, which is intercepted before
        it lands as an ActionEvent) since the last real action, user message, or
        resume/approval marker. This is the signal the StuckDetector and noop valve
        both miss for plan_step spam (issue C)."""
        plan_bk = _BOOKKEEPING_TOOLS - {"finish"}
        streak = 0
        for e in reversed(events):
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                break
            if isinstance(e, StatusEvent) and e.detail in ("resumed", "plan_approved"):
                break
            if isinstance(e, ActionEvent) and e.tool_call is not None:
                if e.tool_call.tool_name in plan_bk:
                    streak += 1
                else:
                    break  # a real (state-changing) action ends the streak
            # observations / other status events between actions are skipped
        return streak

    @staticmethod
    def _active_plan_step_count(events: list[Event]) -> int:
        # Step count of the latest PlanEvent, or 0 if no plan has been
        # approved yet. Used by the (c.3) bookkeeping halt (T7 / E3) to scale
        # the spam cap with the plan's actual size -- a model finishing an
        # N-step plan may legitimately emit up to ~N `plan_step` calls; the
        # halt threshold becomes max(HALT_AT, N + slack) so a legit burst
        # does not trip it.
        plan = None
        for e in events:
            if isinstance(e, PlanEvent):
                if plan is None or e.revision >= plan.revision:
                    plan = e
        if plan is None or not plan.steps:
            return 0
        return len(plan.steps)

    @staticmethod
    def initial_mode(
        events: list[Event],
        *,
        planning: OperatingMode = OperatingMode.PLANNING,
        execution: OperatingMode = OperatingMode.LONG_HORIZON,
        default: OperatingMode = OperatingMode.INTERACTIVE,
    ) -> OperatingMode:
        """Reconstruct the loop's operating mode from the log so a rebuilt loop
        (process restart) resumes in the right phase. Reads the last mode marker
        stamped on a StatusEvent.detail by approve_plan / enter_planning."""
        for e in reversed(events):
            if isinstance(e, StatusEvent) and e.detail == "plan_approved":
                return execution
            if isinstance(e, StatusEvent) and e.detail == "planning":
                return planning
        return default

    # ---- view materialization + condensation (§8) ---------------------------

    async def _workspace_snapshot_message(self, events: list[Event]) -> LLMMessage | None:
        """Re-derive the CURRENT on-disk content of the working-set files from the
        sandbox each turn and render it as an authoritative, always-fresh message.

        WHY (root cause, source-verified + reproduced live): a file_write's body is
        elided at render time to "<N chars elided — use file_read>" (events._snip_args,
        >1.5k chars), and observation-masking + condensation can later erase a
        file_read's output too. A weak driver that doesn't proactively file_read
        every turn therefore loses sight of what's on disk and regenerates files
        from lossy memory — clobbering prior content (reproduced: a 4,441-char file
        rewritten to 202 chars, all constants + fingerprint gone).

        The OSS-proven fix (Aider's ChatChunks.chat_files re-read via io.read_text
        each turn; OpenHands str_replace operating on current on-disk text) is to
        re-derive file content from the SOURCE OF TRUTH (disk) every turn and keep
        it OUT of the condensable history. This message is rebuilt here, post-
        projection, so View.of's snip/mask/condense never touch it and the
        condenser (which acts on EVENTS) can never erase it. Returns None if there
        is no sandbox or no tracked files — degrading to prior behavior.

        Layering-safe: reaches the sandbox via the same duck-typed
        getattr(self.executor, "sandbox", None) seam as _execute_and_observe."""
        sbx = getattr(self.executor, "sandbox", None)
        if sbx is None:
            return None
        mutated, read_only = _workspace_paths_from_events(events)
        ordered = mutated + read_only  # mutated first → never evicted by reads (#1)
        if not ordered:
            return None
        preamble = (
            "# CURRENT WORKSPACE — your files on disk RIGHT NOW (authoritative).\n"
            "Below is the live, exact content of the files you are working on, "
            "re-read from disk this turn. It OVERRIDES any earlier or elided copy of "
            "these files shown above; trust THIS over your memory.\n"
            "To change a file, the RELIABLE way is to call `file_write` with the FULL "
            "new content = everything shown below for that file PLUS your change. "
            "Keep the docstring, every constant, and every existing function you are "
            "not deliberately removing — do not drop anything. Adding to a file is "
            "expected and good. AVOID line-number edits (`file_replace_lines` / "
            "`file_insert_lines`) for these changes: line numbers shift as you edit "
            "and a wrong range silently deletes code — a whole-file `file_write` "
            "based on the content below cannot miscount.\n"
            "**SILENT CONTEXT** — use this block without narrating it. Do NOT "
            "acknowledge the snapshot in your reply (no 'I can see the files', "
            "'the workspace shows…', 'good, the content is here', etc.). "
            "Just continue the work.\n\n"
        )
        blocks: list[str] = []
        # E4 (T8) — per-file notes appended to the trailing omitted-notice.
        # Each note tells the model exactly WHY a file it touched is NOT
        # rendered as a BEGIN/END block (binary, deleted, permission). The
        # model can then `file_read` it itself when it needs the content.
        omitted_notes: list[str] = []
        budget = _WS_TOTAL_CHARS - len(preamble)
        shown_count = 0
        # E4 (T8) — .disco-spill-* are T10's overflow logs (head/tail markers
        # of an oversize stdout), not deliverables. They pollute the working
        # set with multi-MB noise and dwarf every real file. Skip them
        # outright: the model has no business re-reading its own spill log.
        # Match the leaf filename (basename) so an absolute path the model
        # might use (e.g. /workspace/.disco-spill-abc.log) is caught too.
        spill_basename_prefix = ".disco-spill-"
        for path in ordered:
            if shown_count >= _WS_MAX_FILES or budget <= 0:
                break
            # E4 (T8) — skip T10's overflow-log paths up-front
            if os.path.basename(path).startswith(spill_basename_prefix):
                continue
            # C5 — skip the `.pmx/` write-through mirror. It is the on-disk
            # durable copy of the in-View KnowledgeEvent channel (see
            # _write_pmx_memory_fact in this file). The View is the
            # authoritative in-session source; the file is just a recovery
            # aid for hard filesystem resets. Surfacing it in the working-
            # set snapshot would (a) be a divergent second store from the
            # model's POV and (b) double-count the same fact (once in the
            # View, once in the mirror). Exclude it like `.disco-spill-*`.
            if ".pmx" in path.split("/"):
                continue
            try:
                raw = await asyncio.wait_for(
                    sbx.read_file(path), timeout=_WS_READ_TIMEOUT_S
                )
            except TimeoutError:
                # A hung read implies a wedged/dead sandbox. STOP — don't hold the
                # conversation lock for timeout×N files (that would block pause/steer/
                # cancel). Degrade to the snapshot collected so far. The sandbox isn't
                # healed here (this is a pure projection step); the model's NEXT real
                # action goes through _execute_and_observe, which detects the dead
                # sandbox and emits the restart notice.
                break
            except FileNotFoundError:
                # E4 (T8) — file was deleted between the action and this read.
                # Surface that as an explicit note (the model can re-decide
                # whether to re-create the file); don't silently lose it.
                omitted_notes.append(f"[file gone: {path}]")
                continue
            except PermissionError:
                # E4 (T8) — the sandbox denied this read. Note it so the
                # model knows the path exists in its working set but the
                # snapshot cannot show it (the model can still try file_read).
                omitted_notes.append(f"[unreadable: permission: {path}]")
                continue
            except (IsADirectoryError, NotADirectoryError, OSError) as e:
                # E4 (T8) — a directory slipped into the working set (e.g. the
                # model `file_write`d a directory path by mistake). Skip with
                # a brief note; do not include its binary directory listing.
                if isinstance(e, IsADirectoryError) or (
                    isinstance(e, OSError) and getattr(e, "errno", None) == 21  # EISDIR
                ):
                    omitted_notes.append(f"[directory: {path}]")
                    continue
                # Any other OSError — keep the prior silent-skip behavior
                # (the existing contract was "file gone/unreadable: skip").
                continue
            except Exception:  # noqa: BLE001 — file gone/unreadable this turn: skip it
                continue
            # E4 (T8) — binary sniff. A NUL byte in the first 1KB is a near-
            # certain signal of binary content (text decoders reject it;
            # editors render garbage; the snapshot's only value is showing
            # the model something it can act on). Bounded sample (1KB, not
            # the whole file) so a huge text file with one stray NUL past
            # the head — e.g. an embedded null in a template — still
            # renders normally via `errors="replace"`.
            if b"\x00" in raw[:1024]:
                omitted_notes.append(f"[binary omitted: {path}]")
                continue
            text = raw.decode("utf-8", "replace")
            cap = min(_WS_PER_FILE_CHARS, budget)
            if len(text) > cap:
                head = cap * 3 // 4
                tail = cap - head
                shown = (
                    f"{text[:head]}\n"
                    f"… [{len(text) - head - tail:,} chars truncated — this file is "
                    "too large to show in full. "
                    "Before editing it, call file_read on this path to see the full content. "
                    "For a large file like this, make targeted changes with file_edit "
                    "(content-anchored old→new); "
                    "do NOT call file_write with regenerated content, which risks "
                    "dropping the parts not shown here.] …\n"
                    f"{text[-tail:]}"
                )
            else:
                shown = text
            # Plain text delimiters — NOT markdown ``` fences. A fenced snapshot
            # invites the model to reflexively copy the ``` into its file_write
            # (models are trained to wrap code in ```), polluting the real file and
            # then thrashing to strip it (caught live on gpt-oss-120b). file_read
            # returns raw content with no fences; the snapshot matches that.
            block = (
                f"----- BEGIN FILE {path} ({len(text):,} chars on disk) -----\n"
                f"{shown}\n"
                f"----- END FILE {path} -----"
            )
            budget -= len(block)  # charge the WHOLE block incl header/markers (#6)
            blocks.append(block)
            shown_count += 1
        # E4 (T8) — return None only when there is NOTHING to tell the model.
        # If every file was omitted (binary / gone / permission / spill) we
        # still want to surface the notes so the model knows the working
        # set is not empty on disk — it just couldn't be rendered.
        if not blocks and not omitted_notes:
            return None
        omitted = len(ordered) - shown_count
        # Compose the trailing notice: budget-omitted count (existing
        # behavior) PLUS the E4 per-file notes. A single trailing paragraph
        # keeps the snapshot's shape consistent; the notes are short
        # one-liners the model can act on (`file_read`, recreate, etc.).
        notice_parts: list[str] = []
        if omitted > 0:
            notice_parts.append(
                f"[{omitted} more file(s) you have touched are not shown here "
                f"(snapshot budget) — file_read them when you need their content.]"
            )
        notice_parts.extend(omitted_notes)
        notice = ("\n\n" + "\n".join(notice_parts)) if notice_parts else ""
        return LLMMessage(role="user", content=preamble + "\n\n".join(blocks) + notice)

    # ---- C6: recitation cadence + drift gate ---------------------------------

    def _recitation_signature(self, events: list[Event]) -> str | None:
        """C6 — stable signature of (latest plan + plan_step checklist), the
        inputs the view.py tail-recap renders. None iff there is no plan to
        recite (a re-plan or a pre-plan run has no signature).

        Mirrors view.py:_recitation_message's accounting exactly: only
        plan_step marks AFTER the current PlanEvent's seq count, and the
        state set is split into done vs active (the recap's "✓" / "→" /
        "□" markers). Any change in plan.summary, plan.steps, or the
        done/active set flips the signature → DRIFT fires the next
        materialization (once).

        Returned as a string (not bytes) so the comparison in
        `_should_emit_recitation` is cheap and order-insensitive (sorted
        tuples). Hashing is unnecessary — the content is short and we
        never re-hash for any other purpose.
        """
        plan = _latest_plan(events)
        if plan is None or not plan.steps:
            return None
        plan_seq = plan.seq or 0
        done: list[int] = []
        active: list[int] = []
        for e in events:
            if not isinstance(e, ActionEvent) or e.tool_call is None:
                continue
            if e.tool_call.tool_name != "plan_step":
                continue
            if (e.seq or 0) < plan_seq:
                continue  # mark belongs to a superseded plan
            try:
                idx = int(e.tool_call.arguments.get("index"))  # type: ignore[arg-type]
                state = str(e.tool_call.arguments.get("state"))
            except (TypeError, ValueError):
                continue
            if state == "done":
                done.append(idx)
            elif state == "active":
                active.append(idx)
        steps_t = tuple((s.title, s.detail) for s in plan.steps)
        return (
            f"plan:{plan.id}:{plan.revision}:{plan.summary}|"
            f"steps:{steps_t}|"
            f"done:{sorted(done)}|active:{sorted(active)}"
        )

    def _should_emit_recitation(self, events: list[Event]) -> bool:
        """C6 — return True iff the view.py tail-recap should be appended on
        THIS step. Pure predicate (no mutation, no logging); the side
        effects (signature update, step counter increment) live in
        `_materialize_view` so a test can call this in isolation.

        Fires when ANY of:
        - The plan is new or has changed since the last recap (DRIFT). The
          first eligible step ALWAYS drifts (signature is None).
        - The current step is a cadence boundary. The step counter is
          1-indexed by the time we get here (incremented in
          `_materialize_view`); the smolagents math is
          `(step_number - 1) % interval == 0` → fires on steps 1, 1+N,
          1+2N, … (the first step is always a boundary, regardless of
          cadence value).
        Returns False when no plan exists yet (view.py already returns
        None for the recap; nothing to gate) or when neither signal
        fires (drop the message — the model still sees the underlying
        PlanEvent + plan_step ActionEvents in the prior messages).
        """
        sig = self._recitation_signature(events)
        if sig is None:
            return False
        on_cadence = ((self._recitation_step_count - 1) % self._recitation_cadence) == 0
        drift = sig != self._recitation_last_signature
        return on_cadence or drift

    def _gate_recitation(self, view: View, events: list[Event]) -> View:
        """C6 — drop the tail-recap message unless this step is a cadence
        boundary or the plan/checklist has drifted. The recap CONTENT is
        unchanged (the underlying PlanEvent + plan_step actions remain
        visible earlier in the messages list, so the model never loses
        the goal — just the redundant every-step re-render). No new
        steering text is added; this is a passive recap cadence, not a
        steer (no-automatic-nudge invariant c97c1b3).
        """
        # C6: count the step (0-indexed) — one increment per materialize
        # call, regardless of whether a re-projection happened inside
        # (e.g. after a condensation). That keeps the cadence tied to
        # MODEL TURNS, not View re-materializations.
        self._recitation_step_count += 1
        if not (
            view.messages and view.messages[-1].content.startswith(_RECITATION_SENTINEL)
        ):
            # No tail recap in the rendered View (no plan yet) — nothing
            # to gate. Don't touch the signature: the next step with a
            # plan will drift on signature != None.
            return view
        if self._should_emit_recitation(events):
            # The View already has a tail-recap; the gate fired (cadence
            # boundary or drift). Record the signature so the NEXT step
            # can detect drift.
            self._recitation_last_signature = self._recitation_signature(events)
            return view
        # Gate fires: drop the tail-recap. The model still has the
        # PlanEvent + plan_step events in the prior messages list, so
        # the plan info is not lost — just not redundantly re-rendered.
        return view.model_copy(update={"messages": view.messages[:-1]})

    # ---- HS-03: scheduled facts re-grounding (assist-tier only) -------------

    def _should_emit_reground(self, events: list[Event]) -> bool:
        """HS-03 — pure predicate: should this step emit the re-ground
        recap? Fires when EITHER of:
          * POST-RESUME: this is the first eligible step of a fresh run
            segment (actions_since_last_resume == 0) AND the post-resume
            one-shot has not already fired. Mirrors the F4 _bootstrap_emitted
            shape, but is reset on every run() so a resume gets a fresh
            one-shot (the brief is explicit: "ONCE immediately after a
            restart/resume"). The recap is anchored to the fresh
            post-resume state so a model that lost context across the
            pause re-anchors.
          * CADENCE: the current action count is a multiple of
            `self._hs03_reground_cadence` (default
            `_HS03_REGROUND_INTERVAL` = 12; tests inject a smaller
            value) AND we did not already emit at this exact boundary.
            The per-boundary guard
            (`_hs03_reground_last_action_count`) prevents re-firing on
            consecutive steps at the same boundary — e.g. two steps
            in a row both seeing count==12 (a noop interleaved) would
            otherwise emit twice. Only actions since the last resume
            count toward cadence (the brief's wording); a long-running
            session that survives a resume restarts the count.

        Pure predicate: no emission, no side effects on the store. The
        caller is responsible for the actual emit and for updating the
        flags. Returns False when assist is OFF (caller does not need to
        check `self._assist` — this is the gate; the closed-end-to-end
        guarantee lives here), or when the brief's preconditions
        aren't met (no plan yet, no boundary yet, already-fired).

        A no-plan conversation returns False even when the gate
        conditions match: there is no goal to anchor to. The recap is
        a re-ground on the existing plan, not a free-floating
        "remember what we're doing" prompt.
        """
        if not self._assist:
            return False  # closed end-to-end — capable-model default is byte-identical
        if _hs03_reground_message(events) is None:
            return False  # no plan → nothing to recap
        actions = self._actions_since_last_resume(events)
        if actions == 0 and not self._hs03_reground_post_resume_emitted:
            return True  # first eligible step of a fresh segment
        if actions > 0 and (actions % self._hs03_reground_cadence) == 0:
            # Per-boundary guard: don't re-emit at the same boundary
            # on two consecutive steps. The first time we see a
            # boundary we emit; subsequent steps at the same count
            # are silent.
            return actions != self._hs03_reground_last_action_count
        return False

    async def _maybe_emit_reground(self, events: list[Event]) -> list[Event]:
        """HS-03 — gate, build, emit. Returns the (possibly refreshed)
        event list: when an emit fires, the caller should re-poll
        `self._events()` so the materialize step on the same turn
        sees the recap in the View. Mirrors the F4 bootstrap idiom
        (emit + `events = await self._events()`).

        Assist OFF → no-op, the input list is returned unchanged. The
        gate is closed at `_should_emit_reground`; this method is a
        thin wrapper that handles the emit + flag update + event-list
        refresh so the run() call site stays one line.

        Why a MessageEvent (not a separate RecapEvent): the existing
        `MessageEvent` with `source=ENVIRONMENT` + role=user is the
        contract for harness-injected system-reminders across F4
        (bootstrap), F8 (file-write args), C7 (stuck escape), and the
        actionless-valve. A new event type would be a new contract;
        the sentinel-tagged body is enough to identify the recap in
        tests + in the View.

        Why a fresh MessageEvent (not a View-side append like C6): C6
        drops a recap the View ALREADY rendered (the tail-recap is
        built into view.py:_recitation_message and gated at materialize
        time). HS-03 needs a recap that is independent of the View's
        current render — it must persist to the event log so a future
        View materialization includes it, and so tests can assert the
        emit happened (the persisted event IS the test's evidence).
        """
        if not self._should_emit_reground(events):
            return events
        recap = _hs03_reground_message(events)
        if recap is None:  # defensive: the predicate already checked
            return events
        # _actions_since_last_resume is computed again to keep the
        # per-boundary flag precise (the predicate saw the same
        # number; storing it here is the single point of truth).
        actions = self._actions_since_last_resume(events)
        await self._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=recap,
            )
        )
        if actions == 0:
            # Post-resume one-shot fired. Mark so subsequent steps
            # with actions == 0 (e.g. a tool-less first turn) don't
            # re-emit. Cadence still applies once actions > 0.
            self._hs03_reground_post_resume_emitted = True
        # Record the boundary we just emitted at. Even on the
        # post-resume one-shot we record (at count 0) so a step that
        # somehow also hit the cadence condition (count % N == 0
        # trivially when count is 0) doesn't double-fire — though
        # the post-resume flag already prevents that.
        self._hs03_reground_last_action_count = actions
        # Refresh the event list so the materialize step on the same
        # turn sees the recap in the View (F4's idiom: emit then
        # re-poll so the bootstrap is in the next render).
        return await self._events()

    async def _materialize_view(self, events: list[Event]) -> View:
        # S3 Microcompact (GAP A): a cheap, no-model pass FIRST — tombstone no-op
        # turns (a failed call an identical later call superseded) so the lossy
        # model-summarization condenser fires on a smaller, denser residue (or not
        # at all). Idempotent: re-running won't re-tombstone an already-dropped span.
        micro = microcompact(events)
        if micro:
            for tomb in micro:
                await self._emit(tomb)
            events = await self._events()
        view = View.of(events)
        # A8: build the live workspace snapshot up-front so its size is counted in
        # the condense decision (steelman finding #4 — otherwise the condenser
        # undercounts the true prompt by ~4k tokens every turn, re-opening the same
        # estimation gap the A-S1 fix closed). The snapshot content is disk-derived
        # and independent of condensation, so building it before the condense check
        # and attaching it after is sound.
        snapshot = await self._workspace_snapshot_message(events)
        snap_tokens = len(snapshot.content) // 4 if snapshot is not None else 0
        est = self._estimate_tokens(view) + snap_tokens
        # H3: log the prompt size per step so cost regressions are visible (the 60k
        # bloat was invisible because nothing measured it). DEBUG-level; cheap.
        _LOG.debug("driver view: ~%d input tokens, %d messages", est, len(view.messages))
        req = self.condenser.should_condense(view, token_count=est)
        if req is not None:
            tombstone = await self.condenser.condense(events, view, summarizer=self.summarizer)
            if tombstone is not None:
                await self._emit(tombstone)
                view = View.of(await self._events())
            # Soft trigger with no tombstone this step: proceed uncondensed and
            # retry next iteration (§8). Non-fatal.
        # Append the snapshot AFTER any condensation (so it is never rebuilt away by
        # a re-projection) and outside View.of (so the render-time snip/mask never
        # touch it). Disco's equivalent of Aider's always-fresh chat_files chunk.
        # C6 — gate the tail-recap on cadence + drift BEFORE the snapshot
        # append so the gate inspects a message list whose LAST element
        # is the recap (or the final condensation-rebuilt tail), not the
        # snapshot. The gate never touches the snapshot; the snapshot
        # is appended AFTER the gate regardless of the gate's verdict
        # (it's authoritative on-disk content the model needs).
        view = self._gate_recitation(view, events)
        # F8 — GATED mid-turn arg truncation (assist-tier context-window
        # reclaim). When assist is ON, replace the long `content` argument
        # in any past assistant message whose `file_write` tool call was
        # CONFIRMED successful with a short prefix + a path-aware marker.
        # Lossless: the full content lives on disk (and in the snapshot) —
        # `file_read <path>` recovers it. Render-time only: the persisted
        # event log is unchanged. Assist OFF (the capable-model default)
        # → no-op; the rendered messages are byte-identical to today.
        # Runs AFTER the snapshot append so the snapshot (the
        # authoritative current state) is never shrunk, and so any
        # older assistant message in the history has the F8 transform
        # applied to it on every step.
        if self._assist:
            view = view.model_copy(
                update={
                    "messages": self._f8_shrink_file_write_args(view.messages, events),
                }
            )
        if snapshot is not None:
            _LOG.info(
                "A8 workspace snapshot injected: %d chars across the working set",
                len(snapshot.content),
            )
            view = view.model_copy(update={"messages": [*view.messages, snapshot]})
        return view

    def _f8_shrink_file_write_args(
        self, messages: list[LLMMessage], events: list[Event]
    ) -> list[LLMMessage]:
        """F8 — GATED render-time transform. For every assistant message
        whose tool_call is a ``file_write`` that was CONFIRMED successful
        (per :func:`_f8_confirmed_file_writes`), replace the long
        ``content`` argument with a short prefix + a recoverable marker.

        Render-time only: the persisted event log is unchanged. The
        transform runs AFTER ``View.of`` (and the existing
        ``_ARG_SNIP_CHARS`` shaper in events.py) so it OVERRIDES the
        generic "<N chars elided — use file_read>" marker with a more
        useful form: a real 200-char prefix + a path-aware hint. The
        on-disk full content is the recovery surface (file_read /
        workspace snapshot).

        Lossless for the wire (the marker names the file's path, so
        ``file_read <path>`` recovers the full content) and lossless
        for the event log (the event's ``tool_call.arguments["content"]``
        is never modified — only the rendered message the provider
        sees is trimmed).

        Assist OFF (the default) → caller does not invoke this method;
        messages are byte-identical to today.

        Returns a new list; the input ``messages`` is not mutated. Each
        modified message is a new LLMMessage (LLMMessage is frozen, so
        ``model_copy`` is required); each modified tool_call dict is a
        new dict.
        """
        confirmed = _f8_confirmed_file_writes(events)
        if not confirmed:
            return messages
        out: list[LLMMessage] = []
        for msg in messages:
            if msg.role != "assistant" or not msg.tool_calls:
                out.append(msg)
                continue
            new_tcs: list[dict] = []
            mutated = False
            for tc in msg.tool_calls:
                if not isinstance(tc, dict):
                    new_tcs.append(tc)
                    continue
                cid = tc.get("id")
                if (
                    tc.get("name") == "file_write"
                    and isinstance(cid, str)
                    and cid in confirmed
                ):
                    path, content = confirmed[cid]
                    # Only shrink when the ORIGINAL content is long
                    # enough that a prefix is meaningful. Short writes
                    # (≤ _F8_PREFIX_CHARS) pass through unchanged —
                    # the snip shaper in events.py did not elide them
                    # either, and the F8 prefix would be the full
                    # content + marker (no reclaim, no value).
                    if len(content) > _F8_PREFIX_CHARS:
                        args = tc.get("arguments")
                        if isinstance(args, dict):
                            new_args = dict(args)
                            new_args["content"] = (
                                content[:_F8_PREFIX_CHARS]
                                + _F8_TRUNCATION_MARKER_TEMPLATE.format(path=path)
                            )
                            new_tc = dict(tc)
                            new_tc["arguments"] = new_args
                            new_tcs.append(new_tc)
                            mutated = True
                            continue
                new_tcs.append(tc)
            if mutated:
                out.append(msg.model_copy(update={"tool_calls": new_tcs}))
            else:
                out.append(msg)
        return out

    async def _collect_pointer_manifest_paths(self, events: list[Event]) -> list[str]:
        """C16 — collect on-disk artifact paths the hard_reset tombstone will
        POINT to (NOT summarize). Three sources, all checked for existence on
        disk so the manifest stays honest (every pointer resolves):

          1. Written deliverables — paths the agent mutated this run (A8's
             mutating-tool set: file_write / file_edit / file_append /
             file_replace_lines / file_insert_lines). Most-recent-first.
          2. Spill logs — `.disco-spill-<uuid>.log` files in the workspace
             (T10's overflow channel; engine.py:1787 already filters them
             from the snapshot because the model has no business re-reading
             them in bulk — but a hard_reset is exactly the time the model
             DOES need to be able to re-read them selectively, so we POINT
             at them instead of summarizing).
          3. `.pmx/MEMORY.md` — standing memory the agent recorded this run
             (C5's MEMORY channel). The view-channel copy is lost on a
             hard filesystem reset; the on-disk copy survives.

        Returns a de-duplicated list, most-recent-first. Paths that don't
        resolve (the sandbox is gone, the file was wiped) are DROPPED — the
        manifest is required to be honest, and a pointer to a missing file
        is worse than no pointer."""
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(p: str | None) -> None:
            if not p or p in seen:
                return
            seen.add(p)
            candidates.append(p)

        # 1. Working-set mutating paths (the agent's deliverable surface).
        mutated, _read = _workspace_paths_from_events(events)
        for p in mutated:
            _add(p)

        sbx = getattr(self.executor, "sandbox", None)
        if sbx is None:
            return candidates

        # 2. Spill logs in the workspace.
        try:
            workspace = getattr(sbx, "workspace_path", None) or ""
            if workspace:
                names = await sbx.list_dir(workspace)
            else:
                # Some backends don't expose workspace_path; fall back to
                # the root. We do not hard-fail on a missing attribute —
                # the manifest degrades gracefully (no spill pointers).
                names = await sbx.list_dir("")  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — list_dir can raise on a dead backend
            names = []
        for name in names:
            if not isinstance(name, str):
                continue
            bn = os.path.basename(name)
            if bn.startswith(".disco-spill-"):
                _add(name)

        # 3. the standing-memory mirror if present (.disco/, or legacy .pmx/).
        for memory_path in (".disco/MEMORY.md", ".pmx/MEMORY.md"):
            try:
                await sbx.read_file(memory_path)
                _add(memory_path)
                break
            except (FileNotFoundError, NotADirectoryError):
                continue
            except Exception:  # noqa: BLE001 — a dead sandbox just skips the pointer
                break

        return candidates

    async def _hard_reset(self, events: list[Event]) -> bool:
        """Forget-and-recover after a context-window error (§8). Returns True
        if a tombstone was appended (progress made).

        C16 — this is a POINTER-ONLY flush (NOT a prose recap). The tombstone's
        summary is a manifest of on-disk artifact paths the model can re-read
        selectively — never a freeform prose summary. The dropped span is
        already too large to re-summarize usefully (it overflowed the
        context window), and a lossy prose recap is worse than pointing at
        the bytes on disk. The soft-condense path (should_condense → condense
        on a token-bound trigger) is byte-unchanged — it still produces a
        prose summary via the summarizer. Only the hard_reset call site
        changes (it now passes reason="hard_reset" + the collected paths)."""
        artifact_paths = await self._collect_pointer_manifest_paths(events)
        tombstone = await self.condenser.condense(
            events,
            View.of(events),
            summarizer=self.summarizer,
            reason="hard_reset",
            artifact_paths=artifact_paths,
        )
        if tombstone is not None:
            await self._emit(tombstone)
            return True
        return False

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
        """C5 — write-through: append one (scope, fact) pair to `.disco/MEMORY.md`
        in the live sandbox. The file is a MIRROR of the in-View KnowledgeEvent
        channel (which is the authoritative in-session source); the file exists
        only so a hard reset / box wipe can re-read it on the fresh instance.

        Best-effort: a missing sandbox is a no-op (sandbox-less executors in
        tests). The on-disk format is a simple Markdown list grouped by scope
        (`## <scope>` heading + `- <fact>` items), which is human-readable and
        easy to parse on rehydrate. NO deduplication here — the in-View
        `remember` handler already dedupes (see _remember_tool_singleton's
        `seen` set in this file), so the file only ever sees novel facts.
        """
        sbx = getattr(self.executor, "sandbox", None)
        if sbx is None:
            return
        path = self._MEMORY_PATH
        # Read existing content (if any). A missing file means a fresh mirror — but
        # first fall back to the legacy .pmx/ path so a pre-rename workspace's facts
        # migrate forward into the new file on the next write.
        try:
            existing = (await sbx.read_file(path)).decode("utf-8", errors="replace")
        except (FileNotFoundError, NotADirectoryError):
            try:
                existing = (
                    await sbx.read_file(self._LEGACY_MEMORY_PATH)
                ).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — no legacy mirror either: fresh start
                existing = ""
        except Exception:  # noqa: BLE001 — read flakiness: start clean
            existing = ""
        lines: list[str] = existing.splitlines() if existing.strip() else [
            "# Standing memory",
            "",
            "Durable facts the agent learned this run (C5: write-through mirror "
            "of the in-View KnowledgeEvent channel).",
            "",
        ]
        if scope:
            header = f"## {scope}"
            if header not in lines:
                lines.append("")
                lines.append(header)
                lines.append("")
            # Insert the fact as a list item directly under the scope header.
            idx = lines.index(header)
            lines.insert(idx + 1, f"- {fact}")
        else:
            lines.append("")
            lines.append(f"- {fact}")
        # Trailing newline keeps the file POSIX-clean.
        await sbx.write_file(path, ("\n".join(lines) + "\n").encode("utf-8"))

    async def _drain_recovered_memory_facts(self) -> int:
        """C5 — pull recovered facts from the session (set by SandboxSession
        when its post-recreate read-back finds `.pmx/MEMORY.md` on the fresh
        box) and re-emit them as KnowledgeEvents. The View channel is the
        authoritative in-session source, so re-emitting brings the in-memory
        knowledge state back in sync with the surviving on-disk mirror.

        Returns the number of facts re-emitted (0 if the session had no
        recovery data — a fresh box that was never written to, or a
        sandbox-less / fake executor in tests).
        """
        sbx = getattr(self.executor, "sandbox", None)
        take = getattr(sbx, "take_recovered_memory_facts", None) if sbx is not None else None
        if take is None:
            return 0
        try:
            facts = take() or []
        except Exception:  # noqa: BLE001 — recovery is best-effort
            return 0
        count = 0
        for scope, snippet in facts:
            if not snippet:
                continue
            await self._emit(
                KnowledgeEvent(source=EventSource.AGENT, scope=scope, snippet=snippet)
            )
            count += 1
        return count

    # ---- execute-and-observe (§4.1) -----------------------------------------

    async def _execute_and_observe(self, action: ActionEvent) -> None:
        """[CONTRACT] Every executed ActionEvent yields exactly one observation
        event (ObservationEvent on success, AgentErrorEvent on failure),
        correlated by action.id.

        NO hidden retries, NO automatic failure escalation. Errors are surfaced
        IMMEDIATELY and IN FULL to the model on its next turn (the Claude Code
        pattern — see the v2 redesign note in this module's header). Recovery
        is a collaboration: the model reasons about the visible error, the user
        watches the trace and can steer at any moment, and the agent has a
        clean `ask_user` tool available when IT decides it needs human input.

        F9 — GATED read-only sliding-window dedup (assist-tier). When
        assist is ON and the current call is a read-only tool that
        EXACTLY repeats a recent read-only call (same tool name + same
        arguments) within the last _F9_WINDOW_SIZE read-only calls, AND
        nothing has invalidated the prior result, short-circuit: emit a
        synthetic ObservationEvent pointing to the prior result instead
        of re-executing. The pointer is short (no content re-render) so
        the model gets the dedup context-savings; the model can always
        re-`file_read` to force a fresh read. Assist OFF → the dedup
        path is never entered; the call falls through to
        executor.execute() exactly as today.

        If the sandbox transparently RECREATED itself during this call (a mid-
        session death the session healed), append an implicit system-reminder
        so the model knows files-on-disk remain but processes/state were lost."""
        # F9 — short-circuit identical read-only calls within the window.
        # Gated on self._assist so capable-model (assist=OFF) runs are
        # byte-identical to today. The dedup fires ONLY when:
        #   1. self._assist is True (the gate — closeable end-to-end);
        #   2. the action is a read-only tool call (per the executor's
        #      readonly_tool_names, falling back to _WORKSPACE_READ_TOOLS);
        #   3. a prior ActionEvent in the last _F9_WINDOW_SIZE read-only
        #      calls has the SAME tool name + SAME arguments;
        #   4. that prior call has a successful observation; AND
        #   5. no mutating tool call has touched the same path between
        #      the prior read and now (stale-read guard).
        # When all five hold we emit a synthetic ObservationEvent
        # carrying the F9 pointer, return WITHOUT calling
        # executor.execute(). The persisted event shape is unchanged
        # (ActionEvent + ObservationEvent pair) — only the observation
        # content differs.
        if self._assist and action.tool_call is not None:
            _f9_events = await self._events()
            _f9_deduped, _f9_prior_id, _f9_pointer = _f9_dedupable_read(
                action.tool_call.tool_name,
                action.tool_call.arguments,
                _f9_events,
                readonly_names=self._readonly_tool_names(),
            )
            if _f9_deduped:
                _LOG.info(
                    "F9 read-dedup: short-circuited %s (call_id=%s, prior_action_id=%s)",
                    action.tool_call.tool_name,
                    action.tool_call.call_id,
                    _f9_prior_id,
                )
                await self._emit(
                    ObservationEvent(
                        tool_result=ToolResult(
                            call_id=action.tool_call.call_id,
                            tool_name=action.tool_call.tool_name,
                            success=True,
                            content=_f9_pointer,
                        ),
                        action_id=action.id,
                    )
                )
                return
        sbx = getattr(self.executor, "sandbox", None)
        gen_before = getattr(sbx, "generation", 0) if sbx is not None else 0
        try:
            result = await self.executor.execute(action.tool_call)
        except LLMContextWindowExceeded:
            raise  # handled by view-materialization hard-reset (§8)
        except Exception as e:  # noqa: BLE001 — any tool/exec failure is observable
            await self._emit(
                AgentErrorEvent(
                    error=str(e),
                    action_id=action.id,
                    tool_call_id=action.tool_call.call_id if action.tool_call else None,
                )
            )
            await self._maybe_emit_sandbox_restart(sbx, gen_before)
            return
        if result.success:
            await self._emit(ObservationEvent(tool_result=result, action_id=action.id))
            # C18 — advisory done-condition probe. If this was a
            # `plan_step(idx, 'done')` for a step that had a `done_condition`
            # attached, evaluate the predicate and emit a visible
            # pass/fail note. ADVISORY ONLY: a failure never blocks, never
            # nudges, never duplicates the C1c finish gate. Steps without
            # a predicate are an immediate no-op (back-compat).
            await self._maybe_emit_plan_step_done_condition_note(action)
        else:
            await self._emit(
                AgentErrorEvent(
                    error=result.error or "tool failed",
                    action_id=action.id,
                    tool_call_id=action.tool_call.call_id if action.tool_call else None,
                )
            )
        await self._maybe_emit_sandbox_restart(sbx, gen_before)

    async def _maybe_emit_sandbox_restart(self, sbx: object | None, gen_before: int) -> None:
        """If the sandbox's generation grew during the last call AND we already
        had a live instance (gen_before > 0), append an implicit system-reminder
        so the model knows its box was transparently restarted. Cheap, idempotent
        (zero-cost when no restart happened)."""
        if sbx is None or gen_before == 0:
            return
        gen_after = getattr(sbx, "generation", gen_before)
        if gen_after <= gen_before:
            return
        await self._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "Your sandbox container was restarted mid-session (the prior "
                        "container died and was transparently re-created). Files "
                        "previously written to /workspace remain; any background "
                        "processes or unsaved in-memory state are gone. If you relied "
                        "on running state, re-establish it before continuing.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )

    # ---- C20: subagent fan-out (delegate_explore) -------------------------

    async def _run_fanout(
        self, args: dict, events: list[Event], *, call_id: str = ""
    ) -> ToolResult:
        """C20 — dispatch the read-only Explore/Plan helper task and return the
        joined result as a `ToolResult` (the caller — the `delegate_explore`
        intercept path — folds it back as a paired `ObservationEvent`).

        The default implementation is a THIN, DETERMINISTIC STUB: it
        serializes the helper's question + context into a short structured
        report and returns it. This is a TEST SEAM — tests override
        `_run_fanout` on the loop instance to control the response. In a
        production wiring, the stub is replaced by a real LLM round-trip
        that offers the helper ONLY the read-only tools
        (file_read / file_list / search / extract) and folds the response
        back the same way. The shape of the return (a `ToolResult`) does
        not change between stub and production; the loop's
        observe-and-continue path is the same either way.

        Why a stub default (vs. a real LLM call here):
          * bounded test — no model required, no flakiness, no
            per-call latency, no cost (a real round-trip would burn
            tokens on every fan-out);
          * the BOUNDED cap + the dispatch+join shape are the load-
            bearing pieces for the C20 acceptance; the helper's
            INTERNAL logic is a separate concern;
          * the production hook is a one-method override; the test
            seam and the production hook are the same surface.

        Returns a `ToolResult(success=True, content=..., structured=...)`
        on a clean dispatch. The `call_id` echoes the loop's call_id so
        the resulting ObservationEvent stays properly paired with the
        proposed ActionEvent the loop just emitted (KV-cache stability,
        same discipline as the remember/serve intercept paths)."""

        # Import ToolResult locally to avoid a circular-import risk at
        # module-load time (the engine module is imported widely; keeping
        # the symbol scoped to the function is the conservative choice).
        from disco.core import ToolResult as _ToolResult

        # Length-bound the question + context (the engine truncates
        # BEFORE dispatching the seam, so a model that overrides
        # _run_fanout also sees a bounded input — the bound is a
        # property of the fan-out, not of this stub). Defensive: the
        # engine's interception path also length-bounds, so by the
        # time we get here the fields are already short; the defensive
        # bound here is a belt-and-suspenders for a direct override
        # that bypasses the engine's path (a unit test, a regression
        # case).
        question = str(args.get("question") or "").strip()
        context = str(args.get("context") or "").strip()
        # Defensive bound (the engine's path already length-bounds; this
        # is a belt-and-suspenders for a direct override).
        _trunc_marker = "\u2026[truncated]"
        if len(question) > _FANOUT_INPUT_MAX_CHARS:
            question = question[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)] + _trunc_marker
        if len(context) > _FANOUT_INPUT_MAX_CHARS:
            context = context[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)] + _trunc_marker
        # The stub's report: a structured, model-readable summary. In a
        # production wiring this is the helper's actual response; here
        # it is a deterministic echo so tests can assert on the shape
        # (and so a caller that does not override the seam still gets
        # a clean, honest "helper ran" report).
        return _ToolResult(
            call_id=call_id,  # echoes the proposed call's call_id (KV-cache pairing)
            tool_name="delegate_explore",
            success=True,
            content=(
                f"[C20 fan-out #{self._fanout_count}/{self._fanout_max}] "
                f"helper dispatched: "
                f"question=\"{question[:80]}{'…' if len(question) > 80 else ''}\""
                + (f" context={len(context)} chars" if context else "")
                + ". (Default stub — override `_run_fanout` for a real subagent.)"
            ),
            structured={
                "fanout_index": self._fanout_count,
                "fanout_max": self._fanout_max,
                "question_chars": len(question),
                "context_chars": len(context),
                "stub": True,
            },
        )

    # ---- C18: advisory plan-step done-condition ----------------------------

    async def _maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        """If `action` is a `plan_step(idx, 'done')` whose plan step has a
        stored `done_condition` predicate, evaluate it inline and emit a
        visible pass/fail note in the trace. ADVISORY ONLY:

          * never blocks the run;
          * never nudges the agent (no <system-reminder>, no auto-continue,
            no speak-back to the model);
          * never duplicates the C1c finish gate (the C18 check uses a
            lightweight inline evaluator; the C1c gate uses the heavy
            fresh-context DoDEvaluator and gates `finish` itself).

        Steps WITHOUT a predicate are a strict no-op (back-compat): no
        lookup, no note, no event. The check fires only on `state="done"`,
        never on `state="active"` (an active mark is the agent saying
        "I am starting" — there is no work to check yet)."""
        tc = action.tool_call
        if tc is None or tc.tool_name != "plan_step":
            return
        args = tc.arguments or {}
        state = str(args.get("state") or "")
        if state != "done":
            return
        # Find the latest plan + the step's (revision, index). The current
        # plan is whichever PlanEvent has the highest revision; a re-plan
        # supersedes, so we must use the LATEST (not just any) — the
        # `_plan_step_predicates` map is revision-scoped for exactly this
        # reason (a stale (revision, idx) must not match a fresh plan).
        try:
            idx = int(args.get("index"))
        except (TypeError, ValueError):
            return
        events = await self._events()
        latest_plan: PlanEvent | None = None
        for e in events:
            if isinstance(e, PlanEvent):
                if latest_plan is None or e.revision >= latest_plan.revision:
                    latest_plan = e
        if latest_plan is None:
            return  # no plan in scope — the plan_step is unanchored
        # 1-based step index. Out-of-range = nothing to look up.
        if idx < 1 or idx > len(latest_plan.steps):
            return
        predicate = self._plan_step_predicates.get((latest_plan.revision, idx))
        if predicate is None:
            # Back-compat: a step with no predicate is the explicit design
            # target (most steps). No note, no extra event. Today was
            # silent here and stays silent.
            return
        passed, reason = await self._evaluate_plan_step_predicate(predicate)
        # The note is a <system-reminder>-less MessageEvent from
        # ENVIRONMENT with source=ADVISORY semantics: it is visible in
        # the trace for the human and the LLM sees it on its next turn
        # as a normal message (NOT a system-reminder, so it does NOT
        # nudge — the model is free to ignore or act on it as it sees
        # fit). The "advisory" framing in the prefix is what makes the
        # no-nudge contract explicit in the trace.
        verdict_word = "met" if passed else "NOT met"
        body = (
            f"[advisory, C18] done-condition for plan step {idx} "
            f"(\"{latest_plan.steps[idx - 1].title}\"): {verdict_word}. "
            f"{reason}"
        )
        await self._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=body),
                meta={"advisory": "plan_step_done_condition", "passed": passed},
            )
        )

    async def _evaluate_plan_step_predicate(
        self, predicate: DoDPredicate
    ) -> tuple[bool, str]:
        """Lightweight inline evaluation of a single DoDPredicate for the
        C18 advisory note. The three kinds reuse the
        `disco.core.dod.DoDPredicate` discriminated union:

          * `file_exists(path)`: resolved against the executor's sandbox
            workspace root (so the predicate talks about agent-visible
            files, not harness-private paths), with a path-escape check
            mirroring the C1b evaluator's discipline (a `path` that
            resolves outside the workspace is a hard FAIL — the
            predicate is not silently passed by an out-of-scope match).
          * `command(cmd, expect_exit)`: a tight-timeout subprocess run
            against the workspace root. Deny-list failures and timeouts
            count as a non-pass with the reason surfaced.
          * `http_ok(url, expect_status)`: a short-timeout GET against
            the URL; a non-matching status is a non-pass.

        Returns `(passed, reason)`. The reason is a one-line human-
        readable summary the C18 note emits in the trace; on failure it
        names WHY the predicate did not pass (file missing, command
        exited N, http status 5xx, etc.) so the user can see the truth
        of the check, not just a boolean.

        Distinct from the C1c gate's `DoDEvaluator`: this is a single-
        predicate inline check that runs in the same context as the rest
        of the loop, not a fresh-context judge. The C1c gate is the
        authoritative DoD check at finish time; C18 is the
        per-step advisory trail."""
        if isinstance(predicate, FileExistsPredicate):
            return self._check_file_exists_for_plan_step(predicate)
        if isinstance(predicate, CommandExitPredicate):
            return await self._check_command_for_plan_step(predicate)
        if isinstance(predicate, HTTPOkPredicate):
            return await self._check_http_for_plan_step(predicate)
        # Defensive: the union is closed (three kinds) and
        # `predicate_from_obj` rejects unknown kinds at submit_plan
        # time. A future kind would land here as a fail-loud non-pass
        # rather than a silent pass.
        return (False, f"unsupported predicate kind: {type(predicate).__name__}")

    def _check_file_exists_for_plan_step(
        self, predicate: FileExistsPredicate
    ) -> tuple[bool, str]:
        """Resolve `predicate.path` against the executor's sandbox workspace
        root (when available) and check existence. The path-escape check
        mirrors the C1b evaluator's discipline: a `file_exists` whose
        resolved path lies outside the workspace is a hard FAIL — the
        predicate is not silently passed by a coincidental match on an
        out-of-scope file."""
        from pathlib import Path
        sbx = getattr(self.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        path_str = predicate.path
        if workspace:
            try:
                root = Path(workspace).resolve()
                candidate = (root / path_str).resolve()
                root_str = str(root)
                candidate_str = str(candidate)
                # On Windows the resolved strings may differ in case;
                # `Path.resolve()` is case-aware but the prefix check is
                # not, so do a normalized comparison. The agent runs
                # inside a Linux sandbox, so the suffix match is the
                # load-bearing case in practice.
                if not (
                    candidate_str == root_str
                    or candidate_str.startswith(root_str.rstrip("/") + "/")
                ):
                    return (
                        False,
                        f"file_exists: path escapes workspace ({path_str})",
                    )
                if candidate.exists():
                    return (True, f"file_exists({path_str}): found at {candidate}")
                return (False, f"file_exists({path_str}): missing (resolved {candidate})")
            except (OSError, ValueError) as exc:
                return (False, f"file_exists({path_str}): resolve error ({exc})")
        # No workspace_root: check the literal path. This is the
        # sandbox-less / fake-executor path used in tests; the result
        # is just as honest (the predicate names a literal path, we
        # check the literal path).
        p = Path(path_str)
        if p.exists():
            return (True, f"file_exists({path_str}): found")
        return (False, f"file_exists({path_str}): missing")

    async def _check_command_for_plan_step(
        self, predicate: CommandExitPredicate
    ) -> tuple[bool, str]:
        """Run `predicate.cmd` in a fresh subprocess against the workspace
        root and compare to `predicate.expect_exit` (default 0). Tight
        timeout to keep the loop responsive. Failures (deny, timeout,
        wrong exit) are surfaced with the reason in the note."""
        import asyncio
        import subprocess
        from pathlib import Path
        sbx = getattr(self.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        cwd = str(Path(workspace).resolve()) if workspace else None
        # 5s is plenty for a per-step done-condition probe — the C1c
        # gate uses the same default. C18 is advisory so we DON'T hang
        # the loop on a stuck command.
        timeout = 5.0

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                predicate.cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )

        started = asyncio.get_event_loop().time()
        try:
            completed = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return (False, f"command({predicate.cmd!r}): timeout after {timeout}s")
        except Exception as exc:  # noqa: BLE001 — defensive
            return (
                False,
                f"command({predicate.cmd!r}): executor error "
                f"({type(exc).__name__}: {exc})",
            )
        duration = asyncio.get_event_loop().time() - started
        if completed.returncode == predicate.expect_exit:
            return (
                True,
                f"command({predicate.cmd!r}): exited {completed.returncode} "
                f"as expected (in {duration:.2f}s)",
            )
        return (
            False,
            f"command({predicate.cmd!r}): exited {completed.returncode}, "
            f"expected {predicate.expect_exit}",
        )

    async def _check_http_for_plan_step(
        self, predicate: HTTPOkPredicate
    ) -> tuple[bool, str]:
        """GET `predicate.url` and compare to `predicate.expect_status`
        (default 200). Tight timeout; failures surface the reason. Does
        NOT enforce the C1b egress allow-list — this is a per-step
        advisory check the agent opted into by attaching the predicate;
        the C1c gate (fresh-context, with egress discipline) is the
        authoritative check."""
        try:
            import httpx
        except ImportError:
            # httpx is in disco-core's deps (we saw it in pyproject.toml),
            # but be defensive in case the test env differs.
            try:
                import urllib.request
                with urllib.request.urlopen(predicate.url, timeout=2.0) as resp:
                    status = int(resp.status)
            except Exception as exc:  # noqa: BLE001 — defensive
                return (False, f"http_ok({predicate.url}): error ({exc})")
        else:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(predicate.url)
                    status = int(resp.status_code)
            except Exception as exc:  # noqa: BLE001 — defensive
                return (False, f"http_ok({predicate.url}): error ({exc})")
        if status == predicate.expect_status:
            return (True, f"http_ok({predicate.url}): status {status} as expected")
        return (
            False,
            f"http_ok({predicate.url}): status {status}, "
            f"expected {predicate.expect_status}",
        )

    async def _finish_verify_passed(self, command: str) -> bool:
        """Run the agent's stated acceptance check before allowing `finish`
        (verify-on-finish post-condition gate). The agent attaches a shell
        command to finish whose exit 0 means the deliverable is good; we run it,
        VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
        the real problem instead of declaring a broken build complete.

        The verify command is NOT privileged: it passes the same hard-deny gate
        AND the same confirmation policy as any action. A command that would
        normally require confirmation is refused here (we don't silently run a
        gated command as a 'verification') — the agent is told to run it as an
        ordinary, gated action first. Ordinary test/build/lint checks assess as
        MEDIUM and run unimpeded. Returns True iff the check ran and passed."""
        call = ToolCall(tool_name="shell", arguments={"command": command})
        # meta marker: this shell action is the GATE'S probe, not the agent's
        # work. Phase-B re-run #6 (2026-06-10): an unmarked probe counted as a
        # real action in _actions_since_last_resume, so a refused first-move
        # finish UNLOCKED the withheld meta tools and the model remember-spammed
        # straight into the valve. The probe must never flip fresh-session.
        action = ActionEvent(
            thought=f"Verifying completion: {command}",
            tool_call=call,
            meta={"verify_probe": True},
        )

        deny = self._hard_deny_reason(action)
        if deny is not None:
            await self._emit(action)
            await self._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"The verify command attached to finish is hard-denied ({deny}); it "
                        "will not run. Provide a safe verify command, or finish without one.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        risk = self.analyzer.assess(action)
        if self.policy.should_confirm(risk):
            await self._emit(action)
            await self._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "The verify command attached to finish needs confirmation to run "
                        "and won't be executed silently as a verification. Run that check "
                        "as a normal action first (it will go through the confirm gate), "
                        "then finish.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        await self._emit(action)
        await self._execute_and_observe(action)
        # Find the observation correlated to THIS verify action (robust against a
        # trailing sandbox-restart notice that _execute_and_observe may append).
        events_after = await self._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        passed = isinstance(obs, ObservationEvent) and obs.tool_result.success
        # malformed = the verify COMMAND ITSELF is broken (not the deliverable):
        # command-not-found (127) or an interpreter SyntaxError. A non-zero exit from
        # an unrunnable check is NOT evidence the task failed — the carrier was bad.
        # The caller auto-strips a malformed verify rather than counting it as a
        # failed acceptance. Detected from the result text.
        malformed = False
        # Malformed = the verify CARRIER is broken, which only makes sense if the
        # shell actually RAN the command and reported it (an ObservationEvent). An
        # AgentErrorEvent means the executor raised BEFORE any observation (sandbox
        # down, transport error) — that's an environmental failure, NOT a malformed
        # verify, and must stay a real failure so it isn't auto-stripped into a false
        # "done". (Earlier this read AgentErrorEvent.error text and a stray "command
        # not found" substring there would wrongly strip an environmental failure.)
        if not passed and isinstance(obs, ObservationEvent):
            st = obs.tool_result.structured or {}
            ec = st.get("exit_code")
            exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
            low = f"{obs.tool_result.content or ''} {obs.tool_result.error or ''}".lower()
            # The shell couldn't find/parse the command: exit 127 (command-not-found,
            # authoritative from the structured result — locale-independent, can't be
            # faked by output text) or an interpreter SyntaxError. Use the real exit
            # code, NOT a regex over output: "exit 1, 127 tests failed" is a REAL
            # failure, not a malformed carrier, and must NOT become a false success.
            malformed = (
                exit_code == 127
                or "syntaxerror" in low
                # content fallbacks only when the structured exit code is unavailable
                or (exit_code is None and "command not found" in low)
                or (exit_code is None and ": not found" in low)
            )
        return passed, malformed

    # ---- C1c: external DoD evaluator gate on `finish` ----------------------
    # Wires the C1b fresh-context judge into the finish branch. Mirrors the
    # verify-on-finish gate's discipline: refuse-and-continue, never silent,
    # and BYTE-IDENTICAL to today's behavior when no DoD spec is set. See
    # `core/dod.py` for the immutability argument — the spec is write-once
    # and lives outside the agent's tool surface, so the predicates are not
    # the agent's own (they were captured at task start / plan approval).
    #
    # OSS prior-art: OpenHands' `_check_iterative_refinement` emits a followup
    # prompt when an external critic fails instead of finishing; this gate
    # does the same — emit a visible reminder, increment the refusal counter,
    # `continue` so the run keeps going. No cap: the loop's max_iterations
    # + the user's kill switch are the ultimate exits (the spec is
    # structural, not a forcing function with a release valve — a model that
    # genuinely cannot satisfy the predicates should be unblocked by the
    # user, not by a quiet auto-release).

    async def _finish_dod_gate_passed(self) -> bool:
        """C1c DoD gate. Returns True iff the finish should be allowed.

        Algorithm (in order):
          1. Read the DoD spec from the store. None → no spec → True
             (LEGACY BYTE-IDENTICAL PATH — no events emitted, no state
             changed, control flow identical to pre-C1c).
          2. Build a DoDEvaluator. Factory-injected if the loop was
             constructed with one; otherwise build a default over the
             executor's sandbox workspace_root (or skip the gate if the
             sandbox doesn't expose a path — defensive, never a crash).
          3. Run the verdict against the spec.
          4. Verdict passed → True (the finish lands).
          5. Verdict failed → emit a MessageEvent carrying the SPECIFIC
             unmet predicates (visible to the agent AND the audit), bump
             the refusal streak, return False so the caller `continue`s.

        Visible: the refusal is a `<system-reminder>` MessageEvent with the
        spec fingerprint, the number of unmet predicates, and a per-
        predicate line naming kind + reason. Never silent: a refused finish
        ALWAYS leaves a trace event. Same shape as verify-on-finish's
        refusal, distinct content (the predicates are external, not the
        agent's own command)."""
        spec = await self.store.get_dod_spec(self.conversation_id)
        if spec is None:
            # LEGACY: no DoD spec for this conversation → the gate is a
            # no-op. Today's finish path is reproduced EXACTLY — no events,
            # no state change, no log query beyond a single SELECT. The
            # read is observable as a side-effect-free DB query; it does
            # NOT change the events, status transitions, or final state.
            return True
        # Spec exists → run the evaluator. The factory seam is the test
        # injection point (fakes for command_runner / http_probe); the
        # default builds a real DoDEvaluator over the executor's workspace.
        try:
            evaluator = await self._build_dod_evaluator()
        except _DoDWorkspaceUnavailable:
            # No workspace to grade against. This is a misconfiguration
            # (the spec was set but the sandbox doesn't expose a path),
            # not a predicate failure. We log and skip the gate rather
            # than refusing forever — refusing without a reason would
            # also be a silent failure mode. The audit trail will see the
            # log line; the agent sees no gate, so the run can finish.
            _LOG.warning(
                "DoD spec set for %s but no workspace_root available; "
                "skipping C1c gate (refusing without evidence would be a "
                "silent fail).",
                self.conversation_id,
            )
            return True
        verdict = await evaluator.evaluate(spec, conversation_id=self.conversation_id)
        if verdict.passed:
            self._dod_refusals = 0  # clean pass → reset the streak (mirror verify)
            return True
        # Cap the refusal streak (mirror _FINISH_VERIFY_CAP): after N consecutive
        # DoD refusals, RELEASE the gate so an agent that cannot satisfy the
        # external DoD is not trapped in an unbounded refuse-and-continue loop
        # (that loop accumulates events without end — the OOM the uncapped first
        # cut caused). The release is logged LOUDLY; the prior refusal events
        # remain the visible audit trail of the unmet predicates.
        if self._dod_refusals >= _DOD_REFUSAL_CAP:
            _LOG.warning(
                "DoD for %s still unmet after %d refusals (cap %d) — releasing the "
                "finish gate to avoid an unbounded refuse loop.",
                self.conversation_id,
                self._dod_refusals,
                _DOD_REFUSAL_CAP,
            )
            return True
        # Refuse + keep working. The agent sees the SPECIFIC unmet
        # predicates (named by `kind` + the frozen-predicate `repr`); the
        # audit sees the spec fingerprint + the per-predicate results.
        self._dod_refusals += 1  # bounded by _DOD_REFUSAL_CAP (see above)
        unmet_lines: list[str] = []
        for result in verdict.results:
            if result.passed:
                continue
            # The frozen predicate's repr names kind + fields. Pair with
            # the verdict's reason (the human explanation).
            unmet_lines.append(f"  - {result.predicate!r}\n      reason: {result.reason}")
        unmet_block = "\n".join(unmet_lines) if unmet_lines else "  - (no per-predicate results)"
        await self._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You called finish, but the external Definition-of-Done "
                        f"evaluator found {len(verdict.unmet)} unmet acceptance "
                        f"predicate(s) (spec fingerprint {verdict.spec_fingerprint}):\n\n"
                        f"{unmet_block}\n\n"
                        "The task is NOT complete. These predicates were captured at "
                        "task start and live outside the agent's tool surface — you "
                        "cannot edit them, you can only satisfy them. Fix what they "
                        "surface (the predicates name the gap), then finish again.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return False

    async def _build_dod_evaluator(self) -> DoDEvaluator:
        """Construct the DoDEvaluator. Two paths:

          * `_dod_evaluator_factory` is set (test seam): call it, ignore args.
          * Otherwise: derive the workspace_root from the executor's sandbox
            (the in-cluster `workspace_path`); if absent, raise
            `_DoDWorkspaceUnavailable` and the gate degrades to "skip".

        The factory is the dependency-injection point — tests close over
        a tmp_path + fake command_runner / http_probe and return a fully
        configured `DoDEvaluator`. Production callers leave the factory
        None and the engine does the workspace resolution here.
        """
        if self._dod_evaluator_factory is not None:
            # The factory is an async-callable in the common case (tests
            # want to close over a `tmp_path` + fakes without performing
            # any I/O at construction time), but a sync callable is also
            # accepted — production callers may want to keep the
            # construction cheap. Awaiting a non-awaitable raises
            # TypeError, which the gate's `_DoDWorkspaceUnavailable`-
            # style `try/except` doesn't catch; the explicit
            # `inspect.iscoroutine` check keeps both shapes working.
            import inspect

            result = self._dod_evaluator_factory()
            if inspect.iscoroutine(result):
                result = await result
            return result
        sbx = getattr(self.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            raise _DoDWorkspaceUnavailable(
                f"no sandbox.workspace_path on executor {type(self.executor).__name__}"
            )
        from pathlib import Path
        return DoDEvaluator(Path(workspace))

    async def _stop_allowed(self, state: ConversationState, events: list[Event]) -> bool:
        for hook in self._stop_hooks:
            if not await hook.allow_stop(state, events):
                return False
        return True

    # ---- the run loop (§4) --------------------------------------------------

    async def _post_noop_valve(self) -> Disp:
        """Shared actionless-valve tail for the non-blocking virtual-tool arms
        (notify_user / remember / serve / delegate_explore / plan-nudge /
        execution-nudge / no-op / ask-fresh-session). Re-polls the event log,
        adds the invisible-step counter, and halts the run if the actionless
        valve trips. Byte-identical to the 4-line tail it replaces."""
        events = await self._events()
        noops = self._consecutive_noops(events) + self._invisible_steps
        if await self._actionless_valve(events, noops):
            return Disp.HALT
        return Disp.CONTINUE

    async def _gate_f4_bootstrap(self, events: list[Event]) -> list[Event]:
        # F4 — gated bootstrap observation. On the first model turn
        # of a session (no real actions yet, in execution mode) and
        # with assist=ON, emit a one-shot <system-reminder> listing
        # the build/test/entry commands detected from workspace
        # manifests (package.json, pyproject.toml, Makefile,
        # Cargo.toml, go.mod). The observation is gated THREE ways:
        #
        #   * self._assist — capable-model default keeps the path
        #     byte-identical to today (the function below is never
        #     called);
        #   * not self._bootstrap_emitted — fires EXACTLY once per
        #     conversation; a resume/steer is a clean slate for the
        #     agent's state but the model already saw the bootstrap
        #     in turn 1, so re-emitting it would be a redundant
        #     context cost;
        #   * _actions_since_last_resume(events) == 0 — turn-1
        #     detection. Same predicate `fresh_session` uses a few
        #     lines down. If the model has already taken a real
        #     action we are past turn 1 and the hint is no longer
        #     load-bearing.
        #
        # We re-poll events after the emit so the (d) view
        # materialization below includes the bootstrap on the
        # very first step. The detector itself is a pure function
        # over manifest files; it never raises, never blocks.
        if (
            self._assist
            and not self._bootstrap_emitted
            and self.mode != OperatingMode.PLANNING
            and self._actions_since_last_resume(events) == 0
        ):
            _sbx = getattr(self.executor, "sandbox", None)
            _workspace = (
                getattr(_sbx, "workspace_path", None) if _sbx is not None else None
            )
            _bootstrap = (
                _detect_project_bootstrap(_workspace) if _workspace else None
            )
            if _bootstrap:
                await self._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=_bootstrap),
                    )
                )
                events = await self._events()  # include the bootstrap in (d)'s view
            # Mark fired regardless of whether anything was detected
            # (an empty workspace is a valid one-time fact too — the
            # detector ran, there was nothing to surface, we don't
            # re-run on later turns).
            self._bootstrap_emitted = True
        return events

    async def _gate_stuck(self, events: list[Event]) -> Disp:
        # (c) stuck detection BEFORE more work (§6). ESCAPE-then-halt: a
        # repeating action→error loop first gets ONE reframe attempt (a strong
        # "stop repeating, try a different approach" reminder + a temperature
        # bump to break the self-imitation chain) BEFORE we declare STUCK. Only
        # if it repeats AGAIN after acting on the reframe do we halt — so a
        # transient rut doesn't dead-end a run the model could escape.
        escape_seq = self._stuck_escape_seq(events)
        acted_since_escape = escape_seq is not None and any(
            isinstance(e, ActionEvent) and e.seq is not None and e.seq > escape_seq
            for e in events
        )
        if self._stuck.is_stuck(self._recent(events)):
            if escape_seq is None:
                # First time in this user turn: drop a `stuck_escape` MARKER
                # (a status event) AND inject a C7 escape reminder (from the
                # rotating pool, with a per-attempt serialization nonce) so
                # the model's next step sees fresh anti-imitation text —
                # the harness-doesn't-nudge rule (c97c1b3) is preserved by
                # keeping the reminder INSIDE this escape branch (no
                # reminder outside the existing stuck-escape path). The
                # attempt count is the count of escape markers emitted
                # BEFORE this one (so attempt 0 → pool[0], attempt 1 →
                # pool[1], etc., rotating modulo len(pool) on later
                # escapes within the same conversation). The next step's
                # temperature is bumped (escape_temp below) — the reminder
                # is the in-context half, the temperature is the
                # sampling-variance half, and together they break the
                # self-imitation chain the way neither could alone.
                attempt = self._stuck_escape_attempt_count(events)
                await self._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=_stuck_escape_reminder(attempt),
                        ),
                    )
                )
                await self._emit(
                    StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape")
                )
                return Disp.CONTINUE
            if acted_since_escape:
                # The high-temp retry happened and it's STILL stuck → halt now.
                await self._emit(StatusEvent(status=ConversationStatus.STUCK))
                return Disp.HALT
            # else: escape just marked, model hasn't retried yet → fall through
            # and let it act this iteration (with the bumped temperature below).
        return Disp.FALLTHROUGH

    async def _gate_circuit_breaker(self, events: list[Event]) -> Disp:
        # (c.2) CIRCUIT BREAKER (Cluster 2). StuckDetector only catches
        # IDENTICAL action→error repeats; a model that tries N DIFFERENT
        # things that all fail would otherwise grind to max_iterations.
        # After `_circuit_breaker_threshold` consecutive failures, if the
        # model hasn't itself escalated (it would have halted at a gate
        # already), the HARNESS hands off to the user instead of grinding:
        # it halts at AWAITING_USER_DECISION with a summary of what failed.
        # The user's next message resets the streak (see _count_recent_failures).
        fails = self._count_recent_failures(events)
        recent_errors = [
            e.error for e in reversed(events) if isinstance(e, AgentErrorEvent)
        ][:fails]
        recovery_requested = self._recovery_requested_since_reset(events)
        if fails >= self._circuit_breaker_threshold and not recovery_requested:
            # D2 — FIRST time we hit the wall: don't dump a dead-end message.
            # Ask the agent to DIAGNOSE the failures and propose 2-3 CONCRETE
            # recovery options via `ask_user` (model-generated, runnable, from
            # its own failure context — not a canned list). The marker
            # (StatusEvent detail) guards against re-asking before it steps; the
            # NEXT iteration falls through so the agent actually proposes.
            errs = "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
            if self._autonomous:
                cb_content = (
                    "<system-reminder>\n"
                    f"You've failed {fails} times in a row:\n{errs}\n\n"
                    "STOP repeating the same approach — no human is available to "
                    "help. Diagnose the real blocker in one sentence, then take a "
                    "DIFFERENT technical path (a different library, API, command, or "
                    "algorithm). Narrate the pivot with `notify_user`. If it is "
                    "genuinely impossible, call `finish` and state clearly in the "
                    "summary what is blocked and why.\n"
                    "</system-reminder>"
                )
            else:
                cb_content = (
                    "<system-reminder>\n"
                    f"You've failed {fails} times in a row:\n{errs}\n\n"
                    "STOP retrying blindly. Call `ask_user` NOW with: a "
                    "1–2 sentence DIAGNOSIS of what is actually blocking you "
                    "as the `question`, and 2–3 concrete recovery `options`, "
                    "each a SPECIFIC tool action that CHANGES the approach "
                    "(not a repeat of what just failed). The user will pick "
                    "one — or let you continue.\n"
                    "</system-reminder>"
                )
            await self._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=cb_content),
                )
            )
            await self._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING, detail="recovery_requested"
                )
            )
            return Disp.CONTINUE
        if fails > self._circuit_breaker_threshold:
            if self._autonomous:
                # No human to hand off to → clean forfeit (STUCK), not an
                # indefinite AWAITING_USER_DECISION stall. Bounded: we only
                # reach here after the threshold + a failed recovery attempt,
                # so this is NOT an infinite-continue token burn. The failure
                # stays visible (STUCK + the error note) for a later human.
                await self._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                f"⚠ Autonomous run forfeited after {fails} consecutive "
                                "failures (recovery attempted, still failing). "
                                "Recent errors:\n"
                                + "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
                            ),
                        ),
                    )
                )
                await self._emit(StatusEvent(status=ConversationStatus.STUCK))
                return Disp.HALT
            # Recovery was already requested AND it failed again → hand off. The
            # model never volunteered clickable options, so the HARNESS now
            # SYNTHESIZES an AlternativesEvent (failure summary + the structural
            # "Continue anyway" / steer escapes) so the UI renders the recovery
            # GATE — not just dead-end prose. Picking continue resets the streak
            # (pick_alternative special-cases _CONTINUE_OPTION_ID); steering is
            # the manual escape. detail=the alt id so the View resolves the gate.
            summary = (
                f"I've hit {fails} failures in a row and couldn't find a way "
                "through. Pausing for your direction. The recent errors were:\n"
                + "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
            )
            failed_action_id = next(
                (e.id for e in reversed(events) if isinstance(e, ActionEvent)), ""
            )
            alt = AlternativesEvent(
                failed_action_id=failed_action_id,
                summary=summary,
                options=[
                    AlternativeOption(
                        id=_CONTINUE_OPTION_ID,
                        title="Continue anyway",
                        description=(
                            "Reset the failure streak and let the agent try another "
                            "approach with its own judgment."
                        ),
                        tool_name="",  # not a tool — the loop intercepts this id
                        arguments={},
                    )
                ],
            )
            await self._emit(alt)
            await self._emit(
                StatusEvent(
                    status=ConversationStatus.AWAITING_USER_DECISION,
                    detail=alt.id,
                )
            )
            return Disp.HALT
        return Disp.FALLTHROUGH

    async def run(self) -> ConversationState:
        """Drive until a terminal-for-now status. Idempotent to call again after
        a pause/confirmation. [CONTRACT] returns the resulting ConversationState."""
        # Fresh run segment → fresh invisible-step accounting (the counter only
        # measures spin WITHIN a segment; a resume/steer is a clean slate).
        self._invisible_steps = 0
        self._finish_verify_refusals = 0  # fresh segment → fresh verify-cap streak
        self._finish_verify_strips = 0
        # C1c — fresh segment → fresh DoD-refusal streak (telemetry; the gate
        # has no cap, but a resume/steer should not carry a streak across).
        self._dod_refusals = 0
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
        # Restore execution mode after a server restart. `self.mode` is in-memory only;
        # a fresh loop always starts in PLANNING mode regardless of what the event log
        # says. Scan the event log to determine whether the current lifecycle is in
        # execution mode (plan was approved and not subsequently re-entered planning).
        # This fixes post-restart resumes: without it, finish triggers PLAN_NUDGE.
        if self.mode == OperatingMode.PLANNING and self._execution_mode != OperatingMode.PLANNING:
            _boot_events = await self._events()
            _plan_approved_seq: int | None = None
            _reenter_planning_seq: int | None = None
            for _e in _boot_events:
                if isinstance(_e, StatusEvent):
                    if _e.detail == "plan_approved":
                        _plan_approved_seq = _e.seq or 0
                    elif _e.detail == "planning":  # request_plan re-enters planning mode
                        _reenter_planning_seq = _e.seq or 0
            # In execution mode if: plan approved AND not re-entered planning after that.
            if _plan_approved_seq is not None and (
                _reenter_planning_seq is None
                or _plan_approved_seq > _reenter_planning_seq
            ):
                self.mode = self._execution_mode

        await self._emit(StatusEvent(status=ConversationStatus.RUNNING))

        while True:
            action_to_execute: ActionEvent | None = None
            async with self._lock:
                events = await self._events()
                state = ConversationState.reconstruct(
                    self.conversation_id, events, max_iterations=self.max_iterations
                )
                status = state.execution_status

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
                await self._drain_recovered_memory_facts()

                events = await self._gate_f4_bootstrap(events)

                # HS-03 — scheduled facts re-grounding (assist-tier only).
                # On the post-resume one-shot AND on a cadence boundary
                # (every _HS03_REGROUND_INTERVAL actions since the last
                # resume), emit a short recap of the stable facts (goal,
                # plan state, recently-touched files, optional constraints
                # from the planner's exploration context). Recap ONLY —
                # no imperative/steer text (commit c97c1b3's
                # no-automatic-nudge invariant). Three closures:
                #   * self._assist — capable-model default keeps the
                #     path closed end-to-end (the helper is never
                #     called; the predicate short-circuits at the top
                #     of `_should_emit_reground`);
                #   * the per-boundary guards
                #     (_hs03_reground_post_resume_emitted and
                #     _hs03_reground_last_action_count) — fire EXACTLY
                #     once per resume (the post-resume one-shot) and
                #     at most once per cadence boundary (the
                #     per-boundary action-count guard), so a model
                #     that is between actions at the same boundary
                #     doesn't see two consecutive recaps;
                #   * the action count is
                #     `_actions_since_last_resume(events)`, so the
                #     cadence restarts on every resume/restart (a
                #     long-lived conversation gets a fresh re-ground
                #     budget at the start of every run segment).
                # Re-poll events after the emit so the materialize step
                # below sees the recap in this turn's View (F4's
                # idiom). The gate runs BEFORE stuck-detection so a
                # re-ground + a stuck-escape can both fire on the same
                # turn (they don't conflict; the re-ground is a recap
                # of facts, the stuck-escape is the C7 anti-imitation
                # reminder — distinct purposes).
                events = await self._maybe_emit_reground(events)

                disp = await self._gate_stuck(events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                disp = await self._gate_circuit_breaker(events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

                # (c.5) SOFT plan-step nudge — the auditor. When substantial work
                # has happened but the capstone tracker is lagging (the "did the
                # work, forgot to check it off" failure), inject ONE gentle
                # reminder so the model keeps the tracker honest. NOT a gate —
                # the model is free to ignore it; it fires at most once per lag
                # episode. This is the proactive nudge (vs. the finish-boundary
                # auto-continue which catches the same thing at the end).
                if self._plan_step_lag_signal(events):
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    "<system-reminder>\n"
                                    "Gentle note: you've done a fair amount of work but "
                                    "the plan-step tracker is behind — most steps aren't "
                                    "marked done yet. If any completed steps are done, "
                                    "mark them with plan_step(idx, 'done') so the user can "
                                    "see real progress. No need to stop what you're doing; "
                                    "just keep the tracker in sync as you go.\n"
                                    "</system-reminder>"
                                ),
                            ),
                        )
                    )
                    events = await self._events()  # include the nudge in this step's View

                # (c.3) plan_step-spam guard (issue C). A soft nudge once at the
                # streak threshold; a hard STUCK halt at the cap (the model is doing
                # nothing but shuffling the plan tracker — every other valve misses
                # this). STUCK (not a silent proceed) keeps the failure VISIBLE, which
                # matters most for weak local models. Autonomous mode turns STUCK into
                # a clean forfeit (issue A).
                _bk_streak = self._bookkeeping_streak_len(events)
                if _bk_streak == _BOOKKEEPING_STREAK_NUDGE_AT:
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    "<system-reminder>\n"
                                    f"You've called plan-tracking tools {_bk_streak} times "
                                    "in a row without doing any real work (no file edit, "
                                    "shell command, or other state-changing action). Marking "
                                    "steps does NOT advance the task. Take a REAL action now "
                                    "to make progress, or call `finish` if the work is "
                                    "already complete.\n"
                                    "</system-reminder>"
                                ),
                            ),
                        )
                    )
                    events = await self._events()
                else:
                    # Spam cap scales with the active plan's step count (T7 / E3):
                    # a model finishing an N-step plan may legitimately emit
                    # ~N `plan_step` calls (one per step) plus a couple of
                    # over-corrections. Capping at N + slack lets the legit
                    # burst complete; the original `_BOOKKEEPING_STREAK_HALT_AT`
                    # floor still catches spam on a small/no-plan run.
                    _plan_steps = self._active_plan_step_count(events)
                    _bk_halt_cap = max(
                        _BOOKKEEPING_STREAK_HALT_AT,
                        _plan_steps + _BOOKKEEPING_PLAN_SLACK,
                    )
                    if _bk_streak >= _bk_halt_cap:
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(
                                    role="user",
                                    content=(
                                        "⚠ Stopped: the agent kept updating the plan checklist "
                                        "without doing any real work. Re-run or steer it toward a "
                                        "concrete action."
                                    ),
                                ),
                            )
                        )
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.STUCK, detail="bookkeeping_only"
                            )
                        )
                        return await self.get_state()

                # (d) build the model-facing View, condensing if triggered (§8)
                view = await self._materialize_view(events)

                # (e) ask the agent for ONE action (principle 1). The visible tool
                # set is mode-scoped: while PLANNING the agent sees ONLY the plan
                # tool (so it can't act before approval); while executing it sees
                # everything except the plan tool.
                # Escape temperature: jitter HARD to break a self-imitation chain —
                # the single step right after a stuck reframe (StuckDetector's escape).
                # escape_seq/acted_since_escape are pure functions of `events`
                # (the stuck gate recomputes its own copy); recompute here for the
                # temperature decision (folds into `_drive_step` on extraction).
                escape_seq = self._stuck_escape_seq(events)
                acted_since_escape = escape_seq is not None and any(
                    isinstance(e, ActionEvent) and e.seq is not None and e.seq > escape_seq
                    for e in events
                )
                in_escape = escape_seq is not None and not acted_since_escape
                escape_temp = _STUCK_ESCAPE_TEMP if in_escape else None
                # Withhold the meta/handoff virtuals until this session's first
                # real action (see _tools_for_step docstring — Phase-B re-run #4).
                fresh_session = (
                    self.mode != OperatingMode.PLANNING
                    and self._actions_since_last_resume(events) == 0
                )
                try:
                    attempts = 0
                    requery_count = 0
                    transient_messages: list[LLMMessage] = []
                    while True:
                        try:
                            # Apply transient messages (requery-outside-log, Rung 6)
                            # to the View if we're in a retry loop.
                            current_view = view
                            if transient_messages:
                                current_view = view.model_copy(update={
                                    "messages": view.messages + transient_messages
                                })

                            step = await self.agent.step(
                                current_view,
                                self._tools_for_step(suppress_meta_tools=fresh_session),
                                mode=self.mode,
                                overflow_signal=self._overflow_signal(events),
                                on_stream=self._build_stream_hook(),
                                temperature=escape_temp,
                                assist=self._assist,
                                # F5: thread the repair-attempt counter (1 = first
                                # try; incremented on transient-retry / requery).
                                # The OpenAI provider reads `req.attempt >= 2` to
                                # force `enable_thinking=False`. assist-OFF is
                                # untouched (the provider's gate ignores the field
                                # when req.assist is False).
                                attempt=attempts + 1,
                            )

                            # Rung 7: Invalid-tool reroute (weak-model FC kit).
                            # Valid JSON but unknown tool name -> if we haven't
                            # hit the requery bound, inject a hint and retry
                            # without persisting the failure to the store.
                            # The requery applies ONLY to names absent from the
                            # FULL tool registry (truly unknown), never to
                            # known-but-currently-withheld tools.
                            _cn = getattr(self.executor, "callable_tool_names", None)
                            if callable(_cn):
                                known_tool_names = set(_cn())
                            else:
                                known_tool_names = {t.name for t in self.executor.available_tools()}
                            virtual_names = {
                                "ask_user",
                                "clarify",
                                "propose_plan_update",
                                "plan_step",
                                "notify_user",
                                "finish",
                                "remember",
                                "serve",
                                self._plan_tool,
                                # C20 — `delegate_explore`: read-only
                                # Explore/Plan helper. Listed in the
                                # known-names set so the Rung 7 requery
                                # doesn't bounce a valid fan-out call.
                                # The cap is enforced at the call site,
                                # NOT via schema suppression.
                                "delegate_explore",
                            }
                            # Include mode-scoped virtuals (planning tools) so
                            # we don't requery for valid exploration turns.
                            all_known_names = (
                                known_tool_names
                                | virtual_names
                                | set(self._planning_tools)
                            )

                            if step.tool_call and step.tool_call.tool_name not in all_known_names:
                                # A tool whose NAME alone trips the confirm policy's
                                # security gate (publish/deploy/release — it leaves the
                                # blast radius) must NOT be bounced back to the model by
                                # the unknown-tool requery: an unregistered publish-class
                                # name is a real publish intent that has to reach the
                                # human confirm gate, not a hallucination to retry.
                                # Without this the requery swallowed `deploy_site` before
                                # BlastRadiusConfirm's publish guard could pause for
                                # confirmation — then the plan read "done" with nothing
                                # executed and the execution-finish gate spun forever
                                # (the confirm/reject livelock root cause).
                                _gbn = getattr(self.policy, "gates_by_name", None)
                                _name_gated = callable(_gbn) and _gbn(
                                    step.tool_call.tool_name
                                )
                                if not _name_gated and requery_count < 2:
                                    requery_count += 1
                                    _LOG.info(
                                        f"Unknown tool {step.tool_call.tool_name}, requerying..."
                                    )
                                    offered_tools = self._tools_for_step(
                                        suppress_meta_tools=fresh_session
                                    )
                                    offered_names = {t.name for t in offered_tools}
                                    # Mirror the assistant's turn so the next call's
                                    # messages list stays balanced for pairing.
                                    transient_messages.append(
                                        LLMMessage(
                                            role="assistant",
                                            content=step.thought,
                                            tool_calls=[
                                                {
                                                    "id": step.tool_call.call_id,
                                                    "name": step.tool_call.tool_name,
                                                    "arguments": step.tool_call.arguments,
                                                }
                                            ],
                                        )
                                    )
                                    # F2 / T9 (assist-gated): when self._assist is on, append
                                    # a "did you mean <name>?" suggestion computed by
                                    # Levenshtein distance over the offered tool names.
                                    # The existing Rung-7 hint and requery bound (cap=2)
                                    # are reused — no new reroute path, no new cap.
                                    # Assist OFF (capable-model default) leaves the hint
                                    # byte-identical to today.
                                    _hint = (
                                        f"ERROR: Unknown tool '{step.tool_call.tool_name}'. "
                                        f"Available: {sorted(list(offered_names))}"
                                    )
                                    if self._assist:
                                        _suggestion = _nearest_tool_name(
                                            step.tool_call.tool_name, offered_names
                                        )
                                        if _suggestion is not None:
                                            _hint = f"{_hint} did you mean '{_suggestion}'?"
                                    transient_messages.append(
                                        LLMMessage(
                                            role="user",
                                            content=_hint,
                                        )
                                    )
                                    continue

                            break  # Step is valid or requeries exhausted
                        except LLMContextWindowExceeded:
                            raise  # handled by view-materialization hard-reset (§8)
                        except LLMTransientError:
                            if attempts < len(_DRIVER_RETRY_BACKOFFS_S):
                                await _sleep(_DRIVER_RETRY_BACKOFFS_S[attempts])
                                attempts += 1
                                continue
                            else:
                                await self._emit(
                                    MessageEvent(
                                        source=EventSource.ENVIRONMENT,
                                        message=LLMMessage(
                                            role="user",
                                            content=(
                                                "model driver unavailable — conversation"
                                                " paused, resume when the model is back"
                                            ),
                                        ),
                                    )
                                )
                                await self._emit(
                                    StatusEvent(
                                        status=ConversationStatus.PAUSED,
                                        detail="driver-unavailable",
                                    )
                                )
                                return await self.get_state()
                        except LLMError as e:
                            # DEFECT-6: Provider 4xx "rejected request" must not be
                            # terminal; enter requery path with a hint.
                            if requery_count < 2:
                                requery_count += 1
                                _LOG.warning(f"Provider rejected request: {e}, requerying...")
                                transient_messages.append(LLMMessage(
                                    role="user",
                                    content=(
                                        f"The provider rejected the previous request: {e}. "
                                        "Please adjust your response (check tool names, "
                                        "JSON structure, or parameters) and try again."
                                    )
                                ))
                                continue
                            raise
                except LLMContextWindowExceeded:
                    if await self._hard_reset(await self._events()):
                        continue  # retry the step on the condensed view
                    await self._emit(
                        ErrorEvent(code="context_window", detail="hard reset made no progress")
                    )
                    return await self.get_state()
                except LLMError as e:
                    # REACTIVE error surfacing: the driver model's call failed (the
                    # provider rejected the input, refused, auth/transient exhausted,
                    # or the assignment was bad). Do NOT swallow it or flatten it into
                    # a generic failure — surface the provider's real content to the
                    # UI via ErrorEvent.detail (the agent server streams every event
                    # to the client). This is conversation-fatal: the brain itself
                    # failed, so there is no observation to feed back. The typed
                    # classification (the exception class) is preserved in the detail.
                    await self._emit(ErrorEvent(code="model_error", detail=_describe_llm_error(e)))
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
                    msg = str(step.tool_call.arguments.get("message") or "").strip() or step.thought
                    if msg.strip():
                        await self._emit(
                            MessageEvent(
                                source=EventSource.AGENT,
                                message=LLMMessage(role="assistant", content=msg),
                            )
                        )
                    else:
                        self._invisible_steps += 1
                    # Non-blocking, but NOT exempt from the actionless valve —
                    # a bare `continue` here let prose spam bypass every cap
                    # (Phase-B re-run, 2026-06-10).
                    if await self._post_noop_valve() is Disp.HALT:
                        return await self.get_state()
                    continue  # non-blocking — keep working
                if step.tool_call is not None and step.tool_call.tool_name == "remember":
                    # Durable memory: emit a PINNED KnowledgeEvent so the fact
                    # survives condensation and is re-injected into context every
                    # step. Non-blocking — like notify_user, the agent keeps
                    # working right after. A blank fact is ignored (no-op).
                    #
                    # FRESH-SESSION BACKSTOP (Phase-B re-run #6, 2026-06-11):
                    # remember is withheld from the offered set until the
                    # session's first real action, but a weak model can
                    # hallucinate calls to unoffered tools — re-run #6's model
                    # remember-spammed duplicate CSV facts right after its
                    # first-move finish was refused. Unlike notify_user (which
                    # degrades into the bounded prose channel), an executed
                    # remember POLLUTES pinned knowledge and reads as success,
                    # so the model keeps picking it. Refuse with actionable
                    # feedback: pinned facts must come from THIS session's work.
                    if (
                        self.mode != OperatingMode.PLANNING
                        and self._actions_since_last_resume(events) == 0
                    ):
                        self._invisible_steps += 1
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(
                                    role="user",
                                    content=(
                                        "remember refused: no real work has happened "
                                        "yet in this session. Facts worth pinning come "
                                        "from real observations — execute the next plan "
                                        "step with real tool calls first, then remember "
                                        "what you learned."
                                    ),
                                ),
                            )
                        )
                        if await self._post_noop_valve() is Disp.HALT:
                            return await self.get_state()
                        continue  # non-blocking — let the model act on the feedback
                    fact = str(step.tool_call.arguments.get("fact") or "").strip()
                    if fact:
                        import hashlib
                        scope = str(step.tool_call.arguments.get("scope") or "").strip()
                        normalized = fact
                        fact_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                        seen = {
                            (e.scope, hashlib.sha256(e.snippet.strip().encode("utf-8")).hexdigest())
                            for e in events if isinstance(e, KnowledgeEvent)
                        }
                        if (scope, fact_hash) in seen:
                            action = ActionEvent(
                                thought=step.thought,
                                tool_call=step.tool_call,
                                self_assessed_risk=step.self_assessed_risk,
                                llm_response_id=step.llm_response_id,
                            )
                            res = ToolResult(
                                call_id=step.tool_call.call_id,
                                tool_name="remember",
                                success=True,
                                content="Already recorded — not stored again.",
                            )
                            await self._emit(action)
                            await self._emit(ObservationEvent(tool_result=res, action_id=action.id))
                        else:
                            await self._emit(
                                KnowledgeEvent(source=EventSource.AGENT, scope=scope, snippet=fact)
                            )
                            # C5 — write-through mirror: persist the fact to
                            # `<workspace>/.pmx/MEMORY.md` so the standing
                            # memory survives a hard filesystem reset (a
                            # box wipe / fresh backend). The in-View
                            # KnowledgeEvent channel remains the
                            # authoritative in-session source — `.pmx/`
                            # is a write-through mirror, not a divergent
                            # second store. Best-effort: a sandbox write
                            # failure is logged but never blocks the
                            # in-View emission (the agent still has the
                            # fact in-context for THIS run).
                            try:
                                await self._write_pmx_memory_fact(scope, fact)
                            except Exception:  # noqa: BLE001 — mirror is best-effort
                                import logging as _logging
                                _logging.getLogger(__name__).warning(
                                    "pmx MEMORY write-through failed (in-View fact survives)",
                                    exc_info=True,
                                )
                    else:
                        # Blank fact persists NOTHING — count it or it's an
                        # unbounded silent token burn.
                        self._invisible_steps += 1
                    if await self._post_noop_valve() is Disp.HALT:
                        return await self.get_state()
                    continue  # non-blocking — keep working
                if step.tool_call is not None and step.tool_call.tool_name == "serve":
                    # Finished-artifact HANDOFF: emit a DeliverableEvent the UI renders
                    # as Open-the-app / Download-the-files. Non-blocking — the agent
                    # serves, verifies, then finishes. A missing path/title or a
                    # re-serve of an already-handed-off artifact is ignored (no-op)
                    # rather than emitting a useless handoff — and every ignored
                    # form is COUNTED, because each is invisible in the event log
                    # and was the unbounded serve-spam vector (Phase-B, 2026-06-10).
                    #
                    # POST-RESUME SERVE GATE (Phase-B re-run #3, 2026-06-10): the
                    # model's FIRST post-resume turn was serve(path=".") on an empty
                    # restored workspace — a handoff with zero work behind it, which
                    # then seeded a prose/noop streak into the valve. A serve is only
                    # meaningful after at least one real action this session (since
                    # the last resume, or since start). Refuse with ACTIONABLE
                    # feedback (B4: the model must see why, or it just retries).
                    if self._actions_since_last_resume(events) == 0:
                        self._invisible_steps += 1
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(
                                    role="user",
                                    content=(
                                        "serve refused: no real work has happened yet in "
                                        "this session — the sandbox is fresh and nothing "
                                        "is running. Execute the next plan step with real "
                                        "tool calls (write files, run commands, start your "
                                        "server), then serve the result."
                                    ),
                                ),
                            )
                        )
                        if await self._post_noop_valve() is Disp.HALT:
                            return await self.get_state()
                        continue  # non-blocking — let the model act on the feedback
                    if not step.tool_call.arguments:
                        _LOG.debug("Skipping deliverable emission: empty payload from agent")
                        self._invisible_steps += 1
                    else:
                        title = str(step.tool_call.arguments.get("title") or "").strip()
                        path = str(step.tool_call.arguments.get("path") or "").strip()
                        kind = str(step.tool_call.arguments.get("kind") or "app").strip()
                        url = str(step.tool_call.arguments.get("url") or "").strip()
                        if kind not in ("app", "files"):
                            kind = "app"
                        if not (title and path):
                            self._invisible_steps += 1
                        elif any(
                            isinstance(e, DeliverableEvent)
                            and e.path == path
                            and e.artifact_kind == kind
                            for e in events
                        ):
                            # Same artifact already handed off — an identical
                            # card adds nothing for the user; re-emitting it is
                            # the few-shot spam prompt for the next one.
                            _LOG.debug("Skipping duplicate deliverable: %s (%s)", path, kind)
                            self._invisible_steps += 1
                        else:
                            await self._emit(
                                DeliverableEvent(
                                    source=EventSource.AGENT,
                                    title=title,
                                    path=path,
                                    artifact_kind=kind,  # type: ignore[arg-type]
                                    deployment_url=url,
                                )
                            )
                    if await self._post_noop_valve() is Disp.HALT:
                        return await self.get_state()
                    continue  # non-blocking — keep working
                # C20 — `delegate_explore`: a bounded, read-only Explore/Plan
                # helper the loop dispatches+joins. The driver calls it; the
                # loop:
                #   1. enforces the per-run-segment cap (_FANOUT_MAX_PER_RUN;
                #      past the cap → refuse with a system-reminder, no
                #      dispatch, no observation);
                #   2. emits the ActionEvent (so the audit trail sees the
                #      proposed call);
                #   3. dispatches the subagent (default: a thin deterministic
                #      stub — the test seam; production wires a real LLM
                #      round-trip with read-only tools only — see
                #      `_run_fanout` for the override hook);
                #   4. folds the subagent's response back as a paired
                #      ObservationEvent (success=True on a clean dispatch,
                #      success=False with error="cap_exceeded" on refusal)
                #      so the driver sees the result on its next turn.
                # The helper is READ-ONLY: a subagent cannot mutate
                # workspace state, cannot run shell, cannot write files
                # (enforced upstream by the tools the helper is offered —
                # file_read/file_list/search/extract; the engine also
                # cannot recurse through `delegate_explore` because the
                # cap applies to nested calls too). NON-BLOCKING: the
                # driver keeps working right after — the same shape as
                # notify_user/remember/serve (the actionless valve still
                # applies if a fan-out produces no real work).
                if (
                    step.tool_call is not None
                    and step.tool_call.tool_name == "delegate_explore"
                ):
                    if self._fanout_count >= self._fanout_max:
                        # Cap exceeded — refuse with feedback. The cap is
                        # per-run-segment, so a fresh `run()` resets it.
                        # We do NOT raise / halt / STUCK (this is a soft
                        # "no more fan-outs this segment" gate, not a
                        # stuck-detector); we emit a system-reminder +
                        # ActionEvent + AgentErrorEvent (paired by
                        # call_id) so the driver sees the refusal on its
                        # next turn and falls back to direct tools. The
                        # refusal is invisible to the actionless valve
                        # (an error-paired action doesn't extend the
                        # noop streak).
                        action = ActionEvent(
                            thought=step.thought,
                            tool_call=step.tool_call,
                            self_assessed_risk=step.self_assessed_risk,
                            llm_response_id=step.llm_response_id,
                        )
                        await self._emit(action)
                        await self._emit(
                            AgentErrorEvent(
                                error=(
                                    "<system-reminder>\n"
                                    f"delegate_explore refused: the per-run-segment "
                                    f"cap ({self._fanout_max}) has been reached "
                                    f"(used {self._fanout_count}/{self._fanout_max} "
                                    f"this segment). Fall back to direct read-only "
                                    f"tools (file_read, file_list, search, extract) "
                                    f"for the rest of this run segment; a fresh run "
                                    f"segment resets the budget.\n"
                                    "</system-reminder>"
                                ),
                                action_id=action.id,
                                tool_call_id=(
                                    action.tool_call.call_id if action.tool_call else None
                                ),
                            )
                        )
                        continue  # non-blocking — let the model adapt
                    # Under the cap → record the proposed action, dispatch
                    # the helper, and fold the result back. The dispatch
                    # is `await`ed so the ActionEvent and ObservationEvent
                    # land in the same turn (the driver sees both on its
                    # next step).
                    self._fanout_count += 1
                    action = ActionEvent(
                        thought=step.thought,
                        tool_call=step.tool_call,
                        self_assessed_risk=step.self_assessed_risk,
                        llm_response_id=step.llm_response_id,
                    )
                    await self._emit(action)
                    # Length-bound the helper's input BEFORE dispatching —
                    # the bound is a property of the fan-out (regardless
                    # of what `_run_fanout` does — a test seam, a real
                    # LLM round-trip, a future override). Without this
                    # bound a driver could grow the helper's prompt
                    # unboundedly within a single segment; the result
                    # is folded back into the View, which the condenser
                    # would later have to manage.
                    _fanout_args = dict(action.tool_call.arguments or {})
                    _trunc_marker = "\u2026[truncated]"
                    for _k in ("question", "context"):
                        _v = str(_fanout_args.get(_k) or "")
                        if len(_v) > _FANOUT_INPUT_MAX_CHARS:
                            _fanout_args[_k] = (
                                _v[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)]
                                + _trunc_marker
                            )
                    result = await self._run_fanout(
                        _fanout_args,
                        events,
                        call_id=(
                            action.tool_call.call_id if action.tool_call else ""
                        ),
                    )
                    await self._emit(
                        ObservationEvent(tool_result=result, action_id=action.id)
                    )
                    # Fan-out is non-blocking — the driver keeps working
                    # right after. The actionless valve still applies if
                    # the helper returned empty (a degenerate fan-out is
                    # still a no-op step, like remember/serve).
                    if await self._post_noop_valve() is Disp.HALT:
                        return await self.get_state()
                    continue  # non-blocking — keep working
                if step.tool_call is not None and step.tool_call.tool_name == "finish":
                    # VERIFY-ON-FINISH (post-condition gate). If the agent attached
                    # a `verify` check to finish, RUN it first and refuse the finish
                    # if it doesn't pass — the "run the tests before you claim done"
                    # forcing function. The check is visible in the trace; on
                    # failure the agent sees exactly what broke and adapts, instead
                    # of declaring a broken build complete.
                    verify_cmd = str(step.tool_call.arguments.get("verify") or "").strip()
                    # E4: a `static` directive verifies a static page WITHOUT a server
                    # (files present + HTML parses) — the honest check for a page build.
                    if verify_cmd == _STATIC_VERIFY_PREFIX or verify_cmd.startswith(
                        _STATIC_VERIFY_PREFIX + ":"
                    ):
                        _, _, _path = verify_cmd.partition(":")
                        verify_cmd = _static_verify_command(_path)
                    # app / app:<url> — verify the RUNNING deliverable actually serves
                    # (HTTP 200 + non-trivial body), not just that a file exists.
                    elif verify_cmd == _APP_VERIFY_PREFIX or verify_cmd.startswith(
                        _APP_VERIFY_PREFIX + ":"
                    ):
                        _, _, _url = verify_cmd.partition(":")
                        verify_cmd = _app_verify_command(_url)
                    if verify_cmd:
                        passed, malformed = await self._finish_verify_passed(verify_cmd)
                        if passed:
                            self._finish_verify_refusals = 0  # reset streak on clean pass
                        elif malformed and self._finish_verify_strips < 3:
                            # Broken CHECK, not a failed task → auto-strip and finish.
                            self._finish_verify_strips += 1
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(
                                        role="user",
                                        content=(
                                            "<system-reminder>\n"
                                            f"Your verify command `{verify_cmd}` is not runnable "
                                            "(syntax error / command-not-found) — that is a broken "
                                            "CHECK, not a failed task, so it is "
                                            "being ignored and the "
                                            "run is finishing. Next time pass a "
                                            "valid shell command "
                                            "if you want real verification.\n"
                                            "</system-reminder>"
                                        ),
                                    ),
                                )
                            )
                            # fall through to finish
                        elif self._finish_verify_refusals < _FINISH_VERIFY_CAP:
                            # Real failure: refuse + keep working (the forcing function).
                            self._finish_verify_refusals += 1
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(
                                        role="user",
                                        content=(
                                            "<system-reminder>\n"
                                            f"You called finish, but the verify "
                                            f"command `{verify_cmd}` "
                                            "did not pass (see the result above). The task is NOT "
                                            "complete. Fix what it surfaced, then "
                                            "finish again — or "
                                            "finish without a verify command if "
                                            "the check itself is "
                                            "wrong.\n"
                                            "</system-reminder>"
                                        ),
                                    ),
                                )
                            )
                            continue  # verification failed — keep working
                        else:
                            # Cap reached: LOUD release — don't grind forever on a gate the
                            # model can't satisfy (mirrors the browser-verify valve). The
                            # failure stays visible (status detail + reminder + summary).
                            await self._emit(
                                StatusEvent(
                                    status=ConversationStatus.RUNNING,
                                    detail="finish_verify_release",
                                )
                            )
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(
                                        role="user",
                                        content=(
                                            "<system-reminder>\n"
                                            f"The verify command `{verify_cmd}` has failed "
                                            f"{self._finish_verify_refusals} times. "
                                            "Finishing anyway "
                                            "so the run does not loop forever — "
                                            "but the deliverable "
                                            "may be incomplete. Note this clearly "
                                            "in your summary.\n"
                                            "</system-reminder>"
                                        ),
                                    ),
                                )
                            )
                            # ⚠ human-facing: surfaces in the UI as a warning chip so the
                            # user knows the run finished with a failing check.
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(
                                        role="user",
                                        content=(
                                            "⚠ Finished despite the verification check failing "
                                            f"{self._finish_verify_refusals}× — "
                                            "the deliverable may "
                                            "be incomplete; review it."
                                        ),
                                    ),
                                )
                            )
                            self._finish_verify_refusals = 0
                            # fall through to finish
                    # C1c — external DoD evaluator gate. Runs AFTER the
                    # agent's own verify check (which grades the agent's
                    # own command) but BEFORE the step is committed to a
                    # FINISHED status. The spec is captured at task start
                    # and lives outside the agent's tool surface, so the
                    # predicates are NOT the agent's own — they're a
                    # structural, write-once acceptance bar (see
                    # `core/dod.py`). When the spec exists and the verdict
                    # fails, the finish is REFUSED and the run CONTINUES —
                    # the same refuse-and-continue discipline verify-on-
                    # finish uses. When no spec is set for the
                    # conversation, the gate is a no-op (legacy path is
                    # byte-identical). See `_finish_dod_gate_passed` for
                    # the full algorithm + the byte-identical-no-spec
                    # proof.
                    if not await self._finish_dod_gate_passed():
                        continue  # DoD unmet — keep working (status NOT finished)
                    summary = str(step.tool_call.arguments.get("summary") or "").strip()
                    step = step.model_copy(
                        update={
                            "finished": True,
                            "tool_call": None,
                            "thought": summary or step.thought,
                        }
                    )

                # (e.5) PLAN GATE — in PLANNING mode the planner has THREE valid moves:
                #   1. `submit_plan` → intercepted into a PlanEvent, loop halts for approval.
                #   2. Any OTHER tool in planning_tools (e.g. file_read, file_list, search,
                #      extract) → fall through to the normal action path; the planner explores
                #      before proposing (the Claude-Code-style "Phase 1: gather context").
                #   3. No tool call at all (a prose answer) → nudge back to planning.
                # The plan tool itself is NEVER executed.
                if self.mode == OperatingMode.PLANNING:
                    tc = step.tool_call
                    if tc is not None and tc.tool_name == self._plan_tool:
                        plan = self._plan_from_args(tc.arguments, events)
                        await self._emit(plan)
                        if self._autonomous:
                            # No human to approve → auto-approve INLINE, emitting the
                            # exact same events approve_plan() would, so the event log
                            # is identical whether a human or the harness approved.
                            self.mode = self._execution_mode
                            await self._emit(
                                StatusEvent(
                                    status=ConversationStatus.RUNNING, detail="plan_approved"
                                )
                            )
                            continue  # re-enter the loop already in execution mode
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                                detail=plan.id,
                            )
                        )
                        return await self.get_state()
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
                        if step.thought.strip():
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.AGENT,
                                    message=LLMMessage(
                                        role="assistant", content=step.thought
                                    ),
                                )
                            )
                        self._plan_nudges += 1
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(role="user", content=_PLAN_NUDGE),
                            )
                        )
                        if await self._post_noop_valve() is Disp.HALT:
                            return await self.get_state()
                        continue
                    # tc is a planning-allowed read tool — productive exploration.
                    # Reset the nudge counter and fall through to the normal action path.
                # Any productive step (planning read OR execution action) resets the
                # nudge counter so a recovered loop gets a fresh budget next time.
                self._plan_nudges = 0

                # (f) finish path — subject to stop-hook veto (§7.4)
                if step.finished and step.tool_call is None:
                    # PLAN-MODE EXECUTION GATE — a forcing function, NOT a prompt. If
                    # the loop is in execution mode (planning_tools configured) and the
                    # agent declares "done" without any productive action since plan
                    # approval, refuse the finish: append an IMPLICIT system-reminder
                    # and re-enter the loop. No cap — the reminder keeps firing as long
                    # as the agent tries to walk away without acting. The loop's own
                    # max_iterations + the user's kill switch are the ultimate exits.
                    if (
                        self._planning_tools  # plan-first lifecycle is configured
                        and self.mode != OperatingMode.PLANNING  # we're executing
                        and not self._productive_action_since_approval(events)
                    ):
                        self._execution_nudges += 1  # telemetry
                        # Surface the model's reasoning before nudging (don't
                        # discard it) — mirror the plan-nudge sibling.
                        if step.thought.strip():
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.AGENT,
                                    message=LLMMessage(
                                        role="assistant", content=step.thought
                                    ),
                                )
                            )
                        else:
                            # An empty finish-step persists nothing, so it's
                            # invisible to every event-derived detector; the
                            # instance counter has to carry it (the (g) no-op
                            # path does the same).
                            self._invisible_steps += 1
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(role="user", content=_EXECUTION_NUDGE),
                            )
                        )
                        # BACKSTOP (was missing — the confirm/reject livelock's
                        # second defect): route through the shared actionless
                        # valve. A nudge-only turn emits no ActionEvent, so
                        # `iteration`/max_iterations NEVER advances on it — the
                        # old "max_iterations is the ultimate exit" claim was
                        # false and a model that declared done without ever
                        # acting spun here forever. The valve now lands it
                        # cleanly (FINISHED/PAUSED:noop_limit) after
                        # `_max_consecutive_noops` actionless turns; a model that
                        # recovers and acts resets the streak (engine §h).
                        if await self._post_noop_valve() is Disp.HALT:
                            return await self.get_state()
                        continue

                    # BROWSER-VERIFY GATE — §BP-05. If web deliverable holds, refuse finish
                    # until a clean browser observation (zero console errors) exists
                    # since the last state-changing edit.
                    if (
                        self._planning_tools
                        and self.mode != OperatingMode.PLANNING
                        and _is_web_deliverable(events)
                    ):
                        since_seq = _last_productive_seq(events)
                        ok, _ = _browser_verified(events, since_seq)
                        # Messaging reads the FULL history: a post-browse edit
                        # invalidates the verification but not what was seen.
                        first_error = _latest_browser_error(events)
                        if ok:
                            self._browser_verify_refusals = 0  # reset on clean pass
                        elif self._browser_verify_refusals < 3:
                            self._browser_verify_refusals += 1
                            if first_error:
                                # Variant (2): quote the error
                                nudge = (
                                    "Before finishing: verify your app the way a user would. "
                                    "Use the browser tool to navigate to http://127.0.0.1:8000/, "
                                    "read the CONSOLE output, and fix any errors you see. "
                                    f"The last load had errors: {first_error}"
                                )
                            else:
                                # Variant (1): verbatim from order
                                nudge = (
                                    "Before finishing: verify your app the way a user would. "
                                    "Use the browser tool to navigate to http://127.0.0.1:8000/, "
                                    "read the CONSOLE output, and fix any errors you see. "
                                    "Finish only after a clean load."
                                )
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(role="user", content=nudge),
                                )
                            )
                            continue
                        else:
                            # 3-refusal release valve (3): allow but warn visibly
                            warn_msg = (
                                "⚠ finished WITHOUT a clean browser verification — "
                                f"last console errors: {first_error or 'none seen'}"
                            )
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(role="user", content=warn_msg),
                                )
                            )

                    if await self._stop_allowed(state, events):
                        # Record the agent's final message (the answer) before
                        # finishing — the deliverable text belongs on the log, not
                        # discarded on the finish signal. (When the model just
                        # answers a question, this IS the response the UI renders.)
                        if step.thought.strip():
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.AGENT,
                                    message=LLMMessage(role="assistant", content=step.thought),
                                )
                            )
                        # AUTO-CONTINUE GATE — when the agent declares finished
                        # but the plan still has incomplete steps, DO NOT land
                        # in STUCK and freeze. The user shouldn't have to poke
                        # the loop to keep going. Instead, inject a continuation
                        # prompt and re-run automatically, up to AUTO_CONTINUE_CAP
                        # times per user message. Only after the cap (typically
                        # 3) do we fall through to FINISHED with a "soft" detail
                        # so the user sees a clean ending rather than a freeze.
                        #
                        # The cap resets when the user sends a new message —
                        # each fresh prompt gets its own auto-continue budget.
                        incomplete, missing = self._plan_is_incomplete(events)
                        if incomplete:
                            attempts = self._auto_continue_attempts(events)
                            if attempts < self._auto_continue_cap:
                                await self._emit(
                                    MessageEvent(
                                        source=EventSource.ENVIRONMENT,
                                        message=LLMMessage(
                                            role="user",
                                            content=(
                                                "<system-reminder>\n"
                                                "You declared the work finished, but plan "
                                                f"steps {missing} are not yet marked done. "
                                                "Keep working: either complete the remaining "
                                                "steps and mark them via "
                                                "plan_step(idx, 'done'), OR — if a step is "
                                                "structurally wrong now — call "
                                                "propose_plan_update to revise the plan. "
                                                "Do not declare finished again until every "
                                                "step is marked done. The user will see this "
                                                "as the agent automatically continuing.\n"
                                                "</system-reminder>"
                                            ),
                                        ),
                                    )
                                )
                                await self._emit(
                                    StatusEvent(
                                        status=ConversationStatus.RUNNING,
                                        detail=f"auto_continue:plan_incomplete:{attempts + 1}",
                                    )
                                )
                                continue  # back to the loop's top — keep going
                            # Cap hit. Land FINISHED with a "partial" detail
                            # rather than STUCK; the user sees a clean ending
                            # and can steer if more work is needed. STUCK is
                            # reserved for genuine confusion (stuck detector),
                            # not for "model couldn't quite finish the bookkeeping".
                            actions_since = self._actions_since_last_resume(events)
                            if actions_since == 0:
                                await self._emit(
                                    MessageEvent(
                                        source=EventSource.ENVIRONMENT,
                                        message=LLMMessage(
                                            role="user",
                                            content=(
                                                "<system-reminder>\n⚠ finishing was"
                                                " blocked: plan steps remain undone and"
                                                " no work happened in this run segment."
                                                "\n</system-reminder>"
                                            ),
                                        )
                                    )
                                )
                                await self._emit(
                                    StatusEvent(
                                        status=ConversationStatus.PAUSED,
                                        detail="partial_plan",
                                    )
                                )
                            else:
                                await self._emit(
                                    MessageEvent(
                                        source=EventSource.ENVIRONMENT,
                                        message=LLMMessage(
                                            role="user",
                                            content=(
                                                "<system-reminder>\n"
                                                f"After {self._auto_continue_cap} auto-continues, "
                                                f"plan steps {missing} are still not marked done. "
                                                "Landing the run as FINISHED with partial-plan "
                                                "detail — the user can review and steer if more "
                                                "work is needed.\n"
                                                "</system-reminder>"
                                            ),
                                        ),
                                    )
                                )
                                await self._emit(
                                    StatusEvent(
                                        status=ConversationStatus.FINISHED,
                                        detail="partial_plan",
                                    )
                                )
                            return await self.get_state()
                        await self._emit(StatusEvent(status=ConversationStatus.FINISHED))
                        return await self.get_state()
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(role="user", content=self._veto_feedback),
                        )
                    )
                    continue

                # (g) no-op step (thought only) — record and continue. GAP B
                # backstop: a tool-less prose turn emits a MessageEvent (not an
                # ActionEvent), so `iteration`/max_iterations never advances on
                # it. Without a guard, a model that keeps talking without acting
                # would loop forever. Count consecutive no-ops since the last
                # real action / user message; nudge, then hard-stop.
                if step.tool_call is None:
                    if step.thought.strip():
                        await self._emit(
                            MessageEvent(
                                source=EventSource.AGENT,
                                message=LLMMessage(role="assistant", content=step.thought),
                            )
                        )
                    else:
                        # Nothing persisted — invisible to every event-derived
                        # detector, so the instance counter has to carry it.
                        self._invisible_steps += 1
                    if await self._post_noop_valve() is Disp.HALT:
                        return await self.get_state()
                    continue

                # (g.5) ASK-USER GATE — `ask_user` is a MODEL-CHOSEN escape
                # hatch. When the agent itself decides it needs human input
                # (after trying 2-3 distinct approaches that all failed, OR
                # when the right path depends on a judgment call only the user
                # can make), it can call ask_user(question, options=[...]).
                #
                # This is NEVER nudged by the harness — the model discovers the
                # tool via its system prompt + tool list, and chooses to use it.
                # That's the Claude Code lesson: tools are options the model
                # discovers naturally, not fallbacks the harness funnels into.
                # The loop intercepts the call (the tool is never "executed"
                # against the sandbox), builds an AlternativesEvent, and halts
                # at AWAITING_USER_DECISION until the user picks an option or
                # types a steer message.
                # PROPOSE-PLAN-UPDATE GATE — when the agent's existing plan is
                # no longer right (a step failed, a discovery invalidates the
                # path, the user steered toward a different goal), the agent can
                # call `propose_plan_update` to emit a NEW plan revision and
                # halt at AWAITING_PLAN_APPROVAL. The user accepts (Approve &
                # build), refines (Revise…), or rejects. The new plan slots
                # chronologically into the chat (PlanPanel renders the latest
                # revision; prior ones stay in the event log for audit).
                #
                # This is what enables auto-recovery without the user having to
                # poke the model after every failure — the model proposes a
                # course correction; the user confirms or refines.
                # FRESH-SESSION BACKSTOP (Phase-B re-run #5, 2026-06-10):
                # ask_user / propose_plan_update are withheld from the offered
                # set until the session's first real action (_tools_for_step),
                # but a weak model can hallucinate calls to unoffered tools —
                # re-run #5's model called ask_user with an EMPTY question arg
                # as its first post-resume move, halting the run on its own
                # narration. Refuse with the same actionable-feedback shape as
                # the serve gate: attempt the step first, then ask/re-plan
                # with evidence in hand. PLANNING is exempt (ask_user before
                # committing a plan is the legitimate use).
                if (
                    step.tool_call is not None
                    and step.tool_call.tool_name in ("ask_user", "clarify", "propose_plan_update")
                    and self.mode != OperatingMode.PLANNING
                    and self._actions_since_last_resume(events) == 0
                    # In autonomous mode, ask_user/clarify are owned by the headless-
                    # stall guard below (a clean "no user — decide yourself" nudge);
                    # don't pre-empt them here with the interactive "do work, then ask"
                    # message, which tells the model it can ask when it can't. (g.5)
                    # still governs propose_plan_update on a fresh session.
                    and not (
                        self._autonomous
                        and step.tool_call.tool_name in ("ask_user", "clarify")
                    )
                ):
                    self._invisible_steps += 1
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    f"{step.tool_call.tool_name} refused: no real "
                                    "work has happened yet in this session. Attempt "
                                    "the next plan step with real tool calls first — "
                                    "if it fails or something is genuinely unclear, "
                                    "you can then ask or propose a plan change with "
                                    "the evidence in hand."
                                ),
                            ),
                        )
                    )
                    if await self._post_noop_valve() is Disp.HALT:
                        return await self.get_state()
                    continue  # non-blocking — let the model act on the feedback

                # AUTONOMOUS HEADLESS-STALL GUARD. _tools_for_step withholds
                # ask_user/clarify from the schema in autonomous mode, but a weak
                # model can still hallucinate the NAME (prefill, imitation of
                # training data). The first-action guard above only fires before any
                # real work; once work has happened a hallucinated ask_user/clarify
                # would fall through to the handlers below and halt at
                # AWAITING_USER_QUESTION — a silent headless stall, since there is no
                # user to answer. Convert it into a self-directed nudge: record the
                # proposed call (so the assistant tool_call stays PAIRED with a tool
                # result — KV stability, same discipline as the hard-deny path),
                # then feed back a system-reminder that there's no user and it must
                # decide and continue. Gated on self._autonomous (default OFF) so the
                # interactive path is byte-for-byte untouched.
                if (
                    self._autonomous
                    and step.tool_call is not None
                    and step.tool_call.tool_name in ("ask_user", "clarify")
                ):
                    asked = str(
                        step.tool_call.arguments.get("question") or ""
                    ).strip() or step.thought.strip()
                    stall_action = ActionEvent(
                        thought=step.thought,
                        tool_call=step.tool_call,
                        self_assessed_risk=step.self_assessed_risk,
                        llm_response_id=step.llm_response_id,
                    )
                    await self._emit(stall_action)
                    await self._emit(
                        AgentErrorEvent(
                            error=(
                                "<system-reminder>\n"
                                "Autonomous mode is ON — there is no user available "
                                f"to answer. `{step.tool_call.tool_name}` is "
                                "unavailable in this mode. Make the best decision you "
                                "can from the information you already have and continue "
                                "working toward the goal."
                                + (f"\nYour question was: {asked}" if asked else "")
                                + "\n</system-reminder>"
                            ),
                            action_id=stall_action.id,
                            tool_call_id=(
                                stall_action.tool_call.call_id
                                if stall_action.tool_call
                                else None
                            ),
                        )
                    )
                    continue  # non-blocking — let the model act on its own judgment

                if (
                    step.tool_call is not None
                    and step.tool_call.tool_name == "propose_plan_update"
                ):
                    new_plan = self._plan_from_args(step.tool_call.arguments, events)
                    await self._emit(new_plan)
                    if self._autonomous:
                        # No human to approve a mid-run plan revision → auto-approve
                        # inline, emitting exactly what approve_plan() would (mode flip
                        # + RUNNING/plan_approved), so the event log is identical whether
                        # a human or the harness approved. Without this, an autonomous
                        # run that follows the loop's OWN "call propose_plan_update to
                        # revise the plan" guidance (the auto-continue nudge below) would
                        # halt at AWAITING_PLAN_APPROVAL forever — a headless stall.
                        #
                        # C8 (T11): bound the propose_plan_update loop. A weak model
                        # in autonomous mode can hammer the same plan revision over
                        # and over, never realizing there's no human to approve it.
                        # Compare new_plan.steps to the immediately-prior plan's steps
                        # (ignore summary; an appended/added step counts as DIFFERENT
                        # because list lengths differ). Increment the consecutive-
                        # identical counter on a match, reset on a diff. At >= the
                        # cap, feed the existing bookkeeping-stuck valve (emit the
                        # same `bookkeeping_only` warning + STUCK status the (c.3)
                        # valve emits) — do NOT invent a new halt path. Only in
                        # autonomous mode; interactive path is byte-identical.
                        prior_plan: PlanEvent | None = None
                        for e in reversed(events):
                            if e is new_plan:
                                continue  # skip the just-emitted new_plan
                            if isinstance(e, PlanEvent):
                                prior_plan = e
                                break
                        if (
                            prior_plan is not None
                            and [s.title for s in prior_plan.steps]
                            == [s.title for s in new_plan.steps]
                        ):
                            self._identical_plan_revisions += 1
                        else:
                            self._identical_plan_revisions = 0
                        if (
                            self._identical_plan_revisions
                            >= _PROPOSE_PLAN_UPDATE_REPEAT_CAP
                        ):
                            # Reuse the existing bookkeeping-stuck valve: same
                            # message + STUCK/detail pair (c.3) emits.
                            await self._emit(
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(
                                        role="user",
                                        content=(
                                            "⚠ Stopped: the agent kept updating the "
                                            "plan checklist without doing any real "
                                            "work. Re-run or steer it toward a "
                                            "concrete action."
                                        ),
                                    ),
                                )
                            )
                            await self._emit(
                                StatusEvent(
                                    status=ConversationStatus.STUCK,
                                    detail="bookkeeping_only",
                                )
                            )
                            return await self.get_state()
                        self.mode = self._execution_mode
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.RUNNING, detail="plan_approved"
                            )
                        )
                        continue
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                            detail=new_plan.id,
                        )
                    )
                    return await self.get_state()

                if step.tool_call is not None and step.tool_call.tool_name == "clarify":
                    # clarify → ClarifyEvent with typed questions. The planner
                    # calls this when SEVERAL specifics are missing and
                    # guessing would produce a bad plan. The loop emits a
                    # ClarifyEvent (carrying the structured question items)
                    # and halts at AWAITING_USER_QUESTION. The user answers
                    # each question; the answers are re-injected as a user
                    # message that resumes planning.
                    from ..events import ClarifyEvent as _ClarifyEvent
                    from ..events import ClarifyQuestionItem

                    question = str(
                        step.tool_call.arguments.get("question") or ""
                    ).strip() or step.thought.strip()
                    raw_items = step.tool_call.arguments.get("questions") or []
                    items: list[ClarifyQuestionItem] = []
                    for it in raw_items:
                        if not isinstance(it, dict):
                            continue
                        qid = str(it.get("id") or f"q{len(items) + 1}").strip()
                        qtext = str(it.get("question") or "").strip()
                        if not qtext:
                            continue
                        qtype = str(it.get("type") or "short_text").strip()
                        if qtype not in ("short_text", "long_text", "choice"):
                            qtype = "short_text"
                        qopts = it.get("options") or []
                        if isinstance(qopts, list):
                            qopts = [str(o) for o in qopts]
                        else:
                            qopts = []
                        items.append(
                            ClarifyQuestionItem(
                                id=qid, question=qtext, type=qtype, options=qopts
                            )
                        )
                    if not items:
                        # No valid questions → fall back to free-form ask_user
                        q_event = MessageEvent(
                            source=EventSource.AGENT,
                            message=LLMMessage(
                                role="assistant",
                                content=(
                                    question or "The agent needs clarification before planning."
                                ),
                            ),
                        )
                        await self._emit(q_event)
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.AWAITING_USER_QUESTION,
                                detail=q_event.id,
                            )
                        )
                        return await self.get_state()
                    clarify_event = _ClarifyEvent(
                        question=question or "The agent needs clarification before planning.",
                        items=items,
                    )
                    await self._emit(clarify_event)
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.AWAITING_USER_QUESTION,
                            detail=clarify_event.id,
                        )
                    )
                    return await self.get_state()

                if step.tool_call is not None and step.tool_call.tool_name == "ask_user":
                    alt = self._alternatives_from_args(step.tool_call.arguments, events)
                    if alt is not None:
                        # ask_user WITH options → AlternativesEvent + gate
                        await self._emit(alt)
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.AWAITING_USER_DECISION,
                                detail=alt.id,
                            )
                        )
                        return await self.get_state()
                    # ask_user WITHOUT options → free-form question. Emit it as
                    # an assistant message (from the tool's `question` arg or
                    # the step's thought, whichever has content) and halt at the
                    # two-way Ask-gate (AWAITING_USER_QUESTION) — the UI renders
                    # an AskPanel with a focused answer box, not muted prose. The
                    # status's detail carries the question message's id so the
                    # surface can resolve it. The user's reply (send_message /
                    # steer) IS the resume signal.
                    question = str(
                        step.tool_call.arguments.get("question") or ""
                    ).strip() or step.thought.strip()
                    question_id: str | None = None
                    if question:
                        q_event = MessageEvent(
                            source=EventSource.AGENT,
                            message=LLMMessage(role="assistant", content=question),
                        )
                        question_id = q_event.id
                        await self._emit(q_event)
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.AWAITING_USER_QUESTION,
                            detail=question_id or "free_form_question",
                        )
                    )
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

                # (h.5) HARD DENY (Cluster 3) — catastrophic commands are refused
                # outright, BEFORE the confirm gate. No approval, policy, or LLM
                # can run them. The agent sees the refusal as an error and adapts.
                deny_reason = self._hard_deny_reason(action)
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
                    continue

                # (i) RISK GATE — assess, then maybe require confirmation (§5).
                # Audit (security §7): when the analyzer exposes the detailed
                # assessment, stamp it into the action's meta so the security
                # posture (final risk, rationale, contributing analyzers, the
                # self-assessment) is reconstructable from the log. Analyzers that
                # implement only assess() are unaffected.
                detailed = getattr(self.analyzer, "assess_detailed", None)
                if callable(detailed):
                    assessment = detailed(action)
                    risk = assessment.risk
                    audited_meta = {
                        **action.meta,
                        "risk_assessment": assessment.model_dump(mode="json"),
                    }
                    action = action.model_copy(update={"meta": audited_meta})
                else:
                    risk = self.analyzer.assess(action)

                # DC-03: obtain WHERE the tool executes (duck-typed; absent → "unknown").
                _scope_fn = getattr(self.executor, "tool_scope", None)
                _tool_scope = "unknown"
                if callable(_scope_fn):
                    try:
                        _tool_scope = _scope_fn(action.tool_call.tool_name)
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
                    return await self.get_state()

                action_to_execute = action

            # (j) EXECUTE outside the lock (long-running; lock only guards state)
            await self._emit(action_to_execute)
            await self._execute_and_observe(action_to_execute)
            # loop continues

    @staticmethod
    def _has_unprocessed_user_message(events: list[Event]) -> bool:
        """True if a USER message arrived after the most recent agent activity —
        i.e. there is fresh work (a new goal, or a reopen after FINISHED/STUCK)."""
        last_user = max(
            (
                e.seq or 0
                for e in events
                if isinstance(e, MessageEvent) and e.source == EventSource.USER
            ),
            default=None,
        )
        if last_user is None:
            return False
        # Progress = the agent acted, spoke, or the loop reached a run/terminal
        # status after the message. A finish-only step leaves no action/message,
        # so the terminal StatusEvent is what marks the goal as processed.
        activity_statuses = {
            ConversationStatus.RUNNING,
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
        }
        last_progress = max(
            (
                e.seq or 0
                for e in events
                if isinstance(e, ActionEvent)
                or (isinstance(e, MessageEvent) and e.source == EventSource.AGENT)
                or (isinstance(e, StatusEvent) and e.status in activity_statuses)
            ),
            default=0,
        )
        return last_user > last_progress

    # ---- control operations (§7) — map from the wire frames -----------------

    async def send_message(self, text: str, *, steer: bool = False) -> ConversationState:
        """Append a USER message. Never dropped; picked up at the next iteration
        (the next View includes it). If the conversation had FINISHED/STUCK, it
        reopens to IDLE (§2). `steer` differs only in UI intent (BoD §13.4)."""
        async with self._lock:
            state = await self.get_state()
            await self._emit(
                MessageEvent(
                    source=EventSource.USER,
                    message=LLMMessage(role="user", content=text),
                    meta={"steer": True} if steer else {},
                )
            )
            if state.execution_status in (
                ConversationStatus.FINISHED,
                ConversationStatus.STUCK,
            ):
                await self._emit(StatusEvent(status=ConversationStatus.IDLE))
        return await self.get_state()

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

    async def approve_plan(self) -> ConversationState:
        """Approve the pending plan: flip into execution mode (full tools restored)
        and resume to RUNNING. The caller then re-runs the loop. The per-action
        risk gate still governs the build that follows (defense in depth)."""
        async with self._lock:
            state = await self.get_state()
            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
                return state
            self.mode = self._execution_mode
            await self._emit(
                StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
            )
        return await self.get_state()

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
            # Synthesize the ActionEvent. The thought records WHY we're running
            # it (the option's description) so the trace stays self-explanatory.
            action = ActionEvent(
                source=EventSource.AGENT,
                thought=f"User picked alternative: {option.title}. {option.description}",
                tool_call=ToolCall(
                    tool_name=option.tool_name,
                    arguments=option.arguments,
                ),
            )
            emitted = await self._emit(action)
            await self._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=f"alternative_picked:{option.id}",
                )
            )
        # Execute the synthesized action directly so the option actually runs
        # before returning to the main loop (analogous to confirm()'s
        # post-gate execute). The loop's next call to run() then proceeds
        # with the freshly-emitted observation in view.
        await self._execute_and_observe(emitted)
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
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))
        return await self.get_state()

    async def pause(self) -> ConversationState:
        async with self._lock:
            await self._emit(StatusEvent(status=ConversationStatus.PAUSED))
        return await self.get_state()

    async def resume(self) -> ConversationState:
        async with self._lock:
            await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        return await self.run()

    async def cancel(self) -> ConversationState:
        """Cooperative stop (distinct from the network-level kill switch, §7.3).
        Emits a terminal IDLE; the loop returns at its next checkpoint."""
        async with self._lock:
            await self._emit(StatusEvent(status=ConversationStatus.IDLE, detail="cancelled"))
        return await self.get_state()
