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
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm import StreamChunk
    from .boundaries import StreamHook

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
)
from ..llm import (
    Difficulty,
    LLMContextWindowExceeded,
    LLMError,
    LLMRouter,
    OperatingMode,
    OverflowSignal,
)
from ..state import ConversationState
from ..store.base import EventStore
from ..view import Condenser, Summarizer, View
from .boundaries import Agent, ConfirmationPolicy, SecurityAnalyzer, StopHook, ToolExecutor
from .stream_extract import extract_partial_string_field
from .stuck import StuckDetector, StuckThresholds

_LOG = logging.getLogger("perpleximanus.loop")

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


def _describe_llm_error(e: LLMError) -> str:
    """Render a model/provider error for the user WITHOUT flattening its reason.

    Reactive error surfacing: when the assigned model rejects the input or the
    provider fails, the user must see the ACTUAL provider message — not a generic
    "model call failed". We keep the typed classification (the exception class the
    adapter mapped to) AND the real reason (its message), plus provider/model
    context when the typed error carries it."""
    reason = str(e) or "(provider returned no message)"
    loc = " / ".join(p for p in (getattr(e, "provider", ""), getattr(e, "model", "")) if p)
    head = f"{type(e).__name__} [{loc}]" if loc else type(e).__name__
    return f"{head}: {reason}"

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
# instead of calling submit_plan: in PLANNING mode the only terminal move is to
# PROPOSE a structured plan. Reads (file_list/file_read/search/extract) fall
# through and never trigger this — only a tool-less prose response does.
_PLAN_NUDGE = (
    "<system-reminder>\n"
    "Still in PLANNING mode — no plan has been proposed yet. The only terminal "
    "move here is to call the `submit_plan` tool with a summary, ordered steps, "
    "and a markdown `context` block. You may continue to read (file_list, "
    "file_read, search, extract) for more context first, but a prose reply alone "
    "doesn't advance the conversation.\n"
    "</system-reminder>"
)

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
# No cap on execution nudges. The reminder keeps firing as long as the agent
# tries to declare done without acting; the loop's own `max_iterations` backstop
# and the user's kill switch are the ultimate exits — we never error out of the
# gate itself. Keep the counter so tests + telemetry can observe how often the
# reminder fired.

# The tool names that don't count as "productive work" for the execution gate:
# meta tools that don't change workspace state. plan_step is informational; the
# planning tool would have been intercepted upstream but is named for clarity.
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
    }
)

# The virtual ask_user tool — the model's escape hatch when it (in its own
# reasoning, not at harness nudging) decides it needs the human's judgment.
# This is the Claude Code pattern done honestly: the tool is described, it's
# in the model's tool list, and the model chooses to call it. The harness
# never says "you must use this now"; the model uses it when its reasoning
# concludes that human input is the next-best step.
#
# When called, the loop INTERCEPTS it (the tool is never executed against the
# sandbox). WITH options it becomes an AlternativesEvent + AWAITING_USER_DECISION
# (the user picks a card); WITHOUT options it becomes a free-form question +
# AWAITING_USER_QUESTION (the user types an answer). Either way the reply resumes
# the run.
_ASK_USER_TOOL_NAME = "ask_user"
_ASK_USER_DESCRIPTION = (
    "Pause the run and ask the human user for input. Call this tool when YOU "
    "(in your own reasoning) decide that the next step depends on human "
    "judgment, a clarification, or a choice between options you can't pick "
    "with confidence. Typical situations: you've tried 2-3 substantively "
    "different approaches and they all failed; the right path depends on a "
    "preference the user hasn't stated; an action would be irreversible and "
    "you want confirmation of intent (not safety — that's the risk gate). "
    "Provide a clear one-sentence `question` summarizing what you need. "
    "Optionally, provide an `options` list of 2-3 concrete next-step "
    "alternatives the user can click; each option must have a short "
    "`title`, a brief `description`, and a `tool_name` + `arguments` shape "
    "for the action that will run if the user picks it. Without options, "
    "the user simply replies in chat. Do NOT call this on every error — "
    "first attempt to reason about the failure yourself."
)
# Pure JSON Schema (no Pydantic model needed — the loop parses args defensively).
_ASK_USER_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "One-sentence summary of what you need from the user.",
        },
        "options": {
            "type": "array",
            "description": "Optional 2-3 alternatives the user can click. Omit for free-form.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Short stable id, e.g. 'sudo'."},
                    "title": {"type": "string", "description": "Card label (3-5 words)."},
                    "description": {
                        "type": "string",
                        "description": "One sentence: why this might work.",
                    },
                    "tool_name": {"type": "string", "description": "The tool to call if picked."},
                    "arguments": {"type": "object", "description": "Args for that tool."},
                },
                "required": ["title", "tool_name", "arguments"],
            },
            "minItems": 0,
            "maxItems": 3,
        },
    },
    "required": ["question"],
}


def _ask_user_tool_spec():
    """Lazy-imported ToolSpec for ask_user — avoids a circular import on
    module load (ToolSpec lives in ..llm which doesn't yet exist when this
    module imports)."""
    from ..llm.types import ToolSpec

    return ToolSpec(
        name=_ASK_USER_TOOL_NAME,
        description=_ASK_USER_DESCRIPTION,
        parameters_schema=_ASK_USER_PARAMETERS_SCHEMA,
    )


# The propose_plan_update tool — the model's auto-recovery affordance for
# when its current plan is no longer right. Call this when a step fails in a
# way that invalidates the path, OR when discoveries during execution suggest
# a different decomposition is better, OR when the user steers toward a new
# goal. The user accepts/refines/rejects via the existing plan-approval gate.
_PROPOSE_PLAN_UPDATE_DESCRIPTION = (
    "Propose a REVISED plan when the current plan is no longer the right path. "
    "Use this when: a step has failed in a way that means the whole plan needs "
    "rethinking; you've discovered something during execution that suggests a "
    "different decomposition; or the user's steer message implies a new goal. "
    "The user will see the new plan in the chat alongside the prior one (it "
    "slots chronologically), and chooses to Approve, Refine, or implicitly "
    "reject by sending a different message. Provide a `summary` (one sentence "
    "describing what changed and why), an ordered list of `steps` (each with a "
    "`title`), and an optional `context` markdown body explaining your "
    "reasoning. Do NOT call this on every error — only when the current plan "
    "is structurally wrong. Small course corrections inside a single step "
    "should be handled with another tool call."
)
_PROPOSE_PLAN_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence: what changed in the plan and why.",
        },
        "steps": {
            "type": "array",
            "description": "The new ordered list of steps.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title"],
            },
            "minItems": 1,
        },
        "context": {
            "type": "string",
            "description": "Optional markdown body: what you learned, why this plan now.",
        },
    },
    "required": ["summary", "steps"],
}


def _propose_plan_update_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="propose_plan_update",
        description=_PROPOSE_PLAN_UPDATE_DESCRIPTION,
        parameters_schema=_PROPOSE_PLAN_UPDATE_SCHEMA,
    )


# Lazy singletons — built on first access so module-level import order stays clean.
_ASK_USER_TOOL_SPEC = None
_PROPOSE_PLAN_UPDATE_TOOL_SPEC = None


def _ask_user_tool_singleton():
    global _ASK_USER_TOOL_SPEC
    if _ASK_USER_TOOL_SPEC is None:
        _ASK_USER_TOOL_SPEC = _ask_user_tool_spec()
    return _ASK_USER_TOOL_SPEC


def _propose_plan_update_tool_singleton():
    global _PROPOSE_PLAN_UPDATE_TOOL_SPEC
    if _PROPOSE_PLAN_UPDATE_TOOL_SPEC is None:
        _PROPOSE_PLAN_UPDATE_TOOL_SPEC = _propose_plan_update_tool_spec()
    return _PROPOSE_PLAN_UPDATE_TOOL_SPEC


# GAP B turn-taking tools — notify (non-blocking progress / mid-run reply) and
# finish (the explicit, affirmative terminal move). Both are intercepted by the
# loop and never reach the executor.
_NOTIFY_USER_DESCRIPTION = (
    "Send the user a NON-BLOCKING message — a progress update, an explanation of "
    "what you're doing, or a reply to something they said mid-run. The run does "
    "NOT stop; you keep working on your next step right after. Use this freely to "
    "narrate and to answer the user — it is the correct way to 'talk' during a "
    "build. (For a question you genuinely need answered before continuing, use "
    "`ask_user` instead, which halts.)"
)
_NOTIFY_USER_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string", "description": "The message to the user."}},
    "required": ["message"],
}
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
    "rooted at `path`) — make sure a server is actually serving it (the workspace "
    "auto-serves on the preview port; for a subfolder like dist/ start a server "
    "yourself first). Set `kind`='files' for artifacts to download (`path` = the "
    "file or folder). Give a short human `title`. This does NOT end the run — "
    "serve the deliverable, verify it, THEN call finish."
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
    "fails, the run does NOT finish and you'll see exactly what broke."
)
_FINISH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Short summary of what was accomplished."},
        "verify": {
            "type": "string",
            "description": (
                "Optional check that proves completion (exit 0 = success). Either a "
                "shell command (`npm test`, `python -m pytest -q`, `curl -fsS "
                "localhost:8000/`), OR for a STATIC page use the literal `static` "
                "(checks index.html exists + parses) or `static:<path>` for another "
                "file — NO server needed. If it fails, the finish is refused and you "
                "must fix the problem."
            ),
        },
    },
    "required": [],
}


def _notify_user_tool_spec():
    from ..llm.types import ToolSpec

    return ToolSpec(
        name="notify_user",
        description=_NOTIFY_USER_DESCRIPTION,
        parameters_schema=_NOTIFY_USER_SCHEMA,
    )


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


_NOTIFY_USER_TOOL_SPEC = None
_FINISH_TOOL_SPEC = None
_REMEMBER_TOOL_SPEC = None
_SERVE_TOOL_SPEC = None


def _notify_user_tool_singleton():
    global _NOTIFY_USER_TOOL_SPEC
    if _NOTIFY_USER_TOOL_SPEC is None:
        _NOTIFY_USER_TOOL_SPEC = _notify_user_tool_spec()
    return _NOTIFY_USER_TOOL_SPEC


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
    ) -> None:
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
        self._lock = asyncio.Lock()
        # Watch-it-write sink (optional). The runtime wires this to the store's
        # ephemeral broadcast; when set, the driver's streamed tool-call arg
        # fragments are decoded into growing file-content frames and published
        # live (display-only, never persisted). None → no streaming (tests, CLI).
        self.stream_sink: Callable[[dict], None] | None = None

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

    def _tools_for_step(self) -> list:
        """Mode-scoped tool visibility. With no planning_tools configured this is a
        pass-through (Research / default). While PLANNING the agent sees ONLY the
        planning tool(s); while executing it sees everything else.

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

            return [t for t in tools if _planner_ok(getattr(t, "name", None))]
        if self._planning_tools:
            tools = [t for t in tools if getattr(t, "name", None) not in self._planning_tools]
        # Append the virtual ask_user + propose_plan_update tools in execution
        # mode. Both are documented so the model decides WHEN to use them;
        # neither is injected by reminder. propose_plan_update is the model's
        # auto-recovery affordance: when its current plan is wrong, it proposes
        # a revision and the user accepts/refines via the plan-approval gate.
        tools = list(tools) + [
            _ask_user_tool_singleton(),
            _propose_plan_update_tool_singleton(),
            _notify_user_tool_singleton(),
            _finish_tool_singleton(),
            _remember_tool_singleton(),
            _serve_tool_singleton(),
        ]
        return tools

    def _plan_from_args(self, arguments: dict, events: list[Event]) -> PlanEvent:
        """Build a PlanEvent from a `submit_plan` tool call. Defensive against the
        model's shape drift (steps as dicts or bare strings); revision counts prior
        plans so a re-plan is visibly the next iteration. `context` carries any
        markdown rationale / exploration findings the planner included."""
        steps: list[PlanStep] = []
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
        revision = 1 + sum(1 for e in events if isinstance(e, PlanEvent))
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
        if tc is None or tc.tool_name not in ("shell", "code_exec"):
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
    def _consecutive_noops(events: list[Event]) -> int:
        """Count trailing agent prose MessageEvents not separated by an
        ActionEvent or a USER message. Resets when the agent acts or the user
        speaks. The GAP B backstop reads this to stop a talk-without-acting loop."""
        count = 0
        for e in reversed(events):
            if isinstance(e, ActionEvent):
                break
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                break
            if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
                count += 1
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
        done: set[int] = set()
        for e in events:
            if not isinstance(e, ActionEvent) or e.tool_call is None:
                continue
            if e.tool_call.tool_name != "plan_step":
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

    async def _materialize_view(self, events: list[Event]) -> View:
        view = View.of(events)
        est = self._estimate_tokens(view)
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
        return view

    async def _hard_reset(self, events: list[Event]) -> bool:
        """Forget-and-summarize after a context-window error (§8). Returns True
        if a tombstone was appended (progress made)."""
        tombstone = await self.condenser.condense(
            events, View.of(events), summarizer=self.summarizer
        )
        if tombstone is not None:
            await self._emit(tombstone)
            return True
        return False

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

        If the sandbox transparently RECREATED itself during this call (a mid-
        session death the session healed), append an implicit system-reminder
        so the model knows files-on-disk remain but processes/state were lost."""
        sbx = getattr(self.executor, "sandbox", None)
        gen_before = getattr(sbx, "generation", 0) if sbx is not None else 0
        try:
            result = await self.executor.execute(action.tool_call)
        except LLMContextWindowExceeded:
            raise  # handled by view-materialization hard-reset (§8)
        except Exception as e:  # noqa: BLE001 — any tool/exec failure is observable
            await self._emit(AgentErrorEvent(error=str(e), action_id=action.id))
            await self._maybe_emit_sandbox_restart(sbx, gen_before)
            return
        if result.success:
            await self._emit(ObservationEvent(tool_result=result, action_id=action.id))
        else:
            await self._emit(
                AgentErrorEvent(error=result.error or "tool failed", action_id=action.id)
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
        action = ActionEvent(thought=f"Verifying completion: {command}", tool_call=call)

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
            return False

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
            return False

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
        if not passed:
            await self._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            f"You called finish, but the verify command `{command}` did not "
                            "pass (see the result above). The task is NOT complete. Fix what "
                            "it surfaced, then finish again — or finish without a verify "
                            "command if the check itself is wrong.\n"
                            "</system-reminder>"
                        ),
                    ),
                )
            )
        return passed

    async def _stop_allowed(self, state: ConversationState, events: list[Event]) -> bool:
        for hook in self._stop_hooks:
            if not await hook.allow_stop(state, events):
                return False
        return True

    # ---- the run loop (§4) --------------------------------------------------

    async def run(self) -> ConversationState:
        """Drive until a terminal-for-now status. Idempotent to call again after
        a pause/confirmation. [CONTRACT] returns the resulting ConversationState."""
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

                # (c) stuck detection BEFORE more work (§6)
                if self._stuck.is_stuck(self._recent(events)):
                    await self._emit(StatusEvent(status=ConversationStatus.STUCK))
                    return await self.get_state()

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
                    await self._emit(
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(
                                role="user",
                                content=(
                                    "<system-reminder>\n"
                                    f"You've failed {fails} times in a row:\n{errs}\n\n"
                                    "STOP retrying blindly. Call `ask_user` NOW with: a "
                                    "1–2 sentence DIAGNOSIS of what is actually blocking you "
                                    "as the `question`, and 2–3 concrete recovery `options`, "
                                    "each a SPECIFIC tool action that CHANGES the approach "
                                    "(not a repeat of what just failed). The user will pick "
                                    "one — or let you continue.\n"
                                    "</system-reminder>"
                                ),
                            ),
                        )
                    )
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING, detail="recovery_requested"
                        )
                    )
                    continue
                if fails > self._circuit_breaker_threshold:
                    # Recovery was already requested AND it failed again → NOW hand off
                    # with a plain summary (the old behavior) so it can't loop forever.
                    summary = (
                        f"I've hit {fails} failures in a row and couldn't find a way "
                        "through. Pausing for your direction. The recent errors were:\n"
                        + "\n".join(f"  • {err[:200]}" for err in recent_errors[:4])
                        + "\n\nHow would you like me to proceed?"
                    )
                    await self._emit(
                        MessageEvent(
                            source=EventSource.AGENT,
                            message=LLMMessage(role="assistant", content=summary),
                        )
                    )
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.AWAITING_USER_DECISION,
                            detail="circuit_breaker",
                        )
                    )
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

                # (d) build the model-facing View, condensing if triggered (§8)
                view = await self._materialize_view(events)

                # (e) ask the agent for ONE action (principle 1). The visible tool
                # set is mode-scoped: while PLANNING the agent sees ONLY the plan
                # tool (so it can't act before approval); while executing it sees
                # everything except the plan tool.
                try:
                    step = await self.agent.step(
                        view,
                        self._tools_for_step(),
                        mode=self.mode,
                        overflow_signal=self._overflow_signal(events),
                        on_stream=self._build_stream_hook(),
                    )
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
                    continue  # non-blocking — keep working
                if step.tool_call is not None and step.tool_call.tool_name == "remember":
                    # Durable memory: emit a PINNED KnowledgeEvent so the fact
                    # survives condensation and is re-injected into context every
                    # step. Non-blocking — like notify_user, the agent keeps
                    # working right after. A blank fact is ignored (no-op).
                    fact = str(step.tool_call.arguments.get("fact") or "").strip()
                    if fact:
                        scope = str(step.tool_call.arguments.get("scope") or "").strip()
                        await self._emit(
                            KnowledgeEvent(source=EventSource.AGENT, scope=scope, snippet=fact)
                        )
                    continue  # non-blocking — keep working
                if step.tool_call is not None and step.tool_call.tool_name == "serve":
                    # Finished-artifact HANDOFF: emit a DeliverableEvent the UI renders
                    # as Open-the-app / Download-the-files. Non-blocking — the agent
                    # serves, verifies, then finishes. A missing path/title is ignored
                    # (no-op) rather than emitting a useless handoff.
                    title = str(step.tool_call.arguments.get("title") or "").strip()
                    path = str(step.tool_call.arguments.get("path") or "").strip()
                    kind = str(step.tool_call.arguments.get("kind") or "app").strip()
                    if kind not in ("app", "files"):
                        kind = "app"
                    if title and path:
                        await self._emit(
                            DeliverableEvent(
                                source=EventSource.AGENT,
                                title=title,
                                path=path,
                                artifact_kind=kind,  # type: ignore[arg-type]
                            )
                        )
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
                    if verify_cmd and not await self._finish_verify_passed(verify_cmd):
                        continue  # verification failed/refused — keep working
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
                        self._execution_nudges += 1  # telemetry only; not a gate
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(role="user", content=_EXECUTION_NUDGE),
                            )
                        )
                        continue
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
                    noops = self._consecutive_noops(await self._events())
                    if noops >= self._max_consecutive_noops:
                        # The model is talking without acting and won't stop —
                        # end cleanly rather than spin. (A real run resumes on a
                        # user steer; the prompt steers toward finish/act.)
                        await self._emit(
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(
                                    role="user",
                                    content=(
                                        "<system-reminder>\n"
                                        f"You have sent {noops} messages in a row without "
                                        "calling a tool. Ending the run. To continue, the "
                                        "user can send a new instruction; otherwise call a "
                                        "tool to act or `finish` to complete.\n"
                                        "</system-reminder>"
                                    ),
                                ),
                            )
                        )
                        await self._emit(
                            StatusEvent(
                                status=ConversationStatus.FINISHED, detail="noop_limit"
                            )
                        )
                        return await self.get_state()
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
                if (
                    step.tool_call is not None
                    and step.tool_call.tool_name == "propose_plan_update"
                ):
                    new_plan = self._plan_from_args(step.tool_call.arguments, events)
                    await self._emit(new_plan)
                    await self._emit(
                        StatusEvent(
                            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
                            detail=new_plan.id,
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
                if self.policy.should_confirm(risk):
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
