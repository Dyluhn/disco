"""Rendering owners for the static reminder / message builders.

This module is the single bounded implementation owner for the pure helpers
extracted from the former ``messages`` god-file: the LLM-error renderer, the
workspace-path working-set selector, the HS-03 facts re-ground recap, the
re-plan digest, and the stuck-escape reminder pool.  The former ``messages``
module re-exports these symbols so every historical import path, signature,
prompt byte, ordering rule, and sentinel is preserved.

All functions are pure over their inputs: no loop or ``self`` state, no model
calls, no emission.  The cadence/temperature constants that GATE them and the
workspace-snapshot size caps stay in their original owners; only the constants
used EXCLUSIVELY by the moved builders moved with them.
"""

from __future__ import annotations

import json

from ..dod import predicate_to_dict
from ..events import ActionEvent, Event, EventSource, LLMMessage, MessageEvent
from ..llm import LLMError
from ..view import _latest_plan, effective_plan_progress
from .dedup import _WORKSPACE_MUTATING_TOOLS, _WORKSPACE_READ_TOOLS
from .resource_context import canonical_workspace_identifier

# (B2/B6) Cap on consecutive PLANNING-mode read/list tool calls before the loop
# forces a plan. A re-plan model can stay in an "execution frame of mind" and
# explore (file_read/file_list/search) indefinitely without ever proposing a
# plan — the live trace showed ~17 reads and no submit_plan, so the revise
# spinner hung forever. After this many consecutive planning reads with no
# submit_plan, ONE forcing reminder is injected (append-once per threshold
# crossing). Reads are still legitimate Phase-1 context-gathering BELOW the cap.
_PLAN_EXPLORE_READ_CAP = 5

# Injected ONCE when the planner hits _PLAN_EXPLORE_READ_CAP consecutive
# planning-mode reads without proposing a plan. Unlike _PLAN_NUDGE (which fires
# on a tool-LESS prose turn) this fires on productive-but-endless exploration.
_PLAN_EXPLORE_FORCE = (
    "<system-reminder>\n"
    "You've explored {n} files in PLANNING mode without proposing a plan. You "
    "have enough context — call `submit_plan` now with your revised plan. Do NOT "
    "start editing files; in PLANNING mode `submit_plan` is your only terminal "
    "move.\n"
    "</system-reminder>"
)

_WORKFLOW_ROUTER_EXPLORE_FORCE = (
    "<system-reminder>\n"
    "You are in WORKFLOW ROUTER phase and have enough context. Your next response "
    "must be exactly one tool call: `enter_workflow`, `needs_input`, or "
    "`draft_workflow`. If you are already inside a workflow run and the selected "
    "workflow cannot satisfy the goal, call `workflow_abort`.\n"
    "</system-reminder>"
)

# (B2/B6) Injected when RE-entering planning after a build was already approved
# (a revision, not a first plan). Frames the turn so the model proposes a
# revised plan instead of free-building against the old plan.
_REPLAN_FRAMING = (
    "<system-reminder>\n"
    "RE-PLANNING: the user added a new instruction to an existing, already-approved "
    "build.\n\n"
    "Your CURRENT approved plan:\n"
    "{current_plan}\n\n"
    "The user's new instruction:\n"
    "  {instruction}\n\n"
    "Produce a REVISED plan that folds the new instruction into the steps above by "
    "CALLING the `submit_plan` tool with the FULL revised step list — e.g. "
    'submit_plan(summary="…", steps=[{{"title":"…"}}]). '
    "Keep any advisory done_condition that is still useful, but freely replace or omit "
    "one when the implementation layout changes; external acceptance requirements are "
    "owned separately. Keep the steps you have already completed "
    "and add/adjust steps for the new instruction. Do NOT start editing files yet. A "
    "prose description of the plan does NOT register — ONLY a `submit_plan` tool call "
    "does; if you only describe it in text the build stays stuck in planning. The plan "
    "revision number will increment.\n"
    "</system-reminder>"
)

# Ceiling on inline step titles in the re-plan framing — a LAST-RESORT guardrail against a
# pathological plan, NOT default truncation: the whole point is to show the FULL plan so the
# model revises it completely (truncating risks the model DROPPING unseen steps). Titles are
# cheap (one short line each), so this is set high; if it ever trips, the model is told the
# omitted steps must be preserved.
_REPLAN_DIGEST_MAX_STEPS = 80
_REPLAN_DIGEST_TITLE_CAP = 200


def _render_replan_plan_digest(plan: object) -> str:
    """Compact inline digest of the CURRENT plan for the re-plan framing — summary + the
    FULL list of step titles and typed predicates — so the model revises the actual
    approved contract instead of reconstructing it from a long post-build history (and
    narrating it in prose). Only a pathological plan (> `_REPLAN_DIGEST_MAX_STEPS`) is
    truncated, and then EXPLICITLY so the model preserves the omitted steps."""
    steps = getattr(plan, "steps", None) or []
    rev = getattr(plan, "revision", 1)
    summary = (getattr(plan, "summary", "") or "(no summary)").strip()
    lines = [f"  Plan (revision {rev}): {summary}"]
    for i, s in enumerate(steps[:_REPLAN_DIGEST_MAX_STEPS], start=1):
        title = (getattr(s, "title", "") or "").strip()[:_REPLAN_DIGEST_TITLE_CAP]
        lines.append(f"    {i}. {title}")
        detail = (getattr(s, "detail", "") or "").strip()
        if detail:
            lines.append(f"       detail: {detail[:_REPLAN_DIGEST_TITLE_CAP]}")
        predicate = getattr(s, "done_condition", None)
        if predicate is not None:
            rendered = json.dumps(
                predicate_to_dict(predicate),
                sort_keys=True,
                separators=(",", ":"),
            )
            lines.append(f"       done_condition: {rendered}")
    if len(steps) > _REPLAN_DIGEST_MAX_STEPS:
        lines.append(
            f"    … (+{len(steps) - _REPLAN_DIGEST_MAX_STEPS} more existing steps not "
            "shown here — PRESERVE them in your revised plan)"
        )
    return "\n".join(lines)


def _latest_user_instruction(events: list[Event]) -> str | None:
    """The text of the most recent USER MessageEvent, or None if there is
    none. (B2/B6) Used to re-ground on the latest instruction while RE-planning
    instead of anchoring to the original build GOAL (the superseded plan's
    summary)."""
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            text = (e.message.content or "").strip()
            if text:
                return text
    return None


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


# Inputs the host placed in the workspace for the agent to *read*: bound reference
# packs (`references/<slug>/…`) and the durable context files (`.disco/…`). They are
# static, often large (Pharmacy run 7: 142 KB of pack text in a 207 KB snapshot, every
# turn) and one read away; the snapshot is for the files the agent is building.
_SNAPSHOT_INPUT_PREFIXES = ("references/", ".disco/")


def _workspace_paths_from_events(events: list[Event]) -> tuple[list[str], list[str]]:
    """Return (mutated, read_only) working-set paths, each de-duplicated and
    MOST-RECENT FIRST. A path counted as mutated if it was EVER written/edited —
    a write outranks a later read, so the deliverable files the agent is building
    are never pushed out of the snapshot window by read-heavy exploration
    (steelman finding #1). Mirrors Aider's "files in the chat" — bounded +
    relevant. Reads the raw event list directly, so a file touched long ago (and
    since forgotten/condensed from the View) still counts: it is still on disk.
    Host-placed inputs (reference packs, `.disco/` context) never enter the
    read-only bucket; a file the agent wrote there still counts as mutated."""
    mutated: dict[str, None] = {}
    read_only: dict[str, None] = {}
    for e in events:
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue  # defensive: a tool_call-less ActionEvent (finish mirror) has no path
        name = e.tool_call.tool_name
        raw_path = e.tool_call.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        p = canonical_workspace_identifier(raw_path)
        if name in _WORKSPACE_MUTATING_TOOLS:
            read_only.pop(p, None)  # promote a previously read-only file to mutated
            mutated.pop(p, None)
            mutated[p] = None  # most-recent-wins
        elif name in _WORKSPACE_READ_TOOLS:
            # Reads change knowledge, not workspace state. Once a path is in
            # either bucket, re-reading it must not reorder the cacheable
            # CURRENT WORKSPACE prefix (or change which files fit its cap).
            # The just-completed read remains high-attention in the causal tool
            # result; snapshot order advances only on a real mutation action.
            if p.startswith(_SNAPSHOT_INPUT_PREFIXES):
                continue  # reference packs and context files are inputs, not the build
            if p not in mutated and p not in read_only:
                read_only[p] = None
    return list(reversed(mutated.keys())), list(reversed(read_only.keys()))


# The view.py tag for the re-ground recap. A message that starts with this
# sentinel IS the re-ground; the predicate and the tests inspect this tag
# to count emits and assert content shape. Kept short and distinct from
# _RECITATION_SENTINEL so the two recaps are unambiguous in the View.
_HS03_REGROUND_SENTINEL = "<reground-anchors>"
# Hard cap on the per-section body length inside the recap, so a long plan
# summary or many recently-touched files can't bloat the re-ground into a
# second prompt. The whole recap stays well under ~1k chars.
_HS03_REGROUND_SECTION_CHARS = 200
_HS03_REGROUND_MAX_FILES = 4  # only the most recent few files in the recap


def _hs03_clip(s: str, cap: int = _HS03_REGROUND_SECTION_CHARS) -> str:
    s = (s or "").strip()
    if len(s) > cap:
        return s[: cap - 1] + "\u2026"
    return s


def _hs03_goal_line(plan: object, goal_override: str | None) -> str:
    """GOAL: plan summary (the one-line 'what this plan delivers'). (B2/B6)

    When re-grounding in PLANNING mode (a re-plan), ``goal_override`` carries the
    LATEST user instruction so the recap anchors to the NEW task instead of the
    superseded plan's summary — otherwise the old GOAL re-injection reinforces
    'keep executing the old plan' and the model never re-plans.
    """
    goal = _hs03_clip(
        (goal_override or "").strip() or getattr(plan, "summary", "") or "(no summary)"
    )
    return f"GOAL: {goal}"


def _hs03_progress_block(plan: object, events: list[Event]) -> str:
    """PROGRESS: per-step checklist. Mirrors the tail recap's MERGED accounting
    (plan_step + update_plan_progress, see effective_plan_progress), FACTS ONLY —
    '✓ 1. step title', no 'next incomplete' pointer (recap, not steer). This recap is
    PERSISTED as an environment message, so a plan_step-only count would re-inject a
    stale 'incomplete' plan into context for a capable model that already marked it done.
    """
    _, states = effective_plan_progress(events)
    done = {i for i, st in states.items() if st == "done"}
    active = {i for i, st in states.items() if st == "active"}
    step_lines: list[str] = []
    steps = getattr(plan, "steps", []) or []
    for i, step in enumerate(steps, start=1):
        if i in done:
            mark = "✓"
        elif i in active:
            mark = "→"
        else:
            mark = "□"
        step_lines.append(f"  {mark} {i}. {step.title}")
    progress = _hs03_clip("\n".join(step_lines))
    return f"PROGRESS ({len(done)}/{len(steps)} done):\n{progress}"


def _hs03_files_block(events: list[Event]) -> str:
    """FILES: recently-touched paths, names only. Mutating paths first
    (the deliverable), then read-only (the exploration surface). Capped
    at _HS03_REGROUND_MAX_FILES so a 50-file sweep doesn't push the
    recap into a second prompt.
    """
    mutated, read_only = _workspace_paths_from_events(events)
    file_paths: list[str] = list(mutated[:_HS03_REGROUND_MAX_FILES])
    remaining = _HS03_REGROUND_MAX_FILES - len(file_paths)
    if remaining > 0:
        file_paths.extend(read_only[:remaining])
    files_block = "\n".join(f"  - {p}" for p in file_paths) if file_paths else "  (none yet)"
    return f"FILES:\n{files_block}"


def _hs03_constraints_line(plan: object) -> str | None:
    """CONSTRAINTS: the plan's ``context`` field, when present — the
    planner's exploration findings + trade-offs (Claude-Code-style
    rationale). Empty for plans that skipped the rationale, in which
    case the section is omitted entirely (no '(no constraints)' stub
    — silence is cheaper than noise). Clipped so a long rationale
    doesn't blow the budget.
    """
    context = getattr(plan, "context", "") or ""
    if not context:
        return None
    return f"CONSTRAINTS: {_hs03_clip(context)}"


def _hs03_reground_message(
    events: list[Event], *, goal_override: str | None = None
) -> LLMMessage | None:
    """HS-03 — build a short RECAP of the stable facts (goal, plan state,
    recently-touched files, optional constraints from the plan's
    exploration context). PURE function over the event log — no model
    call, no emission. Returns None when there is no plan to recap (a
    re-ground without a plan has nothing to anchor to; the calling
    gate drops the emit).

    The text is FACTS ONLY — the GOAL is stated as a present-tense
    declarative ("Goal: ship the page"), the progress is a checklist
    with checkmarks, the files are a list. There is NO "you should",
    "next, do", "call X" — that would be a steer and would violate
    the no-automatic-nudge invariant (commit c97c1b3). The HS-03
    recap is a passive reminder of facts the model should already
    know; it does not direct behavior.

    The re-ground is built from three event-derived sources, in this
    order — each is bounded so a runaway log doesn't bloat the
    reminder into a second prompt:
      1. Latest PlanEvent — goal summary, exploration `context` (the
         planner's "findings + constraints" markdown; empty for plans
         that skipped the rationale), per-step checkmarks (mirrors
         _recitation_signature's done/active accounting).
      2. _workspace_paths_from_events — top-N most-recently-touched
         files (mutating tools first, then read-only), names only (no
         body — the snapshot is the authoritative current state).
      3. Concise fallback: when no plan exists, the recap is None and
         the calling gate drops the emit. (A re-ground of "remember
         the user said hello" would be cargo-cult; without a plan
         there is no goal to anchor to.)

    Length budget: each section is clamped to _HS03_REGROUND_SECTION_CHARS
    (~200) so a long plan summary or many files can't push the recap past
    a few hundred chars total. The sentinel-tagged wrapper adds < 50
    chars. The full message stays under ~1k chars — a single, easily-
    skipped block in the View, not a competing prompt.
    """
    plan = _latest_plan(events)
    if plan is None or not plan.steps:
        return None

    # Assemble. Order: GOAL → CONSTRAINTS → PROGRESS → FILES. The order
    # matches the HS-02 anchored template (which the brief cites) so a
    # reader familiar with HS-02 sees the same shape. We omit
    # DECISIONS / NEXT (no first-class field for decisions in the
    # log, and NEXT would be a steer).
    sections: list[str] = [_hs03_goal_line(plan, goal_override)]
    constraints = _hs03_constraints_line(plan)
    if constraints is not None:
        sections.append(constraints)
    sections.append(_hs03_progress_block(plan, events))
    sections.append(_hs03_files_block(events))

    body = f"{_HS03_REGROUND_SENTINEL}\n" + "\n".join(sections) + f"\n{_HS03_REGROUND_SENTINEL}"
    return LLMMessage(role="user", content=body)


# C7 — escape-reminder pool + serialization seed. A small fixed pool of
# `<system-reminder>` phrasings selected by attempt count (modulo the pool
# length) so consecutive escape attempts are NOT byte-identical. The
# pool is intentionally small (5 entries) — enough that the model sees
# a different angle each time, but small enough to keep the system prompt
# footprint predictable. The last two entries carry error-recovery guidance
# (search-to-escape + environment-vs-your-code) so the model is reminded to
# look the error up and to tell an environment problem from a code bug.
# A per-attempt nonce is embedded as a hidden
# comment-style suffix so the reminder is identifiable in tests (the model
# ignores HTML comments) AND differs in bytes between attempts. Deterministic
# under a fixed attempt count: index = attempt_count % len(_STUCK_ESCAPE_REMINDER_POOL),
# nonce = attempt_count. The escape path is the ONLY consumer of this pool
# (c97c1b3 — no automatic nudge outside the existing stuck-escape).
_STUCK_ESCAPE_REMINDER_POOL: tuple[str, ...] = (
    "<system-reminder>\n"
    "You've been repeating the same action. STOP and take a different "
    "approach — a different tool, a different argument shape, or a "
    "different sub-task entirely. Do not retry what just failed. "
    "If the work is already complete, do NOT re-verify by re-reading "
    "unchanged files — call `serve`/`finish` now.\n"
    "During this read-loop recovery, general shell/code execution and delegated "
    "exploration are also unavailable: do not use them to reread the same bytes "
    "under another name.\n"
    "Before you move, `think` for one line about WHY the last attempt failed, "
    "then pick a genuinely different action.\n"
    "<!-- disco:escape-attempt=0 -->\n"
    "</system-reminder>",
    "<system-reminder>\n"
    "The previous retry didn't work either. Pivot: re-read the most recent "
    "error, identify the SPECIFIC thing that went wrong, and change exactly "
    "that. Do not echo the same tool call with the same arguments.\n"
    "<!-- disco:escape-attempt=1 -->\n"
    "</system-reminder>",
    "<system-reminder>\n"
    "Self-imitation detected: the last few steps look like copies of one "
    "another. Break the pattern. Try a tool you haven't used in this turn, "
    "or attack a different angle of the problem. If nothing else works, "
    "declare the blocker and call `finish` honestly.\n"
    "<!-- disco:escape-attempt=2 -->\n"
    "</system-reminder>",
    "<system-reminder>\n"
    "Same error again? Stop retrying from memory. Use the `search`/`extract` "
    "tools to look up the EXACT error text or the API you're using, then apply "
    "what you find and try again. Never conclude something is impossible before "
    "you've searched for it.\n"
    "<!-- disco:escape-attempt=3 -->\n"
    "</system-reminder>",
    "<system-reminder>\n"
    "Diagnose the LAYER before you retry: is this an ENVIRONMENT problem "
    "(sandbox, network, a missing tool, a platform limit) or a bug in YOUR "
    "code? If it's the environment, work AROUND it with a different approach "
    "or path instead of fighting it; if it's your code, the root cause is "
    "usually in the code under test, not the test.\n"
    "<!-- disco:escape-attempt=4 -->\n"
    "</system-reminder>",
)


def _stuck_escape_reminder(attempt_count: int) -> str:
    """Return the escape reminder for the given attempt count.

    Selection: `_STUCK_ESCAPE_REMINDER_POOL[attempt_count % len(POOL)]`.
    Deterministic under a fixed attempt_count (testable). The pool rotates
    so consecutive attempts are NOT byte-identical, breaking the
    self-imitation chain that a single fixed reminder would invite.
    """
    idx = attempt_count % len(_STUCK_ESCAPE_REMINDER_POOL)
    return _STUCK_ESCAPE_REMINDER_POOL[idx]
