"""Stuck detection — agent-loop-contract.md §10.5.

StuckDetector unit tests (table-driven over the four patterns, using
event_content_eq) + loop integration (STUCK then resume on a new message).
"""

from __future__ import annotations

import functools

import loop_stuck_no_progress_cases as _no_progress
import loop_stuck_recovery_cases as _recovery
import pytest
from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from disco.core.loop import StuckDetector, StuckThresholds, signals
from disco.core.loop.control import Disp
from disco.core.loop.stuck import barren_streak_no_progress
from event_fakes import action, agent_error, agent_msg, observation, user_msg
from loop_fakes import (
    ScriptedAgent,
    build_loop,
)

CID = "conv"


def _pairs_ao(n, thought="same"):
    """n identical action→observation pairs."""
    out = []
    for _ in range(n):
        out += [action(thought=thought), observation(content="ok")]
    return out


# ---- pattern 1: repeated action→observation ---------------------------------


def test_repeated_action_observation_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3))
    assert d.is_stuck(_pairs_ao(3)) is True
    assert d.is_stuck(_pairs_ao(2)) is False  # below threshold


def _file_read_pair(
    *,
    path: str = "src/index.css",
    content: str = "[lines 1-1 of 1]\n1\tbody {}",
    limit: int | None = None,
):
    args: dict[str, object] = {"path": path}
    if limit is not None:
        args["limit"] = limit
    read = action(thought="inspect current bytes", tool="file_read", args=args)
    return [
        read,
        observation(
            action_id=read.id,
            tool="file_read",
            content=content,
        ),
    ]


def test_exact_unchanged_file_read_arms_escape_before_third_call():
    detector = StuckDetector()
    one = _file_read_pair()
    assert detector.evaluate(one).is_stuck is False

    result = detector.evaluate(one + _file_read_pair())
    assert result.is_stuck is True
    assert result.reason == "repeated_unchanged_file_read"


@pytest.mark.parametrize(
    "second",
    [
        _file_read_pair(path="src/App.css"),
        _file_read_pair(limit=40),
        _file_read_pair(content="[lines 1-1 of 1]\n1\tbody { color: red; }"),
    ],
    ids=["different-resource", "different-range", "changed-bytes"],
)
def test_exact_read_breaker_preserves_truthful_variation(second):
    assert StuckDetector().evaluate(_file_read_pair() + second).is_stuck is False


def test_exact_read_breaker_accepts_host_owned_f9_duplicate_receipt():
    from disco.core.loop.dedup import _F9_POINTER_TEMPLATE

    dedup = _F9_POINTER_TEMPLATE.format(tool_name="file_read", arg_summary="src/index.css")
    result = StuckDetector().evaluate(_file_read_pair() + _file_read_pair(content=dedup))
    assert result.reason == "repeated_unchanged_file_read"


def test_exact_read_breaker_resets_at_typed_escape_and_on_productive_action():
    repeated = _file_read_pair() + _file_read_pair()
    escaped = repeated + [
        StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape"),
        *_file_read_pair(),
    ]
    assert StuckDetector().evaluate(escaped).is_stuck is False

    write = action(
        thought="change the application",
        tool="file_write",
        args={"path": "src/index.css", "content": "body { color: red; }"},
    )
    progressed = repeated[:2] + [
        write,
        observation(action_id=write.id, tool="file_write", content="wrote changed bytes"),
        *_file_read_pair(),
    ]
    assert StuckDetector().evaluate(progressed).is_stuck is False


def test_exact_read_breaker_does_not_lower_generic_tool_threshold():
    detector = StuckDetector()
    shell_pairs = []
    for _ in range(2):
        shell = action(thought="check", tool="shell", args={"command": "npm test"})
        shell_pairs += [
            shell,
            observation(action_id=shell.id, tool="shell", content="all tests passed"),
        ]
    assert detector.evaluate(shell_pairs).is_stuck is False


# ---- pattern 2: repeated action→error ---------------------------------------


def test_repeated_action_error_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = []
    for _ in range(3):
        events += [action(thought="retry"), agent_error("same failure")]
    assert d.is_stuck(events) is True


# ---- pattern 2: the stuck detector compares RAW args, not rendered/snipped ones --
#
# Bug 14 (Build Soak repair #7) regression pin. The LLM-context view snips any
# tool arg > _ARG_SNIP_CHARS to a SHORT placeholder marker keyed on the arg's
# LENGTH (`<{n} chars …>`). Two file_replace_lines calls with DISTINCT new_text of
# IDENTICAL length therefore RENDER to the SAME marker — but they are NOT the same
# action. The detector MUST compare the raw ActionEvent.tool_call.arguments (which
# differ), never the rendered/snipped view, or it would falsely flag two distinct
# large edits as a repeat and STUCK a legitimately-progressing run. (The Bug 14 fix
# is the executor-boundary elision guard; this pins the detector's correctness so a
# future "compare rendered args" refactor can't silently reintroduce the false STUCK.)


def _replace_action(new_text: str):  # noqa: ANN202
    return action(
        thought="patching",
        tool="file_replace_lines",
        args={"path": "src/app.js", "start_line": 1, "end_line": 40, "new_text": new_text},
    )


def test_distinct_large_edits_snip_to_same_marker_but_are_not_stuck():
    from disco.core.events import _ARG_SNIP_CHARS, _snip_args

    big = _ARG_SNIP_CHARS + 100
    text_a = "a" * big
    text_b = "b" * big  # distinct content, IDENTICAL length
    # Premise: the rendered/snipped args collapse to the SAME marker (length-keyed).
    marker_a = _snip_args({"new_text": text_a})["new_text"]
    marker_b = _snip_args({"new_text": text_b})["new_text"]
    assert marker_a == marker_b
    # ...yet the raw args differ.
    assert text_a != text_b

    d = StuckDetector(StuckThresholds(repeat_action_error=2))
    events = [
        _replace_action(text_a),
        agent_error("argument contains the elision placeholder"),
        _replace_action(text_b),
        agent_error("argument contains the elision placeholder"),
    ]
    # Two DISTINCT large edits → NOT stuck (the detector reads raw args, not the
    # rendered marker that would make them look identical).
    assert d.is_stuck(events) is False


def test_repeated_identical_large_edit_is_still_stuck():
    """The other half of the contract: the detector MUST still catch a TRULY
    identical repeated action (same raw new_text) — Bug 14 must not weaken it."""
    from disco.core.events import _ARG_SNIP_CHARS

    same = "z" * (_ARG_SNIP_CHARS + 100)
    d = StuckDetector(StuckThresholds(repeat_action_error=2))
    events = [
        _replace_action(same),
        agent_error("argument contains the elision placeholder"),
        _replace_action(same),
        agent_error("argument contains the elision placeholder"),
    ]
    assert d.is_stuck(events) is True


# ---- pattern 2: NONCRITICAL bookkeeping tools are exempt from action→error ----
#
# A malformed `update_plan_progress` (a cosmetic, declarative plan-tracker tool —
# signals._NONCRITICAL_FAILURE_TOOLS) repeatedly failing schema validation is NOT
# "stuck on the task": some models (MiniMax-M3) intermittently emit steps=[""]. It
# must NOT trip the FATAL `repeated_action_error` and kill an otherwise-productive
# build (the dedicated bookkeeping gate is the right backstop for pure spam). Mirrors
# the existing count_recent_failures exclusion (signals.py).


def test_noncritical_update_plan_progress_error_loop_not_stuck():
    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = []
    for _ in range(5):  # well past the threshold
        events += [
            action(thought="track", tool="update_plan_progress", args={"steps": [""]}),
            agent_error("steps.0: Input should be a valid object"),
        ]
    assert d.is_stuck(events) is False  # exempt — cosmetic bookkeeping, not task-stuck


def test_mixed_noncritical_and_real_error_still_detects_real_loop():
    # A real execution tool (file_write) erroring identically 3x IS stuck, even when
    # interleaved with exempt update_plan_progress errors — the exemption only drops
    # the cosmetic pairs; the real loop remains detectable.
    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = []
    for _ in range(3):
        events += [
            action(thought="track", tool="update_plan_progress", args={"steps": [""]}),
            agent_error("steps.0: Input should be a valid object"),
            action(thought="write", tool="file_write", args={"path": "a.txt", "content": "x"}),
            agent_error("permission denied: read-only filesystem"),
        ]
    assert d.is_stuck(events) is True  # the real file_write error loop still fires


# ---- pattern 3: agent monologue ---------------------------------------------


def test_agent_monologue_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(agent_monologue=4))
    assert d.is_stuck([agent_msg("a"), agent_msg("b"), agent_msg("c"), agent_msg("d")]) is True
    assert d.is_stuck([agent_msg("a"), agent_msg("b")]) is False


# ---- pattern 4: alternating A-B-A-B -----------------------------------------


def test_alternating_actions_trigger_at_threshold():
    d = StuckDetector(StuckThresholds(alternating=3))
    seq = []
    for _ in range(3):
        seq += [action(thought="A"), action(thought="B")]  # A,B repeated 3x
    assert d.is_stuck(seq) is True
    # Identical (not alternating) is pattern 1, not pattern 4:
    assert d._alternating([action(thought="A")] * 6) is False


# ---- reset + window ---------------------------------------------------------


def test_user_message_resets_stuck():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3))
    events = _pairs_ao(3) + [user_msg("new instruction")] + _pairs_ao(1)
    assert d.is_stuck(events) is False  # cleared after the user message


def test_events_outside_window_dont_count():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3, scan_window=4))
    # 3 stuck pairs, but only the last `scan_window`(=4) events are inspected by
    # the loop; here we emulate by trimming as the loop's _recent() would.
    events = _pairs_ao(3)  # 6 events
    assert d.is_stuck(events[-4:]) is False  # only 2 pairs visible in the window


def test_probe_spin_trips_on_varying_server_status_output():
    d = StuckDetector(StuckThresholds(probe_spin_calls=12))
    events = []
    for i in range(12):
        probe = action(
            thought=f"poll {i}",
            tool="server_status",
            args={"url": "http://127.0.0.1:5173"},
        )
        events += [
            probe,
            observation(
                action_id=probe.id,
                tool="server_status",
                content=f"server still starting; attempt={i}",
            ),
        ]

    result = d.evaluate(events)

    assert result.is_stuck is True
    assert result.reason == "probe_spin"


def _numbered_read(
    path: str,
    start: int,
    stop: int,
    *,
    text_prefix: str = "line",
    offset: int | None = None,
    limit: int | None = None,
    total: int | None = None,
):
    args = {"path": path}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    read = action(thought=f"read {start}-{stop}", tool="file_read", args=args)
    numbered = "\n".join(f"{line:>3}\t{text_prefix}-{line}" for line in range(start, stop + 1))
    content = f"[lines {start}-{stop} of {total if total is not None else stop}]\n{numbered}"
    return [read, observation(action_id=read.id, tool="file_read", content=content)]


def _header_only_read(path: str, header: str, *, offset: int | None = None):
    args = {"path": path}
    if offset is not None:
        args["offset"] = offset
    read = action(thought="read empty range", tool="file_read", args=args)
    return [read, observation(action_id=read.id, tool="file_read", content=header)]


def _read_churn_nudge(path: str = "styles.css", *, count: object = 5):
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="typed read churn instruction"),
        meta={"diagnostic": signals.READ_CHURN_NUDGE_DIAGNOSTIC, "path": path, "count": count},
    )


def test_redundant_read_after_churn_nudge_trips_h340_exact_boundary():
    events = [_read_churn_nudge("/workspace/css/styles.css")]
    events += _numbered_read("css/styles.css", 1, 699, total=699)
    assert StuckDetector().evaluate(events).is_stuck is False

    events += _numbered_read(
        "/workspace/css/styles.css", 193, 312, offset=193, limit=120, total=699
    )
    result = StuckDetector().evaluate(events)

    assert result.is_stuck is True
    assert result.reason == "redundant_read_after_churn_nudge"


def test_redundant_read_after_churn_nudge_rejects_partial_first_retry():
    events = [_read_churn_nudge()]
    events += _numbered_read("styles.css", 1, 20, offset=1, limit=20, total=100)

    assert StuckDetector().evaluate(events).reason == "redundant_read_after_churn_nudge"


@pytest.mark.parametrize(
    "malformed",
    [
        "[lines 2-3 of 3]\n1\ta\n2\tb\n3\tc",
        "[lines 1-3 of 3]\n1\ta\n2\tb\n2\tb-again\n3\tc",
        "[lines 1-3 of 3]\n1\ta\n2\tb\n4\td",
        "[lines 1-3 of 3] trailing garbage\n1\ta\n2\tb\n3\tc",
        "[lines 1-3 of 3]\n1\ta\nunnumbered garbage\n2\tb\n3\tc",
    ],
    ids=[
        "wrong-header-range",
        "duplicate-line",
        "out-of-range-line",
        "header-suffix-garbage",
        "unnumbered-body-garbage",
    ],
)
def test_redundant_read_after_churn_nudge_malformed_whole_result_is_inert(malformed):
    marker = _read_churn_nudge()
    malformed_read = action(
        thought="whole read with malformed tool output",
        tool="file_read",
        args={"path": "styles.css"},
    )
    events = [
        marker,
        malformed_read,
        observation(action_id=malformed_read.id, tool="file_read", content=malformed),
    ]
    assert StuckDetector().evaluate(events).is_stuck is False

    # The first subsequent honest whole read establishes the baseline; malformed
    # evidence above must not make this legitimate retry look redundant.
    events += _numbered_read("styles.css", 1, 3, total=3)
    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_huge_total_tiny_body_is_bounded_and_inert():
    marker = _read_churn_nudge()
    read = action(thought="budgeted huge read", tool="file_read", args={"path": "styles.css"})
    content = "[lines 1-2 of 1000000000000; read more with offset=3]\n1\ta\n2\tb"
    events = [marker, read, observation(action_id=read.id, tool="file_read", content=content)]

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_giant_header_integer_is_inert():
    marker = _read_churn_nudge()
    read = action(thought="malformed giant total", tool="file_read", args={"path": "styles.css"})
    giant = "9" * 5000
    events = [
        marker,
        read,
        observation(
            action_id=read.id,
            tool="file_read",
            content=f"[lines 1-1 of {giant}]\n1\ta",
        ),
    ]

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_giant_body_integer_is_inert():
    marker = _read_churn_nudge()
    read = action(thought="malformed giant line", tool="file_read", args={"path": "styles.css"})
    giant = "9" * 5000
    events = [
        marker,
        read,
        observation(
            action_id=read.id,
            tool="file_read",
            content=f"[lines 1-1 of 1]\n{giant}\ta",
        ),
    ]

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_zero_limit_reread_retrips():
    events = [_read_churn_nudge()]
    events += _numbered_read("styles.css", 1, 2, total=2)
    zero = action(
        thought="zero-line reread",
        tool="file_read",
        args={"path": "styles.css", "limit": 0},
    )
    events += [
        zero,
        observation(
            action_id=zero.id,
            tool="file_read",
            content="[lines 1-0 of 2; read more with offset=1]\n",
        ),
    ]

    assert StuckDetector().evaluate(events).reason == "redundant_read_after_churn_nudge"


def test_redundant_read_after_churn_nudge_empty_offset_reread_retrips():
    marker = _read_churn_nudge("empty.txt")
    whole = action(thought="whole empty", tool="file_read", args={"path": "empty.txt"})
    offset = action(
        thought="redundant empty offset", tool="file_read", args={"path": "empty.txt", "offset": 2}
    )
    events = [
        marker,
        whole,
        observation(action_id=whole.id, tool="file_read", content="[lines 1-0 of 0]\n"),
        offset,
        observation(action_id=offset.id, tool="file_read", content="[lines 2-1 of 0]\n"),
    ]

    assert StuckDetector().evaluate(events).reason == "redundant_read_after_churn_nudge"


@pytest.mark.parametrize("spoof", ["source", "diagnostic", "path", "count"])
def test_redundant_read_after_churn_nudge_ignores_untrusted_marker(spoof):
    marker = _read_churn_nudge()
    if spoof == "source":
        marker = marker.model_copy(update={"source": EventSource.AGENT})
    elif spoof == "diagnostic":
        marker = marker.model_copy(
            update={"meta": {**marker.meta, "diagnostic": "read_churn_nudeg"}}
        )
    elif spoof == "path":
        marker = marker.model_copy(update={"meta": {**marker.meta, "path": ""}})
    else:
        marker = marker.model_copy(update={"meta": {**marker.meta, "count": "5"}})
    events = [marker]
    events += _numbered_read("styles.css", 1, 20, total=20)
    events += _numbered_read("styles.css", 1, 10, offset=1, limit=10, total=20)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_is_bound_to_exact_path():
    events = [_read_churn_nudge("styles.css")]
    events += _numbered_read("other.css", 1, 20, total=20)
    events += _numbered_read("other.css", 1, 10, offset=1, limit=10, total=20)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_failed_read_is_inert():
    marker = _read_churn_nudge()
    read = action(thought="failed whole read", tool="file_read", args={"path": "styles.css"})
    events = [marker, read, observation(action_id=read.id, tool="file_read", success=False)]

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_opaque_read_is_inert():
    marker = _read_churn_nudge()
    read = action(thought="opaque whole read", tool="file_read", args={"path": "styles.css"})
    events = [marker, read, observation(action_id=read.id, tool="file_read", content="<binary>")]

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_ignores_non_environment_observation():
    events = [_read_churn_nudge()]
    events += _numbered_read("styles.css", 1, 20, total=20)
    read, result = _numbered_read("styles.css", 1, 10, offset=1, limit=10, total=20)
    events += [read, result.model_copy(update={"source": EventSource.AGENT})]

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_productive_action_recovers():
    events = [_read_churn_nudge()]
    events += _numbered_read("styles.css", 1, 20, total=20)
    events += _numbered_read("styles.css", 1, 10, offset=1, limit=10, total=20)
    write = action(
        thought="act on the known bytes",
        tool="file_write",
        args={"path": "styles.css", "content": "changed"},
    )
    events += [write, observation(action_id=write.id, tool="file_write", content="wrote")]

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_unpaired_productive_result_does_not_recover():
    events = [_read_churn_nudge()]
    events += _numbered_read("styles.css", 1, 20, total=20)
    events += _numbered_read("styles.css", 1, 10, offset=1, limit=10, total=20)
    events.append(observation(action_id="missing", tool="file_write", content="wrote"))

    assert StuckDetector().evaluate(events).reason == "redundant_read_after_churn_nudge"


@pytest.mark.parametrize("change", ["content", "total"])
def test_redundant_read_after_churn_nudge_changed_file_recovers(change):
    events = [_read_churn_nudge()]
    events += _numbered_read("styles.css", 1, 20, total=20)
    if change == "content":
        events += _numbered_read(
            "styles.css", 1, 10, text_prefix="changed", offset=1, limit=10, total=20
        )
    else:
        events += _numbered_read("styles.css", 1, 21, total=21)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_after_churn_nudge_escape_rearms_then_retrips():
    events = [_read_churn_nudge()]
    events += _numbered_read("styles.css", 1, 20, total=20)
    events += _numbered_read("styles.css", 1, 10, offset=1, limit=10, total=20)
    assert StuckDetector().evaluate(events).reason == "redundant_read_after_churn_nudge"

    events.append(StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape"))
    assert StuckDetector().evaluate(events).is_stuck is False
    events += _numbered_read("styles.css", 11, 20, offset=11, limit=10, total=20)
    assert StuckDetector().evaluate(events).reason == "redundant_read_after_churn_nudge"


def _coverage_one_hit_below_threshold(path: str = "styles.css") -> list[Event]:
    events = _numbered_read(path, 1, 20, total=20)
    for limit in (10, 11, 12):
        events += _numbered_read(path, 1, 5, offset=1, limit=limit, total=20)
    assert StuckDetector().evaluate(events).is_stuck is False
    return events


def test_trusted_churn_nudge_rearms_coverage_for_its_permitted_whole_read():
    """H391: the nudge's allowed baseline cannot inherit pre-nudge hit counts."""
    events = _coverage_one_hit_below_threshold()
    events.append(_read_churn_nudge("/workspace/styles.css"))
    events += _numbered_read("./styles.css", 1, 20, total=20)

    assert StuckDetector().evaluate(events).is_stuck is False

    recovered = list(events)
    write = action(
        thought="act on the permitted baseline",
        tool="file_write",
        args={"path": "styles.css", "content": "changed"},
    )
    recovered += [write, observation(action_id=write.id, tool="file_write", content="wrote")]
    assert StuckDetector().evaluate(recovered).is_stuck is False

    # The allowance is exactly one baseline read. A subsequent unchanged slice
    # still trips the stricter typed-nudge breaker before generic coverage.
    events += _numbered_read("styles.css", 1, 5, offset=1, limit=5, total=20)
    assert StuckDetector().evaluate(events).reason == "redundant_read_after_churn_nudge"


def test_trusted_churn_nudge_permits_whole_read_at_coverage_threshold_one():
    """H404: the allowed baseline is not itself a generic redundant hit."""
    events = _numbered_read("styles.css", 1, 20, total=20)
    events.append(_read_churn_nudge("/workspace/styles.css"))
    events += _numbered_read("./styles.css", 1, 20, total=20)
    detector = StuckDetector(StuckThresholds(redundant_read_coverage=1))

    assert detector.evaluate(events).is_stuck is False

    events += _numbered_read("styles.css", 1, 5, offset=1, limit=5, total=20)
    assert detector.evaluate(events).reason == "redundant_read_after_churn_nudge"


def test_h391_frozen_window_38_61_semantic_replay():
    """The exact live overlap/escape/nudge shape cannot kill its allowed read."""
    events: list[Event] = [
        observation(
            action_id="action-before-window",
            tool="file_read",
            content="[lines 129-208 of 623]\n129\tline-129",
        )
    ]
    events += _numbered_read("/workspace/styles.css", 209, 238, offset=209, limit=30, total=623)
    events += _numbered_read("/workspace/styles.css", 220, 234, offset=220, limit=15, total=623)
    events += _numbered_read("/workspace/styles.css", 228, 247, offset=228, limit=20, total=623)
    events += [
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content="take a genuinely different action"),
        ),
        StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape"),
    ]
    events += _numbered_read("/workspace/styles.css", 1, 623, total=623)
    events += _numbered_read("/workspace/styles.css", 205, 214, offset=205, limit=10, total=623)
    events += _numbered_read("/workspace/styles.css", 219, 238, offset=219, limit=20, total=623)
    events += _numbered_read("/workspace/styles.css", 215, 224, offset=215, limit=10, total=623)
    events += _numbered_read("/workspace/styles.css", 224, 238, offset=224, limit=15, total=623)
    events += _numbered_read("/workspace/styles.css", 210, 214, offset=210, limit=5, total=623)
    marker = _read_churn_nudge("/workspace/styles.css")
    events.append(marker)
    events += _numbered_read("/workspace/styles.css", 1, 623, total=623)
    assert len(events) == 24

    assert StuckDetector().evaluate(events).is_stuck is False

    without_nudge = [event for event in events if event is not marker]
    assert StuckDetector().evaluate(without_nudge).reason == "redundant_read_coverage"


def test_whole_reread_without_churn_nudge_still_trips_coverage_threshold_one():
    events = _numbered_read("styles.css", 1, 20, total=20)
    events += _numbered_read("styles.css", 1, 20, total=20)

    result = StuckDetector(StuckThresholds(redundant_read_coverage=1)).evaluate(events)

    assert result.reason == "redundant_read_coverage"


@pytest.mark.parametrize("kind", ["malformed", "opaque", "failed", "unpaired"])
def test_invalid_read_cannot_consume_threshold_one_nudge_baseline(kind):
    events = _numbered_read("styles.css", 1, 20, total=20)
    events.append(_read_churn_nudge("styles.css"))
    invalid = action(thought=f"{kind} whole read", tool="file_read", args={"path": "styles.css"})
    if kind == "malformed":
        giant = "9" * 5000
        result = observation(
            action_id=invalid.id,
            tool="file_read",
            content=f"[lines 1-20 of 20]\n{giant}\tline-1",
        )
    elif kind == "opaque":
        result = observation(action_id=invalid.id, tool="file_read", content="<binary>")
    elif kind == "failed":
        result = observation(action_id=invalid.id, tool="file_read", success=False)
    else:
        result = observation(
            action_id="missing-action",
            tool="file_read",
            content="[lines 1-20 of 20]\n1\tline-1",
        )
    events += [invalid, result]
    events += _numbered_read("styles.css", 1, 20, total=20)

    assert (
        StuckDetector(StuckThresholds(redundant_read_coverage=1)).evaluate(events).is_stuck is False
    )


def test_pre_nudge_action_cannot_claim_threshold_one_baseline_allowance():
    events = _numbered_read("styles.css", 1, 20, total=20)
    stale_action, stale_result = _numbered_read("styles.css", 1, 20, total=20)
    events += [stale_action, _read_churn_nudge("styles.css"), stale_result]

    result = StuckDetector(StuckThresholds(redundant_read_coverage=1)).evaluate(events)

    assert result.reason == "redundant_read_coverage"


@pytest.mark.parametrize(
    "spoof",
    ["source", "diagnostic", "path", "count-string", "count-bool", "count-small"],
)
def test_untrusted_churn_nudge_cannot_rearm_generic_coverage(spoof):
    events = _coverage_one_hit_below_threshold()
    marker = _read_churn_nudge()
    if spoof == "source":
        marker = marker.model_copy(update={"source": EventSource.AGENT})
    elif spoof == "diagnostic":
        marker = marker.model_copy(
            update={"meta": {**marker.meta, "diagnostic": "read_churn_nudeg"}}
        )
    elif spoof == "path":
        marker = marker.model_copy(update={"meta": {**marker.meta, "path": ""}})
    elif spoof == "count-string":
        marker = marker.model_copy(update={"meta": {**marker.meta, "count": "5"}})
    elif spoof == "count-bool":
        marker = marker.model_copy(update={"meta": {**marker.meta, "count": True}})
    else:
        marker = marker.model_copy(update={"meta": {**marker.meta, "count": 4}})
    events.append(marker)
    events += _numbered_read("styles.css", 1, 20, total=20)

    assert StuckDetector().evaluate(events).reason == "redundant_read_coverage"
    assert (
        StuckDetector(StuckThresholds(redundant_read_coverage=1)).evaluate(events).reason
        == "redundant_read_coverage"
    )


def test_trusted_churn_nudge_does_not_rearm_another_path():
    events = _coverage_one_hit_below_threshold("styles.css")
    events.append(_read_churn_nudge("other.css"))
    events += _numbered_read("styles.css", 1, 20, total=20)

    assert StuckDetector().evaluate(events).reason == "redundant_read_coverage"
    assert (
        StuckDetector(StuckThresholds(redundant_read_coverage=1)).evaluate(events).reason
        == "redundant_read_coverage"
    )


def test_trusted_churn_nudge_rearms_named_empty_file_header_coverage():
    events = _header_only_read("empty.txt", "[lines 1-0 of 0]")
    for offset in (2, 3, 4):
        events += _header_only_read("empty.txt", f"[lines {offset}-0 of 0]", offset=offset)
    assert StuckDetector().evaluate(events).is_stuck is False

    events.append(_read_churn_nudge("/workspace/empty.txt"))
    events += _header_only_read("./empty.txt", "[lines 1-0 of 0]")

    assert StuckDetector().evaluate(events).is_stuck is False
    assert (
        StuckDetector(StuckThresholds(redundant_read_coverage=1)).evaluate(events).is_stuck is False
    )


def test_redundant_read_coverage_trips_on_varied_overlapping_offsets():
    """H336: varied ranges still cycle over the same known line four times."""
    events = _numbered_read("styles.css", 1, 20)
    events += _numbered_read("./styles.css", 1, 12, offset=1, total=20)
    events += _numbered_read("workspace/styles.css", 5, 15, offset=5, total=20)
    events += _numbered_read("/workspace/styles.css", 10, 20, offset=10, total=20)
    events += _numbered_read("/workspace/styles.css", 1, 20, total=20)

    result = StuckDetector().evaluate(events)

    assert result.is_stuck is True
    assert result.reason == "redundant_read_coverage"


def test_redundant_read_coverage_allows_finite_full_overlap_tail_validation():
    """H338 live shape: four reads, but no line is redundantly observed four times."""
    events = _numbered_read("styles.css", 1, 624)
    events += _numbered_read("styles.css", 190, 389, offset=190, limit=200, total=624)
    events += _numbered_read("styles.css", 1, 624, total=624)
    events += _numbered_read("styles.css", 190, 289, offset=190, limit=100, total=624)
    events += _numbered_read("styles.css", 290, 624, offset=290, total=624)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_retains_h336_multi_traversal_spin():
    events = _numbered_read("index.html", 1, 328)
    events += _numbered_read("index.html", 86, 185, offset=86, limit=100, total=328)
    events += _numbered_read("index.html", 186, 285, offset=186, limit=100, total=328)
    events += _numbered_read("index.html", 286, 328, offset=286, limit=50, total=328)
    events += _numbered_read("index.html", 1, 328, total=328)
    events += _numbered_read("index.html", 87, 286, offset=87, limit=200, total=328)
    events += _numbered_read("index.html", 286, 328, offset=286, total=328)
    events += _numbered_read("index.html", 1, 100, offset=1, limit=100, total=328)

    result = StuckDetector().evaluate(events)

    assert result.is_stuck is True
    assert result.reason == "redundant_read_coverage"


def test_redundant_read_coverage_stays_below_threshold_at_three():
    events = _numbered_read("styles.css", 1, 20, total=20)
    for limit in (10, 11, 12):
        events += _numbered_read("styles.css", 1, 5, offset=1, limit=limit, total=20)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_rearms_at_stuck_escape_then_retrips():
    events = _numbered_read("styles.css", 1, 20, total=20)
    for limit in (10, 11, 12, 13):
        events += _numbered_read("styles.css", 1, 5, offset=1, limit=limit, total=20)
    assert StuckDetector().evaluate(events).reason == "redundant_read_coverage"

    events.append(StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape"))
    listing = action(thought="change approach", tool="file_list", args={"path": "."})
    events.extend(
        [listing, observation(action_id=listing.id, tool="file_list", content="styles.css")]
    )
    assert StuckDetector().evaluate(events).is_stuck is False

    for limit in (20, 21, 22):
        events += _numbered_read("styles.css", 1, 5, offset=1, limit=limit, total=20)
    assert StuckDetector().evaluate(events).is_stuck is False
    events += _numbered_read("styles.css", 1, 5, offset=1, limit=23, total=20)
    assert StuckDetector().evaluate(events).reason == "redundant_read_coverage"


def test_redundant_read_coverage_allows_legitimate_pagination():
    events = []
    for start in (1, 6, 11, 16, 21, 26):
        events += _numbered_read("big.css", start, start + 4, offset=start, total=30)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_resets_on_changed_lines():
    events = _numbered_read("styles.css", 1, 5)
    for limit in (10, 11, 12):
        events += _numbered_read("styles.css", 1, 5, limit=limit)
    events += _numbered_read("styles.css", 1, 5, text_prefix="changed", limit=20)
    for limit in (21, 22, 23):
        events += _numbered_read("styles.css", 1, 5, text_prefix="changed", limit=limit)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_recovery_after_threshold_changed_lines():
    events = _numbered_read("styles.css", 1, 5)
    for limit in (10, 11, 12, 13):
        events += _numbered_read("styles.css", 1, 5, limit=limit)
    events += _numbered_read("styles.css", 1, 5, text_prefix="changed", limit=20)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_recovery_after_threshold_new_range():
    events = _numbered_read("styles.css", 1, 5, total=10)
    for limit in (10, 11, 12, 13):
        events += _numbered_read("styles.css", 1, 5, limit=limit, total=10)
    events += _numbered_read("styles.css", 6, 10, offset=6, total=10)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_resets_on_productive_action():
    events = _numbered_read("styles.css", 1, 5)
    for limit in (10, 11, 12):
        events += _numbered_read("styles.css", 1, 5, limit=limit)
    write = action(
        thought="change it",
        tool="file_write",
        args={"path": "styles.css", "content": "new"},
    )
    events.extend([write, observation(action_id=write.id, tool="file_write", content="wrote")])
    events += _numbered_read("styles.css", 1, 5, limit=20)
    for limit in (21, 22, 23):
        events += _numbered_read("styles.css", 1, 5, limit=limit)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_recovery_after_threshold_productive_action():
    events = _numbered_read("styles.css", 1, 5)
    for limit in (10, 11, 12, 13):
        events += _numbered_read("styles.css", 1, 5, limit=limit)
    write = action(
        thought="repair",
        tool="file_write",
        args={"path": "styles.css", "content": "changed"},
    )
    events.extend([write, observation(action_id=write.id, tool="file_write", content="wrote")])

    assert StuckDetector().evaluate(events).is_stuck is False


def _failed_or_unpaired_write(kind: str):
    write = action(
        thought="attempt change",
        tool="file_write",
        args={"path": "styles.css", "content": "new"},
    )
    if kind == "error":
        return [write, agent_error("write rejected", action_id=write.id)]
    if kind == "success_false":
        return [
            write,
            observation(
                action_id=write.id,
                tool="file_write",
                content="write rejected",
                success=False,
            ),
        ]
    assert kind == "unpaired"
    return [write]


@pytest.mark.parametrize("kind", ["error", "success_false", "unpaired"])
def test_redundant_read_coverage_failed_or_unpaired_write_does_not_reset(kind):
    events = _numbered_read("styles.css", 1, 5)
    for limit in (10, 11, 12):
        events += _numbered_read("styles.css", 1, 5, limit=limit)
    events += _failed_or_unpaired_write(kind)
    events += _numbered_read("styles.css", 1, 5, limit=13)

    result = StuckDetector().evaluate(events)

    assert result.is_stuck is True
    assert result.reason == "redundant_read_coverage"


def test_redundant_read_coverage_ignores_other_paths_and_opaque_output():
    events = []
    for index in range(6):
        events += _numbered_read(f"file-{index}.css", 1, 5)
    for index in range(6):
        opaque = action(
            thought="binary",
            tool="file_read",
            args={"path": "blob.bin", "offset": index + 1},
        )
        events.extend(
            [opaque, observation(action_id=opaque.id, tool="file_read", content="<binary>")]
        )

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_keeps_backslash_filename_distinct_on_linux():
    events = _numbered_read("dir/file.css", 1, 5, limit=10)
    events += _numbered_read(r"dir\file.css", 1, 5, limit=11)
    events += _numbered_read("dir/file.css", 1, 5, limit=12)
    events += _numbered_read(r"dir\file.css", 1, 5, limit=13)
    events += _numbered_read("dir/file.css", 1, 5, limit=14)

    assert StuckDetector().evaluate(events).is_stuck is False


def test_redundant_read_coverage_new_path_rearms_global_streak():
    events = _numbered_read("a.css", 1, 5)
    for limit in (10, 11, 12, 13):
        events += _numbered_read("a.css", 1, 5, limit=limit)
    events += _numbered_read("b.css", 1, 5)

    assert StuckDetector().evaluate(events).is_stuck is False

    # Coverage for A is retained, but the new-path progress re-armed its count:
    # three more redundant reads remain below the four-read threshold.
    for limit in (20, 21, 22):
        events += _numbered_read("a.css", 1, 5, limit=limit)
    assert StuckDetector().evaluate(events).is_stuck is False
    events += _numbered_read("a.css", 1, 5, limit=23)
    assert StuckDetector().evaluate(events).reason == "redundant_read_coverage"


def test_redundant_read_coverage_trips_on_empty_file_header_only_reads():
    events = _header_only_read("empty.txt", "[lines 1-0 of 0]")
    for offset in (2, 3, 4, 5):
        events += _header_only_read("empty.txt", f"[lines {offset}-0 of 0]", offset=offset)

    result = StuckDetector().evaluate(events)

    assert result.is_stuck is True
    assert result.reason == "redundant_read_coverage"


def test_redundant_read_coverage_trips_on_varied_past_eof_reads():
    events = _numbered_read("tiny.txt", 1, 2)
    for offset in (100, 101, 102, 103):
        events += _header_only_read(
            "tiny.txt",
            f"[lines {offset}-2 of 2 \u2014 offset past end of file]",
            offset=offset,
        )

    assert StuckDetector().evaluate(events).reason == "redundant_read_coverage"


def test_redundant_read_coverage_changed_total_recovers_header_only_streak():
    events = _header_only_read("growing.txt", "[lines 1-0 of 0]")
    for offset in (2, 3, 4, 5):
        events += _header_only_read("growing.txt", f"[lines {offset}-0 of 0]", offset=offset)
    events += _numbered_read("growing.txt", 1, 1, text_prefix="new")

    assert StuckDetector().evaluate(events).is_stuck is False


def _barren_read_events(*, error: str = "books.json: No such file or directory"):
    events = [user_msg("inspect the project")]
    for i in range(8):
        tool = "file_read" if i in {1, 4, 7} else "think"
        a = action(thought=f"read {i}", tool=tool, args={"path": "books.json"})
        events.append(a)
        if tool == "file_read":
            events.append(agent_error(error, action_id=a.id))
        else:
            events.append(observation(action_id=a.id, tool="think", content="noted"))
    return events


async def test_barren_streak_identical_read_errors_marks_no_progress():
    loop, store = build_loop(ScriptedAgent([]))
    for event in _barren_read_events():
        await store.append(CID, event)

    events = await store.get_events(CID)
    assert barren_streak_no_progress(events) is True

    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.CONTINUE
    after = await store.get_events(CID)
    assert any(isinstance(e, StatusEvent) and e.detail == "no_progress" for e in after)


def test_barren_streak_varying_successful_reads_never_fires():
    events = [user_msg("read around")]
    for i in range(8):
        a = action(thought=f"read {i}", tool="file_read", args={"path": f"{i}.txt"})
        events.extend(
            [
                a,
                observation(
                    action_id=a.id,
                    tool="file_read",
                    content=f"unique successful content {i}",
                ),
            ]
        )

    assert barren_streak_no_progress(events) is False


def test_barren_streak_mutating_tool_resets_window():
    events = _barren_read_events()
    mutating = action(
        thought="write once",
        tool="file_write",
        args={"path": "books.json", "content": "[]"},
    )
    # Keep exactly eight recent actions, with a mutating action inside the window.
    events = events[:1] + events[3:] + [mutating, observation(action_id=mutating.id)]

    assert barren_streak_no_progress(events) is False


def test_barren_streak_no_op_write_does_not_reset_window():
    events = [user_msg("finish the edit")]
    for i in range(5):
        a = action(thought=f"think {i}", tool="think", args={})
        events.extend([a, observation(action_id=a.id, tool="think", content="noted")])
    for i in range(3):
        a = action(
            thought=f"write {i}",
            tool="file_write",
            args={"path": "books.json", "content": "[]"},
        )
        events.extend([a, agent_error("no_op_write", action_id=a.id)])

    assert barren_streak_no_progress(events) is True


def test_barren_streak_fresh_read_required_does_not_reset_window():
    events = [user_msg("patch the file")]
    for i in range(5):
        a = action(thought=f"think {i}", tool="think", args={})
        events.extend([a, observation(action_id=a.id, tool="think", content="noted")])
    for i in range(3):
        a = action(
            thought=f"edit {i}",
            tool="file_edit",
            args={"path": "books.json", "old": "a", "new": "b"},
        )
        events.extend([a, agent_error("FRESH_READ_REQUIRED", action_id=a.id)])

    assert barren_streak_no_progress(events) is True


def test_barren_streak_successful_mutation_still_resets_window():
    events = [user_msg("finish the edit")]
    for i in range(7):
        a = action(thought=f"think {i}", tool="think", args={})
        events.extend([a, observation(action_id=a.id, tool="think", content="noted")])
    mutating = action(
        thought="write once",
        tool="file_write",
        args={"path": "books.json", "content": "[]"},
    )
    events.extend([mutating, observation(action_id=mutating.id, tool="file_write")])

    assert barren_streak_no_progress(events) is False


# ---- forwarded implementations (PY-0702 split) ------------------------------
#
# The recovery/escape/bookkeeping/F6 and failed-verifier/semantic-no-progress
# test implementations were moved into private companion modules:
#   loop_stuck_recovery_cases.py   (22 implementations)
#   loop_stuck_no_progress_cases.py (25 implementations)
# Each historical test name below is a thin functools.wraps forwarding wrapper
# through the companion module alias, so pytest collection, parametrize marks,
# and signatures are preserved byte-identically. The companions carry
# __test__ = False and contribute zero collected/static test IDs.


@functools.wraps(_recovery._impl_test_loop_tries_a_temp_escape_before_going_stuck)
async def test_loop_tries_a_temp_escape_before_going_stuck(*args, **kwargs):
    _impl = _recovery._impl_test_loop_tries_a_temp_escape_before_going_stuck
    return await _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_c7_escape_reminders_rotate_deterministically_by_attempt_count
)
async def test_c7_escape_reminders_rotate_deterministically_by_attempt_count(
    *args, **kwargs
):
    _impl = _recovery._impl_test_c7_escape_reminders_rotate_deterministically_by_attempt_count
    return await _impl(*args, **kwargs)


@functools.wraps(_recovery._impl_test_c7_non_escape_step_is_unchanged)
async def test_c7_non_escape_step_is_unchanged(*args, **kwargs):
    _impl = _recovery._impl_test_c7_non_escape_step_is_unchanged
    return await _impl(*args, **kwargs)


@functools.wraps(_recovery._impl_test_c7_stuck_escape_attempt_count_helper)
def test_c7_stuck_escape_attempt_count_helper(*args, **kwargs):
    _impl = _recovery._impl_test_c7_stuck_escape_attempt_count_helper
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_c7_pool_selector_is_deterministic_and_injective_across_attempts
)
def test_c7_pool_selector_is_deterministic_and_injective_across_attempts(
    *args, **kwargs
):
    _impl = _recovery._impl_test_c7_pool_selector_is_deterministic_and_injective_across_attempts
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_t2_escape_pool_carries_search_and_environment_guidance
)
def test_t2_escape_pool_carries_search_and_environment_guidance(*args, **kwargs):
    _impl = _recovery._impl_test_t2_escape_pool_carries_search_and_environment_guidance
    return _impl(*args, **kwargs)


@functools.wraps(_recovery._impl_test_stuck_result_names_the_breaker_per_pattern)
def test_stuck_result_names_the_breaker_per_pattern(*args, **kwargs):
    _impl = _recovery._impl_test_stuck_result_names_the_breaker_per_pattern
    return _impl(*args, **kwargs)


@functools.wraps(_recovery._impl_test_stuck_status_event_names_the_breaker)
async def test_stuck_status_event_names_the_breaker(*args, **kwargs):
    _impl = _recovery._impl_test_stuck_status_event_names_the_breaker
    return await _impl(*args, **kwargs)


@functools.wraps(_recovery._impl_test_loop_goes_stuck_then_resumes_on_new_message)
async def test_loop_goes_stuck_then_resumes_on_new_message(*args, **kwargs):
    _impl = _recovery._impl_test_loop_goes_stuck_then_resumes_on_new_message
    return await _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_bookkeeping_halt_caps_genuine_spam_on_tiny_plan
)
async def test_bookkeeping_halt_caps_genuine_spam_on_tiny_plan(*args, **kwargs):
    _impl = _recovery._impl_test_bookkeeping_halt_caps_genuine_spam_on_tiny_plan
    return await _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_bookkeeping_halt_does_not_trip_legit_burst_on_long_plan
)
async def test_bookkeeping_halt_does_not_trip_legit_burst_on_long_plan(
    *args, **kwargs
):
    _impl = _recovery._impl_test_bookkeeping_halt_does_not_trip_legit_burst_on_long_plan
    return await _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_fires_on_spiral_assist_on
)
def test_f6_per_file_rewrite_directive_fires_on_spiral_assist_on(*args, **kwargs):
    _impl = _recovery._impl_test_f6_per_file_rewrite_directive_fires_on_spiral_assist_on
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_does_not_fire_below_threshold_assist_on
)
def test_f6_per_file_rewrite_directive_does_not_fire_below_threshold_assist_on(
    *args, **kwargs
):
    _impl = (
        _recovery._impl_test_f6_per_file_rewrite_directive_does_not_fire_below_threshold_assist_on
    )
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_isolates_per_file_assist_on
)
def test_f6_per_file_rewrite_directive_isolates_per_file_assist_on(*args, **kwargs):
    _impl = _recovery._impl_test_f6_per_file_rewrite_directive_isolates_per_file_assist_on
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_off_is_byte_identical
)
def test_f6_per_file_rewrite_directive_off_is_byte_identical(*args, **kwargs):
    _impl = _recovery._impl_test_f6_per_file_rewrite_directive_off_is_byte_identical
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_successful_patches_dont_fire_assist_on
)
def test_f6_per_file_rewrite_directive_successful_patches_dont_fire_assist_on(
    *args, **kwargs
):
    _impl = (
        _recovery._impl_test_f6_per_file_rewrite_directive_successful_patches_dont_fire_assist_on
    )
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_ignores_non_mutating_tools_assist_on
)
def test_f6_per_file_rewrite_directive_ignores_non_mutating_tools_assist_on(
    *args, **kwargs
):
    _impl = _recovery._impl_test_f6_per_file_rewrite_directive_ignores_non_mutating_tools_assist_on
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_threshold_zero_disables_assist_on
)
def test_f6_per_file_rewrite_directive_threshold_zero_disables_assist_on(
    *args, **kwargs
):
    _impl = _recovery._impl_test_f6_per_file_rewrite_directive_threshold_zero_disables_assist_on
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_per_file_rewrite_directive_resets_on_user_message_assist_on
)
def test_f6_per_file_rewrite_directive_resets_on_user_message_assist_on(
    *args, **kwargs
):
    _impl = _recovery._impl_test_f6_per_file_rewrite_directive_resets_on_user_message_assist_on
    return _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_gate_stuck_emits_rewrite_directive_for_weak_spiral
)
async def test_f6_gate_stuck_emits_rewrite_directive_for_weak_spiral(*args, **kwargs):
    _impl = _recovery._impl_test_f6_gate_stuck_emits_rewrite_directive_for_weak_spiral
    return await _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_gate_stuck_dedupes_rewrite_directive_until_successful_edit
)
async def test_f6_gate_stuck_dedupes_rewrite_directive_until_successful_edit(
    *args, **kwargs
):
    _impl = _recovery._impl_test_f6_gate_stuck_dedupes_rewrite_directive_until_successful_edit
    return await _impl(*args, **kwargs)


@functools.wraps(
    _recovery._impl_test_f6_gate_stuck_standard_tier_suppresses_rewrite_directive
)
async def test_f6_gate_stuck_standard_tier_suppresses_rewrite_directive(
    *args, **kwargs
):
    _impl = _recovery._impl_test_f6_gate_stuck_standard_tier_suppresses_rewrite_directive
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_fingerprint_trips_across_varied_diagnostics
)
def test_failed_verifier_fingerprint_trips_across_varied_diagnostics(*args, **kwargs):
    _impl = _no_progress._impl_test_failed_verifier_fingerprint_trips_across_varied_diagnostics
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_fingerprint_resets_on_effective_mutation_change_or_pass
)
def test_failed_verifier_fingerprint_resets_on_effective_mutation_change_or_pass(
    *args, **kwargs
):
    _impl = (
        _no_progress._impl_test_failed_verifier_fingerprint_resets_on_effective_mutation_change_or_pass
    )
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_noop_or_receipt_empty_mutation_does_not_reset
)
def test_failed_verifier_noop_or_receipt_empty_mutation_does_not_reset(
    *args, **kwargs
):
    _impl = _no_progress._impl_test_failed_verifier_noop_or_receipt_empty_mutation_does_not_reset
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_concrete_mutation_receipt_resets
)
def test_failed_verifier_concrete_mutation_receipt_resets(*args, **kwargs):
    _impl = _no_progress._impl_test_failed_verifier_concrete_mutation_receipt_resets
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_gate_nudges_then_halts_after_varied_diagnostic
)
async def test_failed_verifier_gate_nudges_then_halts_after_varied_diagnostic(
    *args, **kwargs
):
    _impl = _no_progress._impl_test_failed_verifier_gate_nudges_then_halts_after_varied_diagnostic
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_cross_tool_r9_shape_retains_unresolved_web_streak
)
async def test_failed_verifier_cross_tool_r9_shape_retains_unresolved_web_streak(
    *args, **kwargs
):
    _impl = (
        _no_progress._impl_test_failed_verifier_cross_tool_r9_shape_retains_unresolved_web_streak
    )
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_old_marker_does_not_survive_new_mutation_streak
)
async def test_failed_verifier_old_marker_does_not_survive_new_mutation_streak(
    *args, **kwargs
):
    _impl = _no_progress._impl_test_failed_verifier_old_marker_does_not_survive_new_mutation_streak
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_marker_metadata_cannot_substitute_for_semantic_detail
)
async def test_failed_verifier_marker_metadata_cannot_substitute_for_semantic_detail(
    *args, **kwargs
):
    _impl = (
        _no_progress._impl_test_failed_verifier_marker_metadata_cannot_substitute_for_semantic_detail
    )
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_malformed_fingerprint_halts_evidence_invalid
)
async def test_failed_verifier_malformed_fingerprint_halts_evidence_invalid(
    *args, **kwargs
):
    _impl = _no_progress._impl_test_failed_verifier_malformed_fingerprint_halts_evidence_invalid
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_failed_verifier_malformed_verdict_schema_halts_evidence_invalid
)
async def test_failed_verifier_malformed_verdict_schema_halts_evidence_invalid(
    *args, **kwargs
):
    _impl = _no_progress._impl_test_failed_verifier_malformed_verdict_schema_halts_evidence_invalid
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_no_progress_trips_on_varied_edits_same_symptom
)
def test_no_progress_trips_on_varied_edits_same_symptom(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_trips_on_varied_edits_same_symptom
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_no_progress_does_not_trip_when_outcome_changes
)
def test_no_progress_does_not_trip_when_outcome_changes(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_does_not_trip_when_outcome_changes
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_no_progress_below_distinct_edit_threshold_does_not_trip
)
def test_no_progress_below_distinct_edit_threshold_does_not_trip(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_below_distinct_edit_threshold_does_not_trip
    return _impl(*args, **kwargs)


@functools.wraps(_no_progress._impl_test_no_progress_requires_two_probes)
def test_no_progress_requires_two_probes(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_requires_two_probes
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_no_progress_identical_edits_are_not_distinct
)
def test_no_progress_identical_edits_are_not_distinct(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_identical_edits_are_not_distinct
    return _impl(*args, **kwargs)


@functools.wraps(_no_progress._impl_test_no_progress_resets_on_user_message)
def test_no_progress_resets_on_user_message(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_resets_on_user_message
    return _impl(*args, **kwargs)


@functools.wraps(_no_progress._impl_test_no_progress_gate_nudges_then_halts)
async def test_no_progress_gate_nudges_then_halts(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_gate_nudges_then_halts
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_t2_no_progress_reminder_carries_search_and_environment_guidance
)
async def test_t2_no_progress_reminder_carries_search_and_environment_guidance(
    *args, **kwargs
):
    _impl = _no_progress._impl_test_t2_no_progress_reminder_carries_search_and_environment_guidance
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_t2_circuit_breaker_recovery_message_carries_search_and_environment_guidance
)
async def test_t2_circuit_breaker_recovery_message_carries_search_and_environment_guidance(
    *args, **kwargs
):
    _impl = (
        _no_progress._impl_test_t2_circuit_breaker_recovery_message_carries_search_and_environment_guidance
    )
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_no_progress_gate_finish_hints_when_latest_verify_passes_despite_stale_plan
)
async def test_no_progress_gate_finish_hints_when_latest_verify_passes_despite_stale_plan(
    *args, **kwargs
):
    _impl = (
        _no_progress._impl_test_no_progress_gate_finish_hints_when_latest_verify_passes_despite_stale_plan
    )
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_no_progress_gate_silent_on_genuine_progress
)
async def test_no_progress_gate_silent_on_genuine_progress(*args, **kwargs):
    _impl = _no_progress._impl_test_no_progress_gate_silent_on_genuine_progress
    return await _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_f6_per_file_rewrite_directive_failed_observation_also_counts_assist_on
)
def test_f6_per_file_rewrite_directive_failed_observation_also_counts_assist_on(
    *args, **kwargs
):
    _impl = (
        _no_progress._impl_test_f6_per_file_rewrite_directive_failed_observation_also_counts_assist_on
    )
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_f6_loop_wiring_weak_model_policy_enables_rewrite_directive
)
def test_f6_loop_wiring_weak_model_policy_enables_rewrite_directive(*args, **kwargs):
    _impl = _no_progress._impl_test_f6_loop_wiring_weak_model_policy_enables_rewrite_directive
    return _impl(*args, **kwargs)


@functools.wraps(
    _no_progress._impl_test_f6_loop_wiring_standard_model_policy_suppresses_rewrite_directive
)
def test_f6_loop_wiring_standard_model_policy_suppresses_rewrite_directive(
    *args, **kwargs
):
    _impl = (
        _no_progress._impl_test_f6_loop_wiring_standard_model_policy_suppresses_rewrite_directive
    )
    return _impl(*args, **kwargs)


@functools.wraps(_no_progress._impl_test_plan_done_and_verified_discriminator)
def test_plan_done_and_verified_discriminator(*args, **kwargs):
    _impl = _no_progress._impl_test_plan_done_and_verified_discriminator
    return _impl(*args, **kwargs)
