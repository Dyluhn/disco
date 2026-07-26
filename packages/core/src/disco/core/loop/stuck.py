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

import hashlib
import posixpath
import re
from collections import Counter
from dataclasses import dataclass
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
    StatusEvent,
)
from ..workspace_paths import strip_redundant_workspace_prefix
from .dedup import _F9_POINTER_SENTINEL
from .signals import (
    _NON_PRODUCTIVE_TOOLS,
    _NONCRITICAL_FAILURE_TOOLS,
    READ_CHURN_NUDGE_DIAGNOSTIC,
)

# F6 — tools whose ActionEvents count as "patch attempts" for the per-file
# rewrite tracker. Mirrors `_WORKSPACE_MUTATING_TOOLS` in engine.py (kept
# inline here so stuck.py stays self-contained — engine.py is held by another
# worker and stuck.py must not import from it). A new mutating tool added in
# one place must be added in the other.
F6_FILE_MUTATING_TOOLS = frozenset(
    {
        "file_write",
        "file_edit",
        "file_append",
        "file_replace_lines",
        "file_insert_lines",
    }
)
_BARREN_NO_EFFECT_MUTATING_TOOLS = F6_FILE_MUTATING_TOOLS | frozenset(
    {
        "exact_replace",
        "file_str_replace",
        "run_project_script",
        "safe_write_file",
    }
)
_BARREN_NO_EFFECT_ERROR_CODES = frozenset({"FRESH_READ_REQUIRED", "no_op_edit", "no_op_write"})

# WALK-19 — probe/verify tools whose ObservationEvent carries the "what does the
# running app actually look like now" signal. When this signal is unchanged
# across many DISTINCT edits, the model is making successful-but-useless edits
# (the black-screen-game). `serve` is intercepted by the engine into a
# DeliverableEvent (no ObservationEvent), so it is not a probe here.
_NO_PROGRESS_PROBE_TOOLS = frozenset(
    {"browser", "server_status", "preview_status", "verify_web_app"}
)
# Distinct varied edits that must recur against ONE stable probe outcome before
# the no-progress breaker trips. 4 mirrors the circuit-breaker's failure budget.
NO_PROGRESS_DISTINCT_EDITS = 4

# GAP-1 — a read/reasoning-only loop can burn turns forever without tripping the
# mutating-tool no-progress detectors. Keep this narrow: only pure context tools
# count as barren, and identical failing observations are required.
BARREN_STREAK_TURNS = 8
BARREN_STREAK_IDENTICAL_FAILURES = 3
_BARREN_READ_ONLY_TOOLS = frozenset({"think", "file_read", "file_list", "search", "extract"})
_FAILURE_PREFIX_CHARS = 160

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

# H275 — structured verifier outcomes are semantic progress signals, not ordinary
# successful tool executions.  The live AppKit run varied reads/lists/status calls
# between identical failed verifier fingerprints, evading exact-action and raw-output
# loop detectors.  These tools return ``ToolResult.success=True`` when the verifier
# itself executed even when ``structured.passed=False``; judge the structured verdict.
_STRUCTURED_VERIFIER_TOOLS = frozenset({"verify_web_app", "verify_appkit_app"})
FAILED_VERIFIER_REPEAT_LIMIT = 2

# A repeated failed verdict is re-armed only by a confirmed deliverable mutation.
# Tool name + ``success=True`` is deliberately insufficient: several mutators can
# hollow-succeed after applying zero edits.  These sets select the receipt schema;
# ``_effective_verifier_mutation`` validates the receipt itself.
_VERIFIER_FILE_RECEIPT_TOOLS = F6_FILE_MUTATING_TOOLS
_VERIFIER_APPKIT_RECEIPT_TOOLS = frozenset(
    {
        "app_create",
        "app_add_section",
        "app_update_content",
        "app_set_design",
        "app_add_primitive",
    }
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class VerifierFailureNoProgress:
    """Stable failed-verifier streak after the latest effective mutation."""

    tool_name: str
    failure_fingerprint: str
    failure_fingerprint_sha256: str
    repeats: int
    streak_start_seq: int
    latest_verdict_seq: int
    summary: str
    next_action: str

    @property
    def marker_detail(self) -> str:
        """Semantic durable marker; event metadata is never load-bearing."""

        return f"verifier_no_progress:{self.tool_name}:{self.failure_fingerprint_sha256[:16]}"


@dataclass(frozen=True)
class VerifierEvidenceInvalid:
    """A successful verifier execution emitted unusable structured evidence."""

    tool_name: str
    verdict_seq: int
    reason: str


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


# Recovery boundaries reset stuck detection exactly like a fresh user message:
# a plan approval / harvested revision starts a NEW execution segment, and the
# old segment's read-loop evidence must not convict it before its first turn
# (dt6 autopsy: harvested_revision_plan → plan_approved → STUCK two events
# later, zero actions in between — the window carried stale evidence across).
_RECOVERY_BOUNDARY_DETAILS = frozenset(
    {"plan_approved", "harvested_revision_plan", "alternative_picked:manual"}
)


def _after_last_user_message(events: list[Event]) -> list[Event]:
    """Discard everything at/before the last USER MessageEvent OR recovery
    boundary — a new instruction or an approved recovery means 'not stuck'."""
    last = -1
    for i, e in enumerate(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            last = i
        elif isinstance(e, StatusEvent) and (e.detail or "") in _RECOVERY_BOUNDARY_DETAILS:
            last = i
    return events[last + 1 :]


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


# Pure read-parsing helpers, extracted from StuckDetector.
# None of them read instance state; they only ever called each other. Keeping
# them inside the class bought nothing and pushed a capped coordinator over
# budget, so they move together -- moving one alone breaks the cluster.


def _normalized_read_path(raw: object) -> str | None:
    path = str(raw or "").strip()
    if not path:
        return None
    normalized = posixpath.normpath(strip_redundant_workspace_prefix(path))
    return normalized if normalized not in ("", ".") else None


def _bounded_decimal(raw: str) -> int | None:
    if len(raw) > 18:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _numbered_read_lines(content: str) -> dict[int, str] | None:
    lines: dict[int, str] = {}
    for match in _FILE_READ_NUMBERED_LINE_RE.finditer(content):
        number = _bounded_decimal(match.group(1))
        if number is None:
            return None
        lines[number] = match.group(2)
    return lines


def _read_total(content: str) -> int | None:
    match = _FILE_READ_HEADER_RE.match(content)
    if match is None:
        return None
    raw = match.group(1)
    return _bounded_decimal(raw)


def _read_range(content: str) -> tuple[int, int, int] | None:
    header = content.splitlines()[0] if content else ""
    match = _FILE_READ_RANGE_HEADER_RE.fullmatch(header)
    if match is None:
        return None
    start = _bounded_decimal(match.group(1))
    end = _bounded_decimal(match.group(2))
    total = _bounded_decimal(match.group(3))
    more_offset = (
        _bounded_decimal(match.group(4)) if match.group(4) is not None else None
    )
    # Observation bytes are untrusted detector input. The shared bounded
    # parser prevents Python's max-digit guard (or huge arbitrary-precision
    # integers) from turning malformed evidence into a loop crash/CPU sink.
    if start is None or end is None or total is None:
        return None
    if match.group(4) is not None and more_offset is None:
        return None
    past_eof = match.group(5) is not None
    if more_offset is not None and (end >= total or more_offset != end + 1):
        return None
    if past_eof and not (total > 0 and start > total and end == total):
        return None
    return (start, end, total)


def _trusted_read_churn_nudge_path(event: Event) -> str | None:
    """Return the exact path named by a trusted typed read-churn nudge."""
    if (
        not isinstance(event, MessageEvent)
        or event.source != EventSource.ENVIRONMENT
        or event.meta.get("diagnostic") != READ_CHURN_NUDGE_DIAGNOSTIC
        or type(event.meta.get("count")) is not int
        or int(event.meta["count"]) < 5
    ):
        return None
    return _normalized_read_path(event.meta.get("path"))


def _permitted_whole_read_baseline(
    action: ActionEvent,
    observation: ObservationEvent,
    expected_path: str,
) -> tuple[dict[int, str], int] | None:
    """Return a strictly paired whole-file baseline for a trusted nudge."""
    result = observation.tool_result
    if (
        action.source != EventSource.AGENT
        or observation.source != EventSource.ENVIRONMENT
        or observation.action_id != action.id
        or action.tool_call is None
        or action.tool_call.tool_name != "file_read"
        or result.tool_name != "file_read"
        or not result.success
        or _normalized_read_path(action.tool_call.arguments.get("path")) != expected_path
        or "offset" in action.tool_call.arguments
        or "limit" in action.tool_call.arguments
    ):
        return None
    lines = _numbered_read_lines(result.content)
    read_range = _read_range(result.content)
    if lines is None or read_range is None:
        return None
    start, end, total = read_range
    body = result.content.splitlines()[1:]
    numbered_matches = [_FILE_READ_NUMBERED_LINE_RE.fullmatch(line) for line in body]
    parsed_sequence = [
        _bounded_decimal(match.group(1)) for match in numbered_matches if match is not None
    ]
    body_valid = all(match is not None for match in numbered_matches) and all(
        number is not None for number in parsed_sequence
    )
    numbered_sequence = [number for number in parsed_sequence if number is not None]
    whole_result = (
        total == 0 and start == 1 and end == 0 and body_valid and not numbered_sequence
    ) or (
        total > 0
        and start == 1
        and end == total
        and body_valid
        and _contiguous_numbered_range(numbered_sequence, 1, total)
    )
    return (dict(lines), total) if whole_result else None


def _contiguous_numbered_range(numbers: list[int], start: int, end: int) -> bool:
    expected_count = end - start + 1
    return (
        expected_count >= 0
        and len(numbers) == expected_count
        and all(number == start + index for index, number in enumerate(numbers))
    )


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
        """Detect an exact unchanged read cycle before a third call is proposed.

        The soak contract permits two identical actions and rejects the third.
        Disco therefore has to arm its existing one-shot read escape after the
        second successful, unchanged read—not wait for the generic four-cycle
        breaker. The check remains narrow and host-grounded:

        - only the final consecutive action/observation pairs count, so any
          intervening productive action breaks the streak;
        - every action must be the same exact ``file_read`` request;
        - every observation must be successful and either byte-equivalent to
          the first result or an F9 host-owned dedup pointer proving that the
          request was already satisfied from the unchanged prior read.

        Changed output, failed reads, different paths/ranges, and non-read tools
        remain ordinary positive variation.
        """

        n = self.t.repeat_unchanged_file_read
        if n <= 0:
            return False
        # The trusted escape marker grants one recovery attempt. Historical
        # reads remain available to the receipt-based quarantine, but they must
        # not immediately re-convict the next (possibly different) action.
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if isinstance(event, StatusEvent) and event.detail == "stuck_escape":
                events = events[index + 1 :]
                break
        pairs = _consecutive_pairs(events, ActionEvent, ObservationEvent)
        if len(pairs) < n:
            return False
        last = pairs[-n:]
        first_action, first_observation = last[0]
        if (
            not isinstance(first_action, ActionEvent)
            or not isinstance(first_observation, ObservationEvent)
            or first_action.tool_call is None
            or first_action.tool_call.tool_name != "file_read"
            or not first_observation.tool_result.success
            or first_observation.tool_result.tool_name != "file_read"
        ):
            return False
        for action, observation in last[1:]:
            if (
                not isinstance(action, ActionEvent)
                or not isinstance(observation, ObservationEvent)
                or action.tool_call is None
                or action.tool_call.tool_name != "file_read"
                or not observation.tool_result.success
                or observation.tool_result.tool_name != "file_read"
                or not event_content_eq(action, first_action, ignore_thought=True)
            ):
                return False
            same_result = event_content_eq(
                observation,
                first_observation,
                ignore_volatile_content=True,
            )
            host_dedup = observation.tool_result.content.startswith(_F9_POINTER_SENTINEL)
            if not same_result and not host_dedup:
                return False
        return True

    # -- H336 pattern 7: varied offsets over already-known file content ------

    def _redundant_read_after_churn_nudge(self, events: list[Event]) -> bool:
        """Escalate an ignored typed read-churn instruction.

        ``read_churn_nudge`` explicitly permits one whole-file read and then
        requires action.  H340 obeyed the first clause, immediately resumed
        byte-identical slices of that same file, and stayed nominally
        "progressing" until the hard cap.  Bind this breaker to the trusted
        environment marker and exact normalized path so unnudged finite H338
        validation remains byte-for-byte unaffected.

        Judge the final streak rather than returning on a historical prefix:
        changed bytes, a confirmed productive action, or the one-shot
        ``stuck_escape`` can recover later in the same replay window.
        """
        actions: dict[str, ActionEvent] = {}
        nudge_path: str | None = None
        baseline_lines: dict[int, str] | None = None
        baseline_total: int | None = None
        violated = False

        for event in events:
            if isinstance(event, ActionEvent) and event.tool_call is not None:
                actions[event.id] = event
                continue
            trusted_nudge_path = _trusted_read_churn_nudge_path(event)
            if trusted_nudge_path is not None:
                # Only actions emitted after the trusted marker may satisfy
                # its one-whole-read allowance. This is both sequence-safe
                # for persisted events and deterministic for unit events
                # whose optional seq has not been assigned by EventStore.
                actions.clear()
                nudge_path = trusted_nudge_path
                baseline_lines = None
                baseline_total = None
                violated = False
                continue
            if isinstance(event, StatusEvent) and event.detail == "stuck_escape":
                # Preserve the trusted whole-file baseline, but grant the one
                # existing escape attempt. A fresh redundant read re-trips.
                violated = False
                continue
            if (
                not isinstance(event, ObservationEvent)
                or event.source != EventSource.ENVIRONMENT
                or nudge_path is None
            ):
                continue
            result = event.tool_result
            action = actions.get(event.action_id or "")
            paired = (
                action is not None
                and action.source == EventSource.AGENT
                and action.tool_call is not None
                and action.tool_call.tool_name == result.tool_name
            )
            if paired and result.success and result.tool_name not in _NON_PRODUCTIVE_TOOLS:
                nudge_path = None
                baseline_lines = None
                baseline_total = None
                violated = False
                continue
            if not result.success or result.tool_name != "file_read":
                continue
            if (
                not paired
                or action is None
                or action.tool_call is None
                or action.tool_call.tool_name != "file_read"
                or _normalized_read_path(action.tool_call.arguments.get("path")) != nudge_path
            ):
                continue
            lines = _numbered_read_lines(result.content)
            if lines is None:
                continue
            read_range = _read_range(result.content)
            if read_range is None:
                continue
            start, end, total = read_range
            body = result.content.splitlines()[1:]
            numbered_matches = [_FILE_READ_NUMBERED_LINE_RE.fullmatch(line) for line in body]
            parsed_sequence = [
                _bounded_decimal(match.group(1)) for match in numbered_matches if match is not None
            ]
            body_valid = all(match is not None for match in numbered_matches) and all(
                number is not None for number in parsed_sequence
            )
            numbered_sequence = [number for number in parsed_sequence if number is not None]
            if baseline_lines is None:
                args = action.tool_call.arguments
                permitted_baseline = _permitted_whole_read_baseline(action, event, nudge_path)
                if permitted_baseline is not None:
                    baseline_lines, baseline_total = permitted_baseline
                elif "offset" in args or "limit" in args:
                    violated = True
                continue
            structurally_valid = (
                total == 0
                and start >= 1
                and 0 <= end < start
                and body_valid
                and not numbered_sequence
            ) or (
                total > 0
                and body_valid
                and (
                    (
                        1 <= start <= end <= total
                        and _contiguous_numbered_range(numbered_sequence, start, end)
                    )
                    or (1 <= start <= total and end == start - 1 and not numbered_sequence)
                    or (start > total and end == total and not numbered_sequence)
                )
            )
            if not structurally_valid:
                continue
            if total != baseline_total or any(
                baseline_lines.get(number) != text for number, text in lines.items()
            ):
                # The bytes/extent changed despite no observed product mutation.
                # Treat that external change as recovery; stale baseline evidence
                # may not convict the new content.
                nudge_path = None
                baseline_lines = None
                baseline_total = None
                violated = False
                continue
            # A successful empty/header-only read at the unchanged total, or any
            # byte-identical subset of the whole baseline, ignored "then act".
            violated = True

        return violated

    def _redundant_read_coverage(self, events: list[Event]) -> bool:
        """Detect successful same-file reads that add no content knowledge.

        FileReadTool emits each returned source line as ``<number>\t<content>``.
        We retain that exact per-line content after the latest confirmed successful
        productive action. A read is redundant only when every returned numbered
        line was already observed with identical bytes (or a valid unchanged-total
        header proves an empty range). Legitimate pagination, changed content,
        failed reads, and opaque/binary output do not increment the streak.
        """
        threshold = self.t.redundant_read_coverage
        if threshold <= 0:
            return False
        actions: dict[str, ActionEvent] = {}
        known_by_path: dict[str, dict[int, str]] = {}
        total_by_path: dict[str, int] = {}
        redundant_line_hits_by_path: dict[str, dict[int, int]] = {}
        redundant_header_only_by_path: dict[str, int] = {}
        nudge_path: str | None = None
        post_nudge_action_ids: set[str] = set()
        for event in events:
            if isinstance(event, ActionEvent) and event.tool_call is not None:
                actions[event.id] = event
                if nudge_path is not None:
                    post_nudge_action_ids.add(event.id)
                continue
            trusted_nudge_path = _trusted_read_churn_nudge_path(event)
            if trusted_nudge_path is not None:
                # The typed nudge explicitly permits one whole-file baseline read
                # before requiring action. Rearm only its named path's repeat
                # counters so that allowed read cannot inherit pre-nudge hits and
                # terminal-STUCK before the promised "then act" turn. Retain the
                # known bytes/extent: the sibling post-nudge detector still rejects
                # the very next unchanged subset or second whole read fail-closed.
                redundant_line_hits_by_path.pop(trusted_nudge_path, None)
                redundant_header_only_by_path.pop(trusted_nudge_path, None)
                nudge_path = trusted_nudge_path
                post_nudge_action_ids.clear()
                continue
            if isinstance(event, StatusEvent) and event.detail == "stuck_escape":
                # The escape reminder grants one genuine retry. Pre-reminder hit
                # counts must not remain latched and terminal-STUCK a different
                # next action; retain known lines so fresh redundant hits re-trip.
                redundant_line_hits_by_path.clear()
                redundant_header_only_by_path.clear()
                continue
            if not isinstance(event, ObservationEvent):
                continue
            result = event.tool_result
            # An attempted edit is not progress. Reset only when the environment
            # confirms a productive tool succeeded; failed, success=False, and
            # unpaired ActionEvents remain transparent to the read-spin streak.
            if result.success and result.tool_name not in _NON_PRODUCTIVE_TOOLS:
                known_by_path.clear()
                total_by_path.clear()
                redundant_line_hits_by_path.clear()
                redundant_header_only_by_path.clear()
                nudge_path = None
                post_nudge_action_ids.clear()
                continue
            if not result.success:
                continue
            action = actions.get(event.action_id or "")
            if action is None or action.tool_call is None:
                continue
            if action.tool_call.tool_name != "file_read" or result.tool_name != "file_read":
                continue
            path = _normalized_read_path(action.tool_call.arguments.get("path"))
            lines = _numbered_read_lines(result.content)
            if lines is None:
                continue
            total = _read_total(result.content)
            if path is None or total is None:
                continue
            permitted_baseline = (
                nudge_path == path
                and action.id in post_nudge_action_ids
                and _permitted_whole_read_baseline(action, event, path) is not None
            )
            prior_total = total_by_path.get(path)
            if prior_total is None or prior_total != total:
                total_by_path[path] = total
                known_by_path[path] = dict(lines)
                redundant_line_hits_by_path.clear()
                redundant_header_only_by_path.clear()
                if permitted_baseline:
                    nudge_path = None
                    post_nudge_action_ids.clear()
                continue
            known = known_by_path.setdefault(path, {})
            redundant = not lines or (
                bool(known) and all(known.get(number) == text for number, text in lines.items())
            )
            if permitted_baseline:
                # The trusted contract promises this exact baseline before action.
                # Preserve knowledge but do not let the allowed read itself reach a
                # configured generic threshold (including the supported value one).
                if not redundant:
                    redundant_line_hits_by_path.clear()
                    redundant_header_only_by_path.clear()
                    known.update(lines)
                nudge_path = None
                post_nudge_action_ids.clear()
            elif redundant:
                if lines:
                    hits = redundant_line_hits_by_path.setdefault(path, {})
                    for number in lines:
                        hits[number] = hits.get(number, 0) + 1
                else:
                    redundant_header_only_by_path[path] = (
                        redundant_header_only_by_path.get(path, 0) + 1
                    )
            else:
                # New/changed knowledge on any path is a genuine change of
                # approach after the escape nudge. Keep per-path coverage maps,
                # but re-arm every redundancy streak from zero.
                redundant_line_hits_by_path.clear()
                redundant_header_only_by_path.clear()
                known.update(lines)
        # Judge the FINAL streak, not a historical prefix. A successful repair or
        # changed/new content later in this same window must clear a prior trigger;
        # otherwise the one-shot escape reminder would terminal-STUCK real recovery.
        return any(
            count >= threshold
            for hits in redundant_line_hits_by_path.values()
            for count in hits.values()
        ) or any(count >= threshold for count in redundant_header_only_by_path.values())

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

    def _per_file_rewrite_directive(self, events: list[Event]) -> RewriteDirective | None:
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
            if tc is None or tc.tool_name not in F6_FILE_MUTATING_TOOLS:
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


# -- WALK-19: semantic no-progress detector -----------------------------------
#
# Failure-INDEPENDENT and NOT assist-gated (a free function, not a gated method
# on StuckDetector). Every other breaker keys on a FAILURE or a BYTE-IDENTICAL
# repeat; a capable model debugging a black screen writes a DIFFERENT edit each
# turn (so patterns 1-4 never fire), each edit "succeeds" (writes apply, dev
# server returns 200 — so the circuit breaker's failure count stays 0), and it
# grinds to max_iterations. The semantic signal it misses: the same probe/verify
# OUTCOME recurring across many varied edits = no real progress.


def _verifier_fingerprint(structured: dict) -> tuple[str, str] | None:
    raw = structured.get("failure_fingerprint")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return (raw if len(raw) <= 256 else f"sha256:{digest}", digest)


def successful_mutation_with_receipt(event: ObservationEvent, action: ActionEvent | None) -> bool:
    """True only for a successful mutation carrying a concrete changed-state receipt.

    This is the shared trust boundary for semantic verifier recovery and the
    stuck-escape quarantine. Tool-name taxonomy alone is insufficient: an
    all-purpose tool can succeed without changing the deliverable.
    """

    result = event.tool_result
    if not result.success or action is None or action.tool_call is None:
        return False
    tool_name = action.tool_call.tool_name
    if result.tool_name != tool_name or not isinstance(result.structured, dict):
        return False
    structured = result.structured
    if structured.get("state_changed") is True:
        return True
    if tool_name == "run_project_script":
        applied = structured.get("applied")
        return isinstance(applied, list) and bool(applied)
    if tool_name in _VERIFIER_APPKIT_RECEIPT_TOOLS:
        written = structured.get("files_written")
        return isinstance(written, list) and bool(written)
    if tool_name in _VERIFIER_FILE_RECEIPT_TOOLS:
        path = structured.get("path")
        sha256 = structured.get("sha256")
        return (
            isinstance(path, str)
            and bool(path.strip())
            and isinstance(sha256, str)
            and _SHA256_HEX.fullmatch(sha256) is not None
        )
    return False


@dataclass
class _VerifierStreak:
    fingerprint: str
    fingerprint_sha256: str
    repeats: int
    streak_start_seq: int
    latest_verdict_seq: int
    summary: str
    next_action: str


def repeated_failed_verifier_no_progress(
    events: list[Event],
    *,
    repeats: int = FAILED_VERIFIER_REPEAT_LIMIT,
) -> VerifierFailureNoProgress | VerifierEvidenceInvalid | None:
    """Return the trailing unchanged failed-verifier streak, if bounded progress failed.

    Unlike exact-action detectors, this consumes the structured verifier verdict:
    ``ToolResult.success`` means the probe executed, while ``structured.passed`` is
    the product verdict.  Varied diagnostics between two identical failures are
    transparent.  A new user turn, a successful deliverable mutation, a changed
    fingerprint, or a passing verdict resets that verifier's streak. A confirmed
    deliverable mutation resets all verifier streaks; failed, receipt-empty, and no-op
    mutations do not. Different verifier tools retain independent unresolved streaks.
    The full post-user event history is inspected so a long diagnostic sequence cannot
    fall out of the engine's small recent-event window.
    """
    if repeats <= 0:
        return None
    window = _after_last_user_message(events)
    action_by_id = {
        event.id: event
        for event in window
        if isinstance(event, ActionEvent) and event.tool_call is not None
    }
    streaks: dict[str, _VerifierStreak] = {}

    for index, event in enumerate(window, start=1):
        seq = event.seq if event.seq is not None else index
        if not isinstance(event, ObservationEvent):
            continue
        action = action_by_id.get(event.action_id)
        if successful_mutation_with_receipt(event, action):
            streaks.clear()
            continue

        tool_name = event.tool_result.tool_name
        if tool_name not in _STRUCTURED_VERIFIER_TOOLS:
            continue
        if not event.tool_result.success:
            continue
        structured = event.tool_result.structured
        if not isinstance(structured, dict):
            return VerifierEvidenceInvalid(
                tool_name=tool_name,
                verdict_seq=seq,
                reason="missing_structured_verdict",
            )
        passed = structured.get("passed")
        if not isinstance(passed, bool):
            return VerifierEvidenceInvalid(
                tool_name=tool_name,
                verdict_seq=seq,
                reason="missing_or_malformed_passed",
            )
        if passed:
            streaks.pop(tool_name, None)
            continue

        fingerprint = _verifier_fingerprint(structured)
        if fingerprint is None:
            raw = structured.get("failure_fingerprint")
            reason = (
                "missing_failure_fingerprint" if raw is None else "malformed_failure_fingerprint"
            )
            return VerifierEvidenceInvalid(tool_name=tool_name, verdict_seq=seq, reason=reason)
        normalized, digest = fingerprint
        prior = streaks.get(tool_name)
        streaks[tool_name] = _VerifierStreak(
            fingerprint=normalized,
            fingerprint_sha256=digest,
            repeats=prior.repeats + 1
            if prior is not None and prior.fingerprint == normalized
            else 1,
            streak_start_seq=(
                prior.streak_start_seq
                if prior is not None and prior.fingerprint == normalized
                else seq
            ),
            latest_verdict_seq=seq,
            summary=str(structured.get("summary") or "")[:500],
            next_action=str(structured.get("next_action") or "")[:500],
        )

    candidates = [item for item in streaks.items() if item[1].repeats >= repeats]
    if not candidates:
        return None
    tool_name, current = min(
        candidates,
        key=lambda item: (item[1].streak_start_seq, item[0]),
    )
    return VerifierFailureNoProgress(
        tool_name=tool_name,
        failure_fingerprint=current.fingerprint,
        failure_fingerprint_sha256=current.fingerprint_sha256,
        repeats=current.repeats,
        streak_start_seq=current.streak_start_seq,
        latest_verdict_seq=current.latest_verdict_seq,
        summary=current.summary,
        next_action=current.next_action,
    )


def repeated_verify_no_progress(
    events: list[Event],
    *,
    distinct_edits: int = NO_PROGRESS_DISTINCT_EDITS,
    probe_tools: frozenset[str] = _NO_PROGRESS_PROBE_TOOLS,
    edit_tools: frozenset[str] = F6_FILE_MUTATING_TOOLS,
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
        elif not any(
            event_content_eq(cast("ActionEvent", payload), d, ignore_thought=True) for d in distinct
        ):
            distinct.append(cast("ActionEvent", payload))
    return probe_count >= 2 and len(distinct) >= distinct_edits


def _failure_prefix(text: str) -> str:
    collapsed = " ".join((text or "").strip().split())
    return collapsed[:_FAILURE_PREFIX_CHARS]


def _no_effect_refusal_code_from_error(event: AgentErrorEvent) -> str | None:
    for text in (event.error, event.detail or ""):
        if text in _BARREN_NO_EFFECT_ERROR_CODES:
            return text
    combined = f"{event.error}\n{event.detail or ''}"
    for code in _BARREN_NO_EFFECT_ERROR_CODES:
        if code in combined:
            return code
    lowered = combined.lower()
    for code in ("no_op_edit", "no_op_write"):
        if code in lowered:
            return code
    return None


def _no_effect_refusal_code_from_observation(event: ObservationEvent) -> str | None:
    result = event.tool_result
    structured_kind = ""
    if isinstance(result.structured, dict):
        structured_kind = str(result.structured.get("kind") or "")
    for text in (result.error or "", structured_kind):
        if text in _BARREN_NO_EFFECT_ERROR_CODES:
            return text
    combined = f"{result.error or ''}\n{result.content}\n{structured_kind}"
    for code in _BARREN_NO_EFFECT_ERROR_CODES:
        if code in combined:
            return code
    lowered = combined.lower()
    for code in ("no_op_edit", "no_op_write"):
        if code in lowered:
            return code
    return None


def barren_streak_no_progress(
    events: list[Event],
    *,
    turns: int = BARREN_STREAK_TURNS,
    identical_failures: int = BARREN_STREAK_IDENTICAL_FAILURES,
) -> bool:
    """True for K recent read/reasoning-only turns with M identical failures.

    This catches a model that loops on think/file_list/file_read/search/extract
    without mutating anything, while repeatedly seeing the same failing result.
    A single action outside the narrow read-only set inside the K-turn window
    resets the streak; varying successful research/read results never trip it.
    """
    if turns <= 0 or identical_failures <= 0:
        return False
    window = _after_last_user_message(events)
    actions = [e for e in window if isinstance(e, ActionEvent) and e.tool_call is not None]
    if len(actions) < turns:
        return False
    recent_actions = actions[-turns:]

    recent_action_ids = {a.id for a in recent_actions}
    no_effect_failures: dict[str, str] = {}
    prefixes: list[str] = []
    for event in window:
        if isinstance(event, AgentErrorEvent) and event.action_id in recent_action_ids:
            code = _no_effect_refusal_code_from_error(event)
            if code is not None:
                no_effect_failures[event.action_id] = code
            prefix = _failure_prefix(event.detail or event.error)
            if prefix:
                prefixes.append(prefix)
        elif (
            isinstance(event, ObservationEvent)
            and event.action_id in recent_action_ids
            and not event.tool_result.success
        ):
            code = _no_effect_refusal_code_from_observation(event)
            if code is not None:
                no_effect_failures[event.action_id or ""] = code
            prefix = _failure_prefix(event.tool_result.error or event.tool_result.content)
            if prefix:
                prefixes.append(prefix)
    for action in recent_actions:
        tool_name = action.tool_call.tool_name
        if tool_name in _BARREN_READ_ONLY_TOOLS:
            continue
        if tool_name in _BARREN_NO_EFFECT_MUTATING_TOOLS and action.id in no_effect_failures:
            prefixes.append(no_effect_failures[action.id])
            continue
        return False
    if len(prefixes) < identical_failures:
        return False
    return any(count >= identical_failures for count in Counter(prefixes).values())


def no_progress_detected(events: list[Event]) -> bool:
    return repeated_verify_no_progress(events) or barren_streak_no_progress(events)
