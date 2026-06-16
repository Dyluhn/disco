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
    DoDPredicate,
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
    LLMContextWindowExceeded,
    LLMError,
    LLMRouter,
    LLMTransientError,
    OperatingMode,
)
from ..state import ConversationState
from ..store.base import EventStore
from ..view import Condenser, Summarizer, View
from .bootstrap import _detect_project_bootstrap
from .boundaries import (
    Agent,
    AgentStep,
    ConfirmationPolicy,
    SecurityAnalyzer,
    StopHook,
    ToolExecutor,
)
from .control import Disp
from . import signals, view_render
from .fc_kit import _nearest_tool_name
from .observe import (  # noqa: F401 — _FANOUT_INPUT_MAX_CHARS re-exported for back-compat
    Observer,
    _FANOUT_INPUT_MAX_CHARS,
)
from .plan_conditions import PlanStepConditions
from .recitation import (  # noqa: F401 — _RECITATION_SENTINEL re-exported for back-compat
    RecitationRegrounder,
    _RECITATION_SENTINEL,
)
from .signals import (  # noqa: F401 — re-exported for back-compat (moved to signals.py)
    _BOOKKEEPING_TOOLS,
    _NON_PRODUCTIVE_TOOLS,
)
from .view_render import (  # noqa: F401 — re-exported for back-compat (moved to view_render.py)
    _WS_MAX_FILES,
    _WS_PER_FILE_CHARS,
    _WS_READ_TIMEOUT_S,
    _WS_TOTAL_CHARS,
)
from .messages import (
    _describe_llm_error,
    _hs03_reground_message,
    _stuck_escape_reminder,
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


# `_BOOKKEEPING_TOOLS` + `_NON_PRODUCTIVE_TOOLS` now live in loop/signals.py
# (the pure log-derived signal helpers that use them moved there too).


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

# _WS_* snapshot budget constants moved to loop/view_render.py with the snapshot fn.


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
        incomplete, _ = signals.plan_is_incomplete(events)
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
            actions_since = signals.actions_since_last_resume(events)
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

    # initial_mode delegates to signals (public API for rebuilt loops / restart).
    initial_mode = staticmethod(signals.initial_mode)

    # ---- view materialization + condensation (§8) ---------------------------

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

    def _gate_recitation(self, view: View, events: list[Event]) -> View:
        return self._recit.gate_recitation(view, events)

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

    async def _drain_recovered_memory_facts(self) -> int:
        return await self._recit.drain_recovered_memory_facts()

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

        deny = signals.hard_deny_reason(action)
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

    async def _refuse_fresh_session(self, step: AgentStep, msg: str) -> Disp:
        """Shared fresh-session refusal: a virtual tool (remember / serve / ask /
        propose_plan_update) was called before any real action this session.
        Count the invisible step, surface actionable feedback, and route through
        the actionless valve. Returns HALT if the valve trips, else CONTINUE."""
        self._invisible_steps += 1
        await self._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=msg),
            )
        )
        return await self._post_noop_valve()

    async def _post_noop_valve(self) -> Disp:
        """Shared actionless-valve tail for the non-blocking virtual-tool arms
        (notify_user / remember / serve / delegate_explore / plan-nudge /
        execution-nudge / no-op / ask-fresh-session). Re-polls the event log,
        adds the invisible-step counter, and halts the run if the actionless
        valve trips. Byte-identical to the 4-line tail it replaces."""
        events = await self._events()
        noops = signals.consecutive_noops(events) + self._invisible_steps
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
            and signals.actions_since_last_resume(events) == 0
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
        escape_seq = signals.stuck_escape_seq(events)
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
                attempt = signals.stuck_escape_attempt_count(events)
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
        fails = signals.count_recent_failures(events)
        recent_errors = [
            e.error for e in reversed(events) if isinstance(e, AgentErrorEvent)
        ][:fails]
        recovery_requested = signals.recovery_requested_since_reset(events)
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

    async def _gate_plan_step_lag(self, events: list[Event]) -> list[Event]:
        # (c.5) SOFT plan-step nudge — the auditor. When substantial work
        # has happened but the capstone tracker is lagging (the "did the
        # work, forgot to check it off" failure), inject ONE gentle
        # reminder so the model keeps the tracker honest. NOT a gate —
        # the model is free to ignore it; it fires at most once per lag
        # episode. This is the proactive nudge (vs. the finish-boundary
        # auto-continue which catches the same thing at the end).
        if signals.plan_step_lag_signal(events):
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
        return events

    async def _gate_bookkeeping_streak(self, events: list[Event]) -> tuple[Disp, list[Event]]:
        # (c.3) plan_step-spam guard (issue C). A soft nudge once at the
        # streak threshold; a hard STUCK halt at the cap (the model is doing
        # nothing but shuffling the plan tracker — every other valve misses
        # this). STUCK (not a silent proceed) keeps the failure VISIBLE, which
        # matters most for weak local models. Autonomous mode turns STUCK into
        # a clean forfeit (issue A).
        _bk_streak = signals.bookkeeping_streak_len(events)
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
            _plan_steps = signals.active_plan_step_count(events)
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
                return Disp.HALT, events
        return Disp.FALLTHROUGH, events

    def _known_tool_names_for_requery(self) -> set[str]:
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
        return all_known_names

    def _unknown_tool_requery_hint(self, tool_name: str, offered_names: set[str]) -> str:
        _hint = (
            f"ERROR: Unknown tool '{tool_name}'. "
            f"Available: {sorted(list(offered_names))}"
        )
        if self._assist:
            _suggestion = _nearest_tool_name(
                tool_name, offered_names
            )
            if _suggestion is not None:
                _hint = f"{_hint} did you mean '{_suggestion}'?"
        return _hint

    async def _drive_step(self, view: View, events: list[Event]) -> tuple[AgentStep | None, Disp]:
        # (e) ask the agent for ONE action (principle 1). The visible tool
        # set is mode-scoped: while PLANNING the agent sees ONLY the plan
        # tool (so it can't act before approval); while executing it sees
        # everything except the plan tool.
        # Escape temperature: jitter HARD to break a self-imitation chain —
        # the single step right after a stuck reframe (StuckDetector's escape).
        # escape_seq/acted_since_escape are pure functions of `events`
        # (the stuck gate recomputes its own copy); recompute here for the
        # temperature decision (folds into `_drive_step` on extraction).
        escape_seq = signals.stuck_escape_seq(events)
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
            and signals.actions_since_last_resume(events) == 0
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
                        overflow_signal=view_render.overflow_signal(events),
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
                    all_known_names = self._known_tool_names_for_requery()

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
                            _hint = self._unknown_tool_requery_hint(
                                step.tool_call.tool_name, offered_names
                            )
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
                        return None, Disp.HALT
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
                return None, Disp.CONTINUE
            await self._emit(
                ErrorEvent(code="context_window", detail="hard reset made no progress")
            )
            return None, Disp.HALT
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
            return None, Disp.HALT
        return step, Disp.FALLTHROUGH

    async def _handle_notify_user(self, step: AgentStep, events: list[Event]) -> Disp:
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
            return Disp.HALT
        return Disp.CONTINUE

    async def _handle_remember(self, step: AgentStep, events: list[Event]) -> Disp:
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
            and signals.actions_since_last_resume(events) == 0
        ):
            return await self._refuse_fresh_session(
                step,
                "remember refused: no real work has happened "
                "yet in this session. Facts worth pinning come "
                "from real observations — execute the next plan "
                "step with real tool calls first, then remember "
                "what you learned.",
            )
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
            return Disp.HALT
        return Disp.CONTINUE

    async def _handle_serve(self, step: AgentStep, events: list[Event]) -> Disp:
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
        if signals.actions_since_last_resume(events) == 0:
            return await self._refuse_fresh_session(
                step,
                "serve refused: no real work has happened yet in "
                "this session — the sandbox is fresh and nothing "
                "is running. Execute the next plan step with real "
                "tool calls (write files, run commands, start your "
                "server), then serve the result.",
            )
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
            return Disp.HALT
        return Disp.CONTINUE

    async def _handle_delegate_explore(self, step: AgentStep, events: list[Event]) -> Disp:
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
            return Disp.CONTINUE
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
            return Disp.HALT
        return Disp.CONTINUE

    def _resolve_verify_command(self, args: dict) -> str:
        verify_cmd = str(args.get("verify") or "").strip()
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
        return verify_cmd

    async def _normalize_finish_step(
        self, step: AgentStep, events: list[Event]
    ) -> tuple[AgentStep, Disp]:
        # VERIFY-ON-FINISH (post-condition gate). If the agent attached
        # a `verify` check to finish, RUN it first and refuse the finish
        # if it doesn't pass — the "run the tests before you claim done"
        # forcing function. The check is visible in the trace; on
        # failure the agent sees exactly what broke and adapts, instead
        # of declaring a broken build complete.
        verify_cmd = self._resolve_verify_command(step.tool_call.arguments)
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
                return step, Disp.CONTINUE
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
            return step, Disp.CONTINUE
        summary = str(step.tool_call.arguments.get("summary") or "").strip()
        step = step.model_copy(
            update={
                "finished": True,
                "tool_call": None,
                "thought": summary or step.thought,
            }
        )
        return step, Disp.FALLTHROUGH

    async def _gate_planning_mode(self, step: AgentStep, events: list[Event]) -> Disp:
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
                    return Disp.CONTINUE
                await self._emit(
                    StatusEvent(
                        status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                        detail=plan.id,
                    )
                )
                return Disp.HALT
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
                    return Disp.HALT
                return Disp.CONTINUE
            # tc is a planning-allowed read tool — productive exploration.
            # Reset the nudge counter and fall through to the normal action path.
        return Disp.FALLTHROUGH

    async def _gate_execution_nudge(self, step: AgentStep, events: list[Event]) -> Disp:
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
            and not signals.productive_action_since_approval(events)
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
                return Disp.HALT
            return Disp.CONTINUE
        return Disp.FALLTHROUGH

    async def _gate_browser_verify(self, step: AgentStep, events: list[Event]) -> Disp:
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
                return Disp.CONTINUE
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
        return Disp.FALLTHROUGH

    async def _finalize_finish(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
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
            incomplete, missing = signals.plan_is_incomplete(events)
            if incomplete:
                attempts = signals.auto_continue_attempts(events)
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
                    return Disp.CONTINUE
                # Cap hit. Land FINISHED with a "partial" detail
                # rather than STUCK; the user sees a clean ending
                # and can steer if more work is needed. STUCK is
                # reserved for genuine confusion (stuck detector),
                # not for "model couldn't quite finish the bookkeeping".
                actions_since = signals.actions_since_last_resume(events)
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
                return Disp.HALT
            await self._emit(StatusEvent(status=ConversationStatus.FINISHED))
            return Disp.HALT
        await self._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=self._veto_feedback),
            )
        )
        return Disp.CONTINUE

    async def _handle_finish_path(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        disp = await self._gate_execution_nudge(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT

        disp = await self._gate_browser_verify(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE

        disp = await self._finalize_finish(step, state, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT
        return Disp.FALLTHROUGH

    async def _handle_noop_step(self, step: AgentStep, events: list[Event]) -> Disp:
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
            return Disp.HALT
        return Disp.CONTINUE

    async def _gate_ask_fresh_session(self, step: AgentStep, events: list[Event]) -> Disp:
        if (
            step.tool_call is not None
            and step.tool_call.tool_name in ("ask_user", "clarify", "propose_plan_update")
            and self.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
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
            return await self._refuse_fresh_session(
                step,
                f"{step.tool_call.tool_name} refused: no real "
                "work has happened yet in this session. Attempt "
                "the next plan step with real tool calls first — "
                "if it fails or something is genuinely unclear, "
                "you can then ask or propose a plan change with "
                "the evidence in hand.",
            )
        return Disp.FALLTHROUGH

    async def _gate_autonomous_ask_stall(self, step: AgentStep, events: list[Event]) -> Disp:
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
            return Disp.CONTINUE
        return Disp.FALLTHROUGH

    async def _handle_propose_plan_update(self, step: AgentStep, events: list[Event]) -> Disp:
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
                return Disp.HALT
            self.mode = self._execution_mode
            await self._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING, detail="plan_approved"
                )
            )
            return Disp.CONTINUE
        await self._emit(
            StatusEvent(
                status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                detail=new_plan.id,
            )
        )
        return Disp.HALT

    async def _handle_clarify(self, step: AgentStep, events: list[Event]) -> Disp:
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
            return Disp.HALT
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
        return Disp.HALT

    async def _handle_ask_user(self, step: AgentStep, events: list[Event]) -> Disp:
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
            return Disp.HALT
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
        return Disp.HALT

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

                # HS-03 — scheduled facts re-grounding (assist-tier only), BEFORE
                # stuck-detection so a re-ground + a stuck-escape can co-fire.
                # Re-polls events so the materialized View sees the recap this turn.
                # Full cadence/closure rationale: see _maybe_emit_reground.
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

                events = await self._gate_plan_step_lag(events)

                disp, events = await self._gate_bookkeeping_streak(events)
                if disp is Disp.HALT:
                    return await self.get_state()

                # (d) build the model-facing View, condensing if triggered (§8)
                view = await self._materialize_view(events)

                step, disp = await self._drive_step(view, events)
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
                    if await self._handle_notify_user(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if step.tool_call is not None and step.tool_call.tool_name == "remember":
                    if await self._handle_remember(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if step.tool_call is not None and step.tool_call.tool_name == "serve":
                    if await self._handle_serve(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if (
                    step.tool_call is not None
                    and step.tool_call.tool_name == "delegate_explore"
                ):
                    if await self._handle_delegate_explore(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue
                if step.tool_call is not None and step.tool_call.tool_name == "finish":
                    step, disp = await self._normalize_finish_step(step, events)
                    if disp is Disp.CONTINUE:
                        continue

                disp = await self._gate_planning_mode(step, events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()
                # Any productive step (planning read OR execution action) resets the
                # nudge counter so a recovered loop gets a fresh budget next time.
                self._plan_nudges = 0

                # (f) finish path — subject to stop-hook veto (§7.4)
                if step.finished and step.tool_call is None:
                    disp = await self._handle_finish_path(step, state, events)
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
                    if await self._handle_noop_step(step, events) is Disp.HALT:
                        return await self.get_state()
                    continue

                # (g.5) Model-chosen escape hatches: ask_user / clarify (halt for
                # human input), propose_plan_update (re-plan + re-approval). Each is
                # intercepted by its handler below; the loop never nudges the model
                # toward them. First, the fresh-session backstop: a hallucinated
                # ask/clarify/propose before any real action this session is refused
                # with actionable feedback (see _gate_ask_fresh_session).
                disp = await self._gate_ask_fresh_session(step, events)
                if disp is Disp.CONTINUE:
                    continue
                if disp is Disp.HALT:
                    return await self.get_state()

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
                disp = await self._gate_autonomous_ask_stall(step, events)
                if disp is Disp.CONTINUE:
                    continue

                if (
                    step.tool_call is not None
                    and step.tool_call.tool_name == "propose_plan_update"
                ):
                    disp = await self._handle_propose_plan_update(step, events)
                    if disp is Disp.CONTINUE:
                        continue
                    if disp is Disp.HALT:
                        return await self.get_state()

                if step.tool_call is not None and step.tool_call.tool_name == "clarify":
                    if await self._handle_clarify(step, events) is Disp.HALT:
                        return await self.get_state()

                if step.tool_call is not None and step.tool_call.tool_name == "ask_user":
                    if await self._handle_ask_user(step, events) is Disp.HALT:
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

            # (j) EXECUTE outside the lock (long-running; lock only guards state)
            await self._emit(action_to_execute)
            await self._execute_and_observe(action_to_execute)
            # loop continues

    # _has_unprocessed_user_message delegates to signals (external callers + run()).
    _has_unprocessed_user_message = staticmethod(signals.has_unprocessed_user_message)

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
