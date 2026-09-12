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
the spiraling file. `gate_stuck` consumes that directive before the generic
stuck branch and injects a full-file-rewrite reminder. Assist OFF → the
directive is always None, and the bool facade is unchanged.
"""

from __future__ import annotations

import re
from typing import Literal

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
from .no_progress_detector import (
    _NO_PROGRESS_PROBE_TOOLS,
    _after_last_user_message,
)
from .no_progress_detector import DEBUG_PROBE_BUDGET_DETAIL as DEBUG_PROBE_BUDGET_DETAIL
from .no_progress_detector import (
    F6_FILE_MUTATING_TOOLS as F6_FILE_MUTATING_TOOLS,
)
from .no_progress_detector import VerifierEvidenceInvalid as VerifierEvidenceInvalid
from .no_progress_detector import (
    VerifierFailureNoProgress as VerifierFailureNoProgress,
)
from .no_progress_detector import (
    barren_streak_no_progress as barren_streak_no_progress,
)
from .no_progress_detector import debug_probe_budget as debug_probe_budget
from .no_progress_detector import (
    debug_probe_budget_notice_active as debug_probe_budget_notice_active,
)
from .no_progress_detector import no_progress_detected as no_progress_detected
from .no_progress_detector import (
    repeated_failed_verifier_no_progress as repeated_failed_verifier_no_progress,
)
from .no_progress_detector import (
    repeated_verify_no_progress as repeated_verify_no_progress,
)
from .no_progress_detector import (
    successful_mutation_with_receipt as successful_mutation_with_receipt,
)
from .redundant_read_detector import _normalized_read_path as _normalized_read_path
from .redundant_read_detector import (
    _permitted_whole_read_baseline as _permitted_whole_read_baseline,
)
from .redundant_read_detector import _read_range as _read_range
from .redundant_read_detector import (
    per_file_rewrite_counts,
    redundant_read_after_churn_nudge,
    redundant_read_coverage,
    repeated_unchanged_file_read,
)
from .signals import (
    _NON_PRODUCTIVE_TOOLS,
    _NONCRITICAL_FAILURE_TOOLS,
)

# Live M3 probe-spin: a weak model can poll the same non-productive probe tool
# repeatedly with slightly-changing output (server_status timestamps/counters), so
# byte-identical stuck patterns never fire. This detector keys on tool-name
# frequency instead of output equality, and resets as soon as the model attempts a
# productive action.
_PROBE_SPIN_TOOLS = _NO_PROGRESS_PROBE_TOOLS | frozenset(
    {
        "verify_appkit_app",
        "design_lint",
        "app_snapshot_version",
        "shell_view",
        "shell_wait",
        "job_status",
        "deploy_status",
    }
)

# H336 — successful file reads can silently thrash by varying offset/limit over
# lines the model already read. Raw action/output equality cannot see that the
# coverage is identical. The parser keys only on FileReadTool's explicit numbered
# line contract; opaque/binary/malformed observations never contribute.
_FILE_READ_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\t(.*)$", re.MULTILINE)
_FILE_READ_HEADER_RE = re.compile(r"^\[lines\s+\d+-\d+\s+of\s+(\d+)(?:[; (\]\u2014])")
_FILE_READ_RANGE_HEADER_RE = re.compile(
    r"\[lines\s+(\d+)-(\d+)\s+of\s+(\d+)"
    r"(?:(?:; read more with offset=(\d+))|( \u2014 offset past end of file))?\]"
)

# W1 — wait/poll tools exempted from patterns 1 and 4 (but NOT 2): a legit
# "poll until server up" loop must not be flagged as stuck (OpenHands #5355 FP
# class). Pattern 2 keeps them: a perpetually-erroring poll IS stuck.
_WAIT_POLL_TOOLS = frozenset(
    {"sleep", "wait", "server_status", "poll", "browser_wait", "job_status", "deploy_status"}
)

# W1 — plan/meta tools managed by the dedicated bookkeeping halt
# (turn_control.py gate_bookkeeping_streak). Excluded from patterns 1 and 4
# to avoid double-firing with the bookkeeping gate. Mirrors _BOOKKEEPING_TOOLS
# in signals.py — kept inline so stuck.py stays self-contained; a new tool
# added to _BOOKKEEPING_TOOLS must also be added here.
_PLAN_META_TOOLS = frozenset(
    {
        "submit_plan",
        "propose_plan_update",
        "plan_step",
        "update_plan_progress",
        "finish",
        "think",
    }
)


class StuckThresholds(BaseModel):
    # An exact, successful, byte-unchanged file_read may be repeated once, but
    # the loop must enter its typed read-recovery episode before a third
    # identical call. This is deliberately narrower and earlier than the
    # generic four-cycle detector: changed bytes, another action, another
    # resource/range, and failed reads do not contribute.
    repeat_unchanged_file_read: int = 2
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
    # M3 — same non-productive probe tool calls in the recent window, with no
    # productive action between them. 0 disables the detector.
    probe_spin_calls: int = 12
    # H336/H338 — redundant byte-identical observations of the same known line
    # after the latest productive action. Counting per-line hits (not merely read
    # calls) distinguishes a cyclic reread from a finite set of disjoint/overlapping
    # validation slices. The first read and new/changed lines are progress.
    # 0 disables the detector.
    redundant_read_coverage: int = 4


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
    has crossed the per-file threshold. `gate_stuck` consumes the directive
    directly from `evaluate()`; `is_stuck()` remains the byte-identical bool
    facade for existing callers. Assist OFF ⇒ `rewrite_directive` is always
    None.
    """

    is_stuck: bool
    # W-31 — the NAME of the first stuck pattern that fired (None when not
    # stuck). Stamped onto the gate_stuck STUCK StatusEvent's `detail` so logs/
    # the UI identify WHICH breaker halted the run instead of an undifferentiated
    # STUCK (Dylan had to infer it). Mirrors every sibling gate's `detail`.
    reason: str | None = None
    rewrite_directive: RewriteDirective | None = None


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

    def required_scan_window(self) -> int:
        """Number of raw recent events needed by all enabled patterns.

        The legacy identical-repeat patterns are governed by ``scan_window``.
        Probe-spin counts ActionEvents, which normally arrive paired with
        ObservationEvents, so N probe calls require roughly 2N raw events.
        """
        probe_window = self.t.probe_spin_calls * 2 if self.t.probe_spin_calls > 0 else 0
        # One coverage-establishing read plus N redundant reads, each normally an
        # action/observation pair. Four extra events preserve the one-shot escape
        # reminder and the model's bounded response inside the next evaluation.
        read_window = (
            (self.t.redundant_read_coverage + 1) * 2 + 4
            if self.t.redundant_read_coverage > 0
            else 0
        )
        exact_read_window = (
            self.t.repeat_unchanged_file_read * 2 + 4
            if self.t.repeat_unchanged_file_read > 0
            else 0
        )
        return max(self.t.scan_window, probe_window, read_window, exact_read_window)

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
        legacy_recent = recent[-self.t.scan_window :]
        if self._repeated_action_observation(legacy_recent):
            return "repeated_action_observation"  # pattern 1 (W-30/W-31: repeated reads)
        if self._repeated_action_error(legacy_recent):
            return "repeated_action_error"  # pattern 2
        if self._agent_monologue(legacy_recent):
            return "agent_monologue"  # pattern 3
        if self._alternating(legacy_recent):
            return "alternating_actions"  # pattern 4
        if self._pure_repeat(legacy_recent):
            return "pure_repeat"  # W1 pattern 5
        if self._redundant_read_after_churn_nudge(recent):
            return "redundant_read_after_churn_nudge"
        if self._redundant_read_coverage(recent):
            return "redundant_read_coverage"
        if self._repeated_unchanged_file_read(legacy_recent):
            return "repeated_unchanged_file_read"
        if self._probe_spin(recent):
            return "probe_spin"
        return None

    def _repeated_unchanged_file_read(self, events: list[Event]) -> bool:
        return repeated_unchanged_file_read(
            events,
            self.t.repeat_unchanged_file_read,
        )

    # -- H336 pattern 7: varied offsets over already-known file content ------

    def _redundant_read_after_churn_nudge(self, events: list[Event]) -> bool:
        return redundant_read_after_churn_nudge(events)

    def _redundant_read_coverage(self, events: list[Event]) -> bool:
        return redundant_read_coverage(
            events,
            self.t.redundant_read_coverage,
        )

    # -- M3 pattern 6: repeated varying probe calls --------------------------

    def _probe_spin(self, events: list[Event]) -> bool:
        threshold = self.t.probe_spin_calls
        if threshold <= 0:
            return False
        counts: dict[str, int] = {}
        for event in events:
            if not isinstance(event, ActionEvent) or event.tool_call is None:
                continue
            tool_name = event.tool_call.tool_name
            if event.meta.get("verify_probe"):
                continue
            if tool_name not in _NON_PRODUCTIVE_TOOLS:
                counts.clear()
                continue
            if tool_name not in _PROBE_SPIN_TOOLS:
                continue
            counts[tool_name] = counts.get(tool_name, 0) + 1
        return any(count >= threshold for count in counts.values())

    # -- W1 pattern 5: Roo-style pure-repeat (back-to-back identical actions) ----
    #
    # Catches a model that fires the same tool call repeatedly WITHOUT waiting
    # for a paired observation — e.g. a tight loop issuing back-to-back
    # file_read("x.py") with no observation in between. Pattern 1 requires
    # action→obs pairs; this pattern works on the raw non-wait action stream.
    # Uses ignore_thought=True so thought paraphrasing does not mask the loop.
    # Wait/poll and plan/meta tools are exempt (same rationale as patterns 1+4).
    #
    # 2026-07-09 deck-run autopsy: the implementation had DRIFTED from this
    # intent — it collapsed the stream to actions-only, so `click → obs(2/9) →
    # click → obs(3/9) → …` (identical actions, each with a FRESH, CHANGING
    # observation: the model paging through its own slideshow) counted as a
    # "run" and killed a legitimately-progressing build. Interleaved repeats are
    # pattern 1's jurisdiction (it correctly requires the OBSERVATION to repeat
    # too); this pattern only owns actions with NO observation/error in between.

    def _pure_repeat(self, events: list[Event]) -> bool:
        threshold = self.t.repeat_action_observation
        _exempt = _WAIT_POLL_TOOLS | _PLAN_META_TOOLS
        # Keep observations/errors in the walked stream — their PRESENCE between
        # two actions is exactly what distinguishes "the model waited and the
        # world answered" from a tight no-wait loop. Only exempt ACTIONS are
        # dropped (a `think` between two clicks does not make them non-adjacent).
        stream = [
            e
            for e in events
            if isinstance(e, ObservationEvent | AgentErrorEvent)
            or (
                isinstance(e, ActionEvent)
                and e.tool_call is not None
                and e.tool_call.tool_name not in _exempt
            )
        ]
        # Trailing maximal block of ActionEvents (no obs/error inside): the
        # currently-unanswered burst. An observation ANYWHERE inside would mean
        # the model did wait at least once — that episode belongs to pattern 1.
        run = 0
        last: ActionEvent | None = None
        for e in reversed(stream):
            if not isinstance(e, ActionEvent):
                break
            if last is None:
                last = e
                run = 1
            elif event_content_eq(e, last, ignore_thought=True):
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
        self,
        events: list[Event],
    ) -> RewriteDirective | None:
        counts = per_file_rewrite_counts(
            events,
            self.t.per_file_rewrite_failures,
            self.t.per_file_rewrite_min_attempts,
        )
        if counts is None:
            return None
        path, failures, attempts = counts
        return RewriteDirective(
            kind="full_rewrite",
            path=path,
            failures=failures,
            attempts=attempts,
        )

    # -- pattern 1: identical action→observation cycles -----------------------

    def _repeated_action_observation(self, events: list[Event]) -> bool:
        n = self.t.repeat_action_observation
        # W1: exempt wait/poll tools (legit poll loops) and plan/meta tools
        # (managed by the dedicated bookkeeping halt). Filter the ACTION events
        # only — the paired ObservationEvents stay in the stream so the
        # consecutive-pairs logic sees the correct adjacency structure.
        _exempt = _WAIT_POLL_TOOLS | _PLAN_META_TOOLS
        filtered = [
            e
            for e in events
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
        # ignore_volatile_content=True so the browser's per-action screenshot
        # path (an incrementing counter) can't make a genuinely DEAD click loop
        # look like fresh observations — this pattern owns that case now that
        # _pure_repeat no longer (wrongly) fired across interleaved observations.
        return all(
            event_content_eq(a, a0, ignore_thought=True)
            and event_content_eq(o, o0, ignore_volatile_content=True)
            for a, o in last
        )

    # -- pattern 2: identical action→error cycles -----------------------------

    def _repeated_action_error(self, events: list[Event]) -> bool:
        n = self.t.repeat_action_error
        # W1: a perpetually-erroring poll/execution tool IS stuck — those stay counted.
        # EXCEPTION: a NONCRITICAL declarative bookkeeping tool (update_plan_progress,
        # signals._NONCRITICAL_FAILURE_TOOLS) whose calls fail SCHEMA VALIDATION is a
        # cosmetic UI hiccup, NOT task-stuck — some models (MiniMax-M3) intermittently
        # malform its `steps` payload. Killing an OTHERWISE-PRODUCTIVE build (real
        # file_writes succeeding) on that loop is wrong. Mirror the existing
        # count_recent_failures exclusion (signals.py:119) so the asymmetry is gone:
        # drop pairs whose ACTION is a noncritical tool, judge the loop on the rest.
        # Genuine plan-tracker-only spam still fails fast at gate_bookkeeping_streak
        # (the dedicated bookkeeping halt). Real execution tools are NEVER filtered.
        pairs = [
            (a, e)
            for (a, e) in _consecutive_pairs(events, ActionEvent, AgentErrorEvent)
            if isinstance(a, ActionEvent)
            and isinstance(e, AgentErrorEvent)
            and (a.tool_call is None or a.tool_call.tool_name not in _NONCRITICAL_FAILURE_TOOLS)
            and not a.meta.get("verify_probe")  # the finish gate's re-runs are not the model's loop
        ]
        if len(pairs) < n:
            return False
        last = pairs[-n:]
        a0, e0 = last[0]
        # W1: ignore_thought=True so thought-paraphrasing does not mask a loop.
        return all(
            event_content_eq(a, a0, ignore_thought=True) and event_content_eq(e, e0)
            for a, e in last
        )

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
            e
            for e in events
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
