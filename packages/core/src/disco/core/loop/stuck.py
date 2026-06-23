"""Stuck detection — agent-loop-contract.md §6 (re-implemented from the SDK).

Checked every iteration before stepping. Pure function of the recent event
window; uses `event_content_eq` (event contract §6.3) so byte-differences in
ids/timestamps never mask a semantic loop.

Four patterns are implemented (BoD §12.5 patterns 1–4). Pattern 5 (the
context-window-error loop) is known-hard and is NOT detected here; its cause is
prevented by the hard-reset condensation (§8) and bounded by `max_iterations`.

F6 (patch-spiral detector, gated on assist): tracks per-file edit failures +
total attempts; when a file crosses the threshold and assist is ON, the richer
`StuckResult` returned by `evaluate()` carries a `RewriteDirective` that names
the spiraling file. The engine consumes the directive via the existing stuck
path (the new fields are ignored when the engine has not been wired to them
yet — see `is_stuck()` which is the byte-identical bool facade). Assist OFF
→ the directive is always None, and the bool facade is unchanged.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel

from ..equality import event_content_eq
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
)

# F6 — tools whose ActionEvents count as "patch attempts" for the per-file
# rewrite tracker. Mirrors `_WORKSPACE_MUTATING_TOOLS` in engine.py (kept
# inline here so stuck.py stays self-contained — engine.py is held by another
# worker and stuck.py must not import from it). A new mutating tool added in
# one place must be added in the other.
_F6_FILE_MUTATING_TOOLS = frozenset({
    "file_write",
    "file_edit",
    "file_append",
    "file_replace_lines",
    "file_insert_lines",
})

# WALK-19 — probe/verify tools whose ObservationEvent carries the "what does the
# running app actually look like now" signal. When this signal is unchanged
# across many DISTINCT edits, the model is making successful-but-useless edits
# (the black-screen-game). `serve` is intercepted by the engine into a
# DeliverableEvent (no ObservationEvent), so it is not a probe here.
_NO_PROGRESS_PROBE_TOOLS = frozenset({"browser", "server_status", "deploy_preview"})
# Distinct varied edits that must recur against ONE stable probe outcome before
# the no-progress breaker trips. 4 mirrors the circuit-breaker's failure budget.
NO_PROGRESS_DISTINCT_EDITS = 4

# W1 — wait/poll tools exempted from patterns 1 and 4 (but NOT 2): a legit
# "poll until server up" loop must not be flagged as stuck (OpenHands #5355 FP
# class). Pattern 2 keeps them: a perpetually-erroring poll IS stuck.
_WAIT_POLL_TOOLS = frozenset({
    "sleep", "wait", "server_status", "poll", "browser_wait", "job_status", "deploy_status"
})

# W1 — plan/meta tools managed by the dedicated bookkeeping halt
# (turn_control.py gate_bookkeeping_streak). Excluded from patterns 1 and 4
# to avoid double-firing with the bookkeeping gate. Mirrors _BOOKKEEPING_TOOLS
# in signals.py — kept inline so stuck.py stays self-contained; a new tool
# added to _BOOKKEEPING_TOOLS must also be added here.
_PLAN_META_TOOLS = frozenset(
    {"submit_plan", "propose_plan_update", "plan_step", "update_plan_progress", "finish"}
)


class StuckThresholds(BaseModel):
    repeat_action_observation: int = 4  # identical action→obs cycles (W1: raised 3→4)
    repeat_action_error: int = 3  # identical action→error cycles
    agent_monologue: int = 4  # consecutive agent msgs, no user
    alternating: int = 3  # A-B-A-B cycles
    scan_window: int = 20  # only inspect the last N events
    # F6 — per-file patch-spiral rewrite threshold. A file that has accumulated
    # at least `per_file_rewrite_failures` FAILED patch attempts AND at least
    # `per_file_rewrite_min_attempts` total patch attempts is "spiraling":
    # patch a few more lines at a time, watch it fail, repeat → a full rewrite
    # of the file is more likely to land than another surgical patch. Defaults
    # are 3 + 3 (three failed out of three attempts). 0 disables the tracker.
    per_file_rewrite_failures: int = 3
    per_file_rewrite_min_attempts: int = 3


class RewriteDirective(BaseModel):
    """F6 — the per-file rewrite signal surfaced by `StuckDetector.evaluate()`
    when assist is ON and a file's failure streak crosses the threshold.

    `kind` discriminates future directive shapes (today only "full_rewrite";
    a "rollback_to_baseline" or "skip_file" shape may be added later behind
    the same gate). `path` is the workspace-relative file to rewrite (taken
    verbatim from the failing ActionEvent's `path` argument). `failures` and
    `attempts` are the per-file counts at the moment the directive fired —
    the engine can log them for the audit trail.
    """

    kind: Literal["full_rewrite"] = "full_rewrite"
    path: str
    failures: int
    attempts: int


class StuckResult(BaseModel):
    """F6 — the richer return type of `StuckDetector.evaluate()`.

    `is_stuck` is the bool the existing stuck patterns (1–4) would have
    returned. `rewrite_directive` is None unless assist is ON AND a file
    has crossed the per-file threshold. The engine consumes `is_stuck` via
    `is_stuck()` (the bool facade) and can later be wired to read
    `rewrite_directive` from `evaluate()` without a behavior change for
    any existing caller. Assist OFF ⇒ `rewrite_directive` is always None.
    """

    is_stuck: bool
    # W-31 — the NAME of the first stuck pattern that fired (None when not
    # stuck). Stamped onto the gate_stuck STUCK StatusEvent's `detail` so logs/
    # the UI identify WHICH breaker halted the run instead of an undifferentiated
    # STUCK (Dylan had to infer it). Mirrors every sibling gate's `detail`.
    reason: str | None = None
    rewrite_directive: RewriteDirective | None = None


def _after_last_user_message(events: list[Event]) -> list[Event]:
    """Discard everything at/before the last USER MessageEvent — a new
    instruction means 'not stuck'."""
    last_user = -1
    for i, e in enumerate(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            last_user = i
    return events[last_user + 1 :]


def _consecutive_pairs(
    events: list[Event], first_type: type, second_type: type
) -> list[tuple[Event, Event]]:
    """Collect immediately-adjacent (first_type, second_type) event pairs."""
    pairs: list[tuple[Event, Event]] = []
    i = 0
    while i < len(events) - 1:
        a, b = events[i], events[i + 1]
        if isinstance(a, first_type) and isinstance(b, second_type):
            pairs.append((a, b))
            i += 2
        else:
            i += 1
    return pairs


class StuckDetector:
    """[CONTRACT] Pure stuck-pattern detection over the recent event window."""

    def __init__(
        self,
        thresholds: StuckThresholds | None = None,
        *,
        assist: bool = False,
    ) -> None:
        self.t = thresholds or StuckThresholds()
        # F6 — the assist gate. When False (default, today), the per-file
        # rewrite tracker is inert: `evaluate()` returns the same stuck bool
        # `is_stuck()` would have, with `rewrite_directive=None`. The engine
        # can later pass `assist=self._assist` to start getting the richer
        # signal. Adding the kwarg with a default is backwards-compatible —
        # every existing `StuckDetector(...)` call site is unchanged.
        self._assist = assist

    def is_stuck(self, recent: list[Event]) -> bool:
        """Byte-identical facade to `evaluate(...).is_stuck`. The engine calls
        this today; the per-file rewrite signal is exposed separately via
        `evaluate()` so the engine can be wired to consume it without
        changing the bool-return contract of this method."""
        return self.evaluate(recent).is_stuck

    def evaluate(self, recent: list[Event]) -> StuckResult:
        """The richer stuck signal. Returns the same stuck bool the four
        patterns have always returned, plus an optional F6 rewrite directive
        when assist is ON and a per-file patch-spiral is detected.

        Assist OFF ⇒ `rewrite_directive` is None and `is_stuck` matches the
        pre-F6 detector exactly (the per-file tracker is a no-op when the
        gate is closed).
        """
        recent = _after_last_user_message(recent)
        reason = self._stuck_reason(recent)
        rewrite_directive: RewriteDirective | None = None
        if self._assist:
            rewrite_directive = self._per_file_rewrite_directive(recent)
        return StuckResult(
            is_stuck=reason is not None,
            reason=reason,
            rewrite_directive=rewrite_directive,
        )

    def _stuck_reason(self, recent: list[Event]) -> str | None:
        """W-31 — name the FIRST stuck pattern that fires (or None). The order
        is the SAME short-circuit chain `evaluate` used before, so the stuck
        bool is byte-identical; we just additionally surface which pattern won
        so the gate_stuck STUCK emit can NAME the breaker. `recent` is already
        sliced to after the last user message by the caller."""
        if self._repeated_action_observation(recent):
            return "repeated_action_observation"  # pattern 1 (W-30/W-31: repeated reads)
        if self._repeated_action_error(recent):
            return "repeated_action_error"  # pattern 2
        if self._agent_monologue(recent):
            return "agent_monologue"  # pattern 3
        if self._alternating(recent):
            return "alternating_actions"  # pattern 4
        if self._pure_repeat(recent):
            return "pure_repeat"  # W1 pattern 5
        return None

    # -- W1 pattern 5: Roo-style pure-repeat (back-to-back identical actions) ----
    #
    # Catches a model that fires the same tool call repeatedly WITHOUT waiting
    # for a paired observation — e.g. a tight loop issuing back-to-back
    # file_read("x.py") with no observation in between. Pattern 1 requires
    # action→obs pairs; this pattern works on the raw non-wait action stream.
    # Uses ignore_thought=True so thought paraphrasing does not mask the loop.
    # Wait/poll and plan/meta tools are exempt (same rationale as patterns 1+4).

    def _pure_repeat(self, events: list[Event]) -> bool:
        threshold = self.t.repeat_action_observation
        _exempt = _WAIT_POLL_TOOLS | _PLAN_META_TOOLS
        actions = [
            e for e in events
            if isinstance(e, ActionEvent)
            and e.tool_call is not None
            and e.tool_call.tool_name not in _exempt
        ]
        if len(actions) < threshold:
            return False
        last = actions[-1]
        run = 0
        for x in reversed(actions):
            if event_content_eq(x, last, ignore_thought=True):
                run += 1
            else:
                break
        return run >= threshold

    # -- F6 pattern 5: per-file patch-spiral → rewrite directive --------------
    #
    # A weak model can patch-spiral a single file: try a small edit, watch it
    # fail, try a slightly different edit, watch it fail, … — none of those
    # cycles is byte-identical (so patterns 1–4 don't fire), but the file is
    # clearly stuck. Counting failures + total attempts PER FILE and crossing
    # a threshold yields a "switch to a full rewrite of <file>" signal. The
    # directive is a recommendation, not an automatic edit — the engine (or
    # a future Rung consumer) decides how to act on it.

    def _per_file_rewrite_directive(
        self, events: list[Event]
    ) -> RewriteDirective | None:
        # Gate closed at the threshold level (e.g. 0 ⇒ disabled) and at the
        # call site (assist ON). A 0 in either knob short-circuits the
        # bookkeeping, so a misconfigured threshold can't silently do work.
        if self.t.per_file_rewrite_failures <= 0:
            return None
        if self.t.per_file_rewrite_min_attempts <= 0:
            return None

        # Build a map action.id → (event, path) for every file-mutating
        # action in the window. `id` is the BaseEvent correlation key the
        # observation/error events use (ObservationEvent.action_id,
        # AgentErrorEvent.action_id). We only count mutating tools — a
        # `file_read` or `shell` call is not a patch attempt.
        actions_by_id: dict[str, tuple[ActionEvent, str | None]] = {}
        for e in events:
            if not isinstance(e, ActionEvent):
                continue
            tc = e.tool_call
            if tc is None or tc.tool_name not in _F6_FILE_MUTATING_TOOLS:
                continue
            path = tc.arguments.get("path")
            actions_by_id[e.id] = (e, path if isinstance(path, str) and path else None)

        if not actions_by_id:
            return None

        # Count per-path attempts and per-path failures. A failure is any
        # paired (action_id match) AgentErrorEvent OR ObservationEvent whose
        # ToolResult reports success=False. An attempt with no paired result
        # event (still in flight) is neither a success nor a failure — it
        # doesn't count against the file. A non-mutating tool's outcome
        # obviously doesn't affect the file either.
        attempts: dict[str, int] = {}
        failures: dict[str, int] = {}
        for e in events:
            if isinstance(e, AgentErrorEvent):
                if e.action_id is None or e.action_id not in actions_by_id:
                    continue
                _, path = actions_by_id[e.action_id]
                if path is None:
                    continue
                attempts[path] = attempts.get(path, 0) + 1
                failures[path] = failures.get(path, 0) + 1
            elif isinstance(e, ObservationEvent):
                ae = actions_by_id.get(e.action_id or "")
                if ae is None:
                    continue
                _, path = ae
                if path is None:
                    continue
                attempts[path] = attempts.get(path, 0) + 1
                if not e.tool_result.success:
                    failures[path] = failures.get(path, 0) + 1

        # Pick the first file (in iteration order, which is event order) that
        # crosses both thresholds. Multi-file spirals are possible but the
        # directive surfaces ONE file at a time — the engine's existing
        # one-action-per-iteration loop can re-evaluate next turn and surface
        # the next file if the first rewrite also fails. Stable, testable
        # order matters: iteration order of the recent window is the order
        # the events were appended (the store's monotonic seq), which is
        # also the order the engine reasons about.
        for path, attempt_count in attempts.items():
            fail_count = failures.get(path, 0)
            if (
                fail_count >= self.t.per_file_rewrite_failures
                and attempt_count >= self.t.per_file_rewrite_min_attempts
            ):
                return RewriteDirective(
                    kind="full_rewrite",
                    path=path,
                    failures=fail_count,
                    attempts=attempt_count,
                )
        return None

    # -- pattern 1: identical action→observation cycles -----------------------

    def _repeated_action_observation(self, events: list[Event]) -> bool:
        n = self.t.repeat_action_observation
        # W1: exempt wait/poll tools (legit poll loops) and plan/meta tools
        # (managed by the dedicated bookkeeping halt). Filter the ACTION events
        # only — the paired ObservationEvents stay in the stream so the
        # consecutive-pairs logic sees the correct adjacency structure.
        _exempt = _WAIT_POLL_TOOLS | _PLAN_META_TOOLS
        filtered = [
            e for e in events
            if not (
                isinstance(e, ActionEvent)
                and e.tool_call is not None
                and e.tool_call.tool_name in _exempt
            )
        ]
        pairs = _consecutive_pairs(filtered, ActionEvent, ObservationEvent)
        if len(pairs) < n:
            return False
        last = pairs[-n:]
        a0, o0 = last[0]
        # W1: ignore_thought=True so thought-paraphrasing does not mask a loop.
        return all(
            event_content_eq(a, a0, ignore_thought=True) and event_content_eq(o, o0)
            for a, o in last
        )

    # -- pattern 2: identical action→error cycles -----------------------------

    def _repeated_action_error(self, events: list[Event]) -> bool:
        n = self.t.repeat_action_error
        # W1: NO tool filter here — a perpetually-erroring poll IS stuck.
        pairs = _consecutive_pairs(events, ActionEvent, AgentErrorEvent)
        if len(pairs) < n:
            return False
        last = pairs[-n:]
        a0, e0 = last[0]
        # W1: ignore_thought=True so thought-paraphrasing does not mask a loop.
        return all(event_content_eq(a, a0, ignore_thought=True) and event_content_eq(e, e0) for a, e in last)

    # -- pattern 3: agent monologue (consecutive agent messages) --------------

    def _agent_monologue(self, events: list[Event]) -> bool:
        run = 0
        best = 0
        for e in events:
            if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
                run += 1
                best = max(best, run)
            else:
                run = 0
        return best >= self.t.agent_monologue

    # -- pattern 4: alternating A-B-A-B action loops --------------------------

    def _alternating(self, events: list[Event]) -> bool:
        cycles = self.t.alternating
        need = 2 * cycles
        # W1: exempt wait/poll and plan/meta tools (same rationale as pattern 1).
        _exempt = _WAIT_POLL_TOOLS | _PLAN_META_TOOLS
        actions = [
            e for e in events
            if isinstance(e, ActionEvent)
            and e.tool_call is not None
            and e.tool_call.tool_name not in _exempt
        ]
        if len(actions) < need:
            return False
        window = actions[-need:]
        evens, odds = window[0::2], window[1::2]
        a, b = evens[0], odds[0]
        # W1: ignore_thought=True so thought paraphrasing does not mask the loop.
        if event_content_eq(a, b, ignore_thought=True):
            return False  # identical (ignoring thought) => pattern 1, not alternation
        return all(event_content_eq(x, a, ignore_thought=True) for x in evens) and all(
            event_content_eq(y, b, ignore_thought=True) for y in odds
        )


# -- WALK-19: semantic no-progress detector -----------------------------------
#
# Failure-INDEPENDENT and NOT assist-gated (a free function, not a gated method
# on StuckDetector). Every other breaker keys on a FAILURE or a BYTE-IDENTICAL
# repeat; a capable model debugging a black screen writes a DIFFERENT edit each
# turn (so patterns 1-4 never fire), each edit "succeeds" (writes apply, dev
# server returns 200 — so the circuit breaker's failure count stays 0), and it
# grinds to max_iterations. The semantic signal it misses: the same probe/verify
# OUTCOME recurring across many varied edits = no real progress.


def repeated_verify_no_progress(
    events: list[Event],
    *,
    distinct_edits: int = NO_PROGRESS_DISTINCT_EDITS,
    probe_tools: frozenset[str] = _NO_PROGRESS_PROBE_TOOLS,
    edit_tools: frozenset[str] = _F6_FILE_MUTATING_TOOLS,
) -> bool:
    """True when >= `distinct_edits` DISTINCT edit actions all yield the SAME
    probe/verify outcome — the successful-but-useless varied-edit loop (F4).

    Pure function of the recent window. A new USER instruction resets it (a new
    goal means 'not stuck'). The outcome key is the probe observation's
    (success, content); distinctness of edits uses `event_content_eq` so a
    byte-identical re-edit (pattern 1's job) is not double-counted. Requires at
    least two probe observations so a single recurrence cannot trip it.
    """
    if distinct_edits <= 0:
        return False
    window = _after_last_user_message(events)
    tool_by_id: dict[str, str] = {
        e.id: e.tool_call.tool_name
        for e in window
        if isinstance(e, ActionEvent) and e.tool_call is not None
    }
    # Ordered trail of edit ActionEvents and probe-observation outcome keys.
    trail: list[tuple[str, object]] = []
    for e in window:
        if (
            isinstance(e, ActionEvent)
            and e.tool_call is not None
            and e.tool_call.tool_name in edit_tools
        ):
            trail.append(("edit", e))
        elif isinstance(e, ObservationEvent) and tool_by_id.get(e.action_id) in probe_tools:
            trail.append(("probe", (e.tool_result.success, e.tool_result.content)))

    last_key: object | None = next(
        (payload for kind, payload in reversed(trail) if kind == "probe"), None
    )
    if last_key is None:
        return False
    # Walk back over the maximal trailing run of probes sharing `last_key`,
    # counting the DISTINCT edits interleaved in that run's span.
    probe_count = 0
    distinct: list[ActionEvent] = []
    for kind, payload in reversed(trail):
        if kind == "probe":
            if payload != last_key:
                break
            probe_count += 1
        elif not any(event_content_eq(cast("ActionEvent", payload), d, ignore_thought=True) for d in distinct):
            distinct.append(cast("ActionEvent", payload))
    return probe_count >= 2 and len(distinct) >= distinct_edits
