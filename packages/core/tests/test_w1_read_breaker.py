"""W1 — thought-excluded, wait-exempt read-aware breaker tests.

Verifies the four W1 behavioral guarantees:
  1. A thought-VARIED identical read loop fires (pattern 1 + _pure_repeat).
  2. A legit wait/poll server_status loop does NOT fire (exempt from patterns 1+4).
  3. An A-B-A-B alternating loop with varied thoughts fires (pattern 4).
  4. Editing files A, B, C in sequence does NOT fire (genuinely different args).
  5. event_content_eq(ignore_thought=True) ignores thought; the default compares it.

Does NOT test the disposition layer (turn_control.py gate_stuck) — that is
owned by another worker and already correct.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ToolCall,
    event_content_eq,
)
from disco.core.loop import StuckDetector, StuckThresholds
from disco.core.loop.stuck import _PLAN_META_TOOLS, _WAIT_POLL_TOOLS
from event_fakes import action, observation, user_msg

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_pair(path: str = "src/app.tsx", thought: str = "reading") -> list:
    """One file_read action + its observation."""
    a = action(thought=thought, tool="file_read", args={"path": path})
    o = observation(action_id=a.id, content="<file content>", tool="file_read")
    return [a, o]


def _poll_pair(thought: str = "checking status") -> list:
    """One server_status action + its observation (a legit poll)."""
    a = action(thought=thought, tool="server_status", args={})
    o = observation(action_id=a.id, content="HTTP 200", tool="server_status")
    return [a, o]


def _edit_pair(path: str, thought: str = "editing") -> list:
    """One file_edit action (distinct path) + its observation."""
    a = action(thought=thought, tool="file_edit", args={"path": path, "patch": f"fix-{path}"})
    o = observation(action_id=a.id, content="ok", tool="file_edit")
    return [a, o]


# ---------------------------------------------------------------------------
# 1. Thought-varied identical read loop FIRES
# ---------------------------------------------------------------------------


def test_thought_varied_read_loop_fires_via_pattern1():
    """Pattern 1 (repeat_action_observation): four file_read calls to the SAME
    path with DIFFERENT thoughts — before W1 the thought variation would defeat
    the detector; after W1 (ignore_thought=True) it fires at threshold=4."""
    d = StuckDetector()  # default threshold=4 after W1
    events = [user_msg("go")]
    for i in range(4):
        events += _read_pair(path="src/app.tsx", thought=f"still trying to read it, attempt {i}")
    assert d.is_stuck(events) is True, (
        "4 file_read('src/app.tsx') calls with varied thoughts MUST fire stuck "
        "(thought variation must not defeat pattern 1 after W1)"
    )


def test_thought_varied_read_loop_below_threshold_does_not_fire():
    """The generic detector remains below threshold at three cycles.

    Disable the separate exact-read breaker so this test continues to isolate
    W1's generic four-cycle contract.
    """
    d = StuckDetector(StuckThresholds(repeat_unchanged_file_read=0))
    events = [user_msg("go")]
    for i in range(3):
        events += _read_pair(thought=f"try {i}")
    assert d.is_stuck(events) is False, "3 file_read calls (below threshold=4) must NOT fire stuck"


def test_pure_repeat_fires_on_back_to_back_identical_actions():
    """_pure_repeat (W1 pattern 5): 4 back-to-back identical actions with NO
    paired observation in between — pattern 1 requires obs pairs; this catches
    the obs-free case."""
    d = StuckDetector()
    events = [user_msg("go")]
    for i in range(4):
        events.append(action(thought=f"loop {i}", tool="file_read", args={"path": "a.py"}))
    # No observations — patterns 1 and 2 stay quiet; _pure_repeat fires.
    assert d.is_stuck(events) is True, (
        "4 back-to-back identical file_read actions (no observations) must fire "
        "_pure_repeat after W1"
    )


# ---------------------------------------------------------------------------
# 2. Legit server_status poll loop does NOT fire
# ---------------------------------------------------------------------------


def test_wait_poll_server_status_loop_does_not_fire():
    """server_status is in _WAIT_POLL_TOOLS: patterns 1 and 4 are exempt from
    it. 6 identical poll cycles must NOT be flagged as stuck."""
    d = StuckDetector()
    events = [user_msg("wait for server")]
    for i in range(6):
        events += _poll_pair(thought=f"poll attempt {i}")
    assert d.is_stuck(events) is False, (
        "server_status poll loop (6 cycles) must NOT fire stuck — "
        "wait/poll tools are exempt from patterns 1 and 4"
    )


def test_wait_poll_tools_enumerated_in_sentinel():
    """Smoke-test that _WAIT_POLL_TOOLS contains the expected members."""
    expected = {
        "sleep",
        "wait",
        "server_status",
        "poll",
        "browser_wait",
        "job_status",
        "deploy_status",
    }
    assert expected <= _WAIT_POLL_TOOLS, (
        f"_WAIT_POLL_TOOLS is missing members: {expected - _WAIT_POLL_TOOLS}"
    )


def test_wait_poll_action_error_still_fires():
    """Pattern 2 (_repeated_action_error) does NOT exempt wait/poll tools —
    a perpetually-erroring poll IS stuck.  Three identical server_status → error
    pairs must fire."""
    from event_fakes import agent_error

    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = [user_msg("wait for server")]
    for _ in range(3):
        a = action(thought="poll", tool="server_status", args={})
        events.append(a)
        events.append(agent_error("connection refused", action_id=a.id))
    assert d.is_stuck(events) is True, (
        "server_status → error repeated 3× must fire pattern 2 (perpetually-erroring poll IS stuck)"
    )


# ---------------------------------------------------------------------------
# 3. A-B-A-B alternating loop with varied thoughts FIRES
# ---------------------------------------------------------------------------


def test_alternating_varied_thought_fires():
    """Pattern 4 (_alternating): two genuinely different tools (file_read and
    shell) alternate A-B-A-B-A-B with varied thoughts each time — before W1
    thought variation would defeat detection; after W1 it fires."""
    d = StuckDetector(StuckThresholds(alternating=3))
    events = [user_msg("go")]
    for i in range(3):
        events.append(action(thought=f"reading again {i}", tool="file_read", args={"path": "a.py"}))
        events.append(action(thought=f"running tests {i}", tool="shell", args={"cmd": "pytest"}))
    assert d.is_stuck(events) is True, (
        "A-B-A-B-A-B (file_read / shell) with varied thoughts MUST fire pattern 4 "
        "after W1 (ignore_thought=True)"
    )


def test_alternating_same_tool_same_args_different_thought_is_pure_repeat_not_alternating():
    """If A and B have the same tool/args (only different thoughts), they are
    semantically IDENTICAL with ignore_thought=True.  _alternating must return
    False (it's pattern 1 / _pure_repeat territory, not genuine alternation).
    The overall is_stuck can still be True via _pure_repeat."""
    d = StuckDetector(StuckThresholds(alternating=3))
    events = [user_msg("go")]
    for _ in range(3):
        events.append(action(thought="read A", tool="shell", args={}))
        events.append(action(thought="read B", tool="shell", args={}))
    # _alternating must NOT fire (A and B are identical ignoring thought).
    assert d._alternating(events) is False, (
        "_alternating must return False when A and B are identical "
        "ignoring thought (not genuine alternation)"
    )
    # But is_stuck may still be True (via _pure_repeat firing on 6 identical actions).
    assert d.is_stuck(events) is True, (
        "6 thought-varied but tool/args-identical actions must still fire "
        "is_stuck (via _pure_repeat)"
    )


# ---------------------------------------------------------------------------
# 4. Editing files A, B, C in sequence does NOT fire
# ---------------------------------------------------------------------------


def test_varied_file_edits_do_not_fire():
    """A sequence of distinct file edits (different paths) is NOT a loop — the
    stuck detector must stay quiet even though the tool_name is always file_edit."""
    d = StuckDetector()
    events = [user_msg("fix it")]
    events += _edit_pair("src/a.ts", thought="fixing a")
    events += _edit_pair("src/b.ts", thought="fixing b")
    events += _edit_pair("src/c.ts", thought="fixing c")
    assert d.is_stuck(events) is False, (
        "file_edit on src/a.ts, src/b.ts, src/c.ts (distinct args) must NOT fire stuck"
    )


def test_same_file_edit_repeated_fires():
    """Confirm the inverse: editing the SAME file with the SAME args 4× does
    fire pattern 1 (even with varied thoughts — W1 guarantee)."""
    d = StuckDetector()
    events = [user_msg("fix it")]
    for i in range(4):
        events += _edit_pair("src/broken.ts", thought=f"attempt {i}, maybe this diff works")
    assert d.is_stuck(events) is True, (
        "file_edit('src/broken.ts') × 4 with varied thoughts MUST fire stuck after W1"
    )


# ---------------------------------------------------------------------------
# 5. event_content_eq: ignore_thought vs default behaviour
# ---------------------------------------------------------------------------


def test_event_content_eq_default_compares_thought():
    """Without ignore_thought, two ActionEvents that differ only in thought are
    NOT equal — the default must preserve thought comparison."""
    a = ActionEvent(thought="approach A", tool_call=ToolCall(tool_name="shell", arguments={}))
    b = ActionEvent(thought="approach B", tool_call=ToolCall(tool_name="shell", arguments={}))
    assert not event_content_eq(a, b), (
        "default event_content_eq must NOT equal events with different thoughts"
    )


def test_event_content_eq_ignore_thought_equals_same_tool_args():
    """With ignore_thought=True, two ActionEvents with the same tool/args are
    equal regardless of thought differences."""
    a = ActionEvent(thought="approach A", tool_call=ToolCall(tool_name="shell", arguments={}))
    b = ActionEvent(thought="approach B", tool_call=ToolCall(tool_name="shell", arguments={}))
    assert event_content_eq(a, b, ignore_thought=True), (
        "event_content_eq(ignore_thought=True) must equal events with same tool/args "
        "even when thoughts differ"
    )


def test_event_content_eq_ignore_thought_still_distinguishes_different_args():
    """With ignore_thought=True, genuinely different args must remain NOT equal —
    ignore_thought must not collapse different paths/commands into one."""
    a = ActionEvent(
        thought="same thought",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": "a.py"}),
    )
    b = ActionEvent(
        thought="same thought",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": "b.py"}),
    )
    assert not event_content_eq(a, b, ignore_thought=True), (
        "event_content_eq(ignore_thought=True) must NOT equal events with different args "
        "(different paths must remain different)"
    )


def test_event_content_eq_ignore_thought_same_thought_still_equals():
    """When thoughts are actually the same, ignore_thought=True and False both
    return True — the flag must not BREAK equality."""
    a = ActionEvent(thought="identical", tool_call=ToolCall(tool_name="shell", arguments={}))
    b = ActionEvent(thought="identical", tool_call=ToolCall(tool_name="shell", arguments={}))
    assert event_content_eq(a, b) is True
    assert event_content_eq(a, b, ignore_thought=True) is True


def test_event_content_eq_ignore_thought_irrelevant_for_observations():
    """ignore_thought=True is irrelevant for ObservationEvents (they have no
    thought field) — must not raise or change the equality result."""
    from disco.core import ObservationEvent, ToolResult

    o1 = ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="out"),
        action_id="a1",
    )
    o2 = ObservationEvent(
        tool_result=ToolResult(call_id="c2", tool_name="shell", success=True, content="out"),
        action_id="a2",
    )
    # Both with and without the flag must return True (volatile ids are excluded).
    assert event_content_eq(o1, o2) is True
    assert event_content_eq(o1, o2, ignore_thought=True) is True


# ---------------------------------------------------------------------------
# 6. _PLAN_META_TOOLS exclusion — bookkeeping tools must not double-fire
# ---------------------------------------------------------------------------


def test_plan_meta_tools_enumerated_in_sentinel():
    """Smoke-test that _PLAN_META_TOOLS contains the expected members."""
    expected = {"submit_plan", "propose_plan_update", "plan_step", "finish"}
    assert expected <= _PLAN_META_TOOLS, (
        f"_PLAN_META_TOOLS is missing members: {expected - _PLAN_META_TOOLS}"
    )


def test_repeated_plan_step_does_not_fire_pattern1():
    """plan_step (a bookkeeping tool) repeated with same args must NOT fire
    patterns 1 or 4 — these tools are managed by gate_bookkeeping_streak and
    are exempt from stuck pattern double-firing."""
    d = StuckDetector()
    events = [user_msg("execute plan")]
    for i in range(6):
        events += [
            action(
                thought=f"mark step done {i}", tool="plan_step", args={"index": 1, "state": "done"}
            ),
            observation(content="ok"),
        ]
    # Pattern 1 must NOT fire on plan_step (it is in _PLAN_META_TOOLS).
    assert d._repeated_action_observation(events) is False, (
        "pattern 1 must not fire on plan_step (bookkeeping tool exempt)"
    )
    # Overall is_stuck might still be False (plan_step filtered from all non-error patterns).
    # No _pure_repeat either (plan_step is exempt).
    assert d.is_stuck(events) is False, (
        "repeated plan_step must not fire is_stuck "
        "(managed by dedicated bookkeeping halt, not stuck detector)"
    )


# ---------------------------------------------------------------------------
# 4. 2026-07-09 deck-run autopsy — the slideshow-paging false positive
# ---------------------------------------------------------------------------
#
# conv_b3d0be38 (Latest US-Iran Conflict Slides): the agent verified its own
# 9-slide deck by clicking the Next button — the SAME tool call each time
# (`browser click button:nth-child(2)`), but every observation showed the deck
# ADVANCING (2/9 → 3/9 → …). _pure_repeat collapsed the stream to actions-only,
# so the interleaved fresh observations were invisible and it hard-blocked the
# run at slide 6/9 as "pure_repeat". Interleaved repeats are pattern 1's
# jurisdiction (it requires the OBSERVATION to repeat too); _pure_repeat only
# owns back-to-back bursts with no observation in between.


def _slide_obs(slide: int, shot: int, page_text: str | None = None):
    """A browser observation shaped like the captured trace: page text with a
    slide counter + the volatile per-action screenshot path."""
    text = page_text if page_text is not None else f"US MILITARY ACTION\n{slide} / 9"
    return observation(
        tool="browser",
        content=(
            f"URL: http://localhost:3000/deck.html\nTEXT:\n{text}\n"
            f"screenshot: .pmx/screenshots/{shot:04d}-click.png"
        ),
    )


def _click():
    return action(
        thought="", tool="browser", args={"action": "click", "selector": "button:nth-child(2)"}
    )


def test_progressing_click_loop_is_not_stuck():
    """Identical 'click Next' actions whose observations CHANGE (the slide
    counter advances) are progress, not a loop — neither pattern 1 (obs differ)
    nor _pure_repeat (observations interleave every action) may fire."""
    d = StuckDetector()
    events = [user_msg("verify the deck")]
    for i in range(6):
        events += [_click(), _slide_obs(slide=2 + i, shot=3 + i)]
    assert d._pure_repeat(events) is False, (
        "_pure_repeat must not fire across interleaved observations "
        "(that is pattern 1's jurisdiction, which requires the obs to repeat)"
    )
    assert d.is_stuck(events) is False, (
        "paging through a slideshow (identical click, ADVANCING slide counter) "
        "is progress — the 2026-07-09 deck run must not be killed as stuck"
    )


def test_dead_click_loop_still_fires_despite_screenshot_counter():
    """The flip side: a genuinely DEAD button (page text identical every time,
    only the volatile screenshot counter changes) must still be stuck — pattern
    1 now normalizes the screenshot path, so it owns the case _pure_repeat
    previously (accidentally) caught."""
    d = StuckDetector()
    events = [user_msg("verify the deck")]
    for i in range(4):  # threshold=4
        events += [_click(), _slide_obs(slide=9, shot=10 + i, page_text="END 9 / 9")]
    assert d._repeated_action_observation(events) is True, (
        "identical page text with only the screenshot counter varying must "
        "compare EQUAL (volatile-content normalization) and fire pattern 1"
    )
    assert d.is_stuck(events) is True


def test_pure_repeat_run_broken_by_any_observation():
    """A burst of identical actions that eventually gets an observation is not
    a pure repeat — only the trailing UNANSWERED burst counts."""
    d = StuckDetector()
    events = [user_msg("go")]
    # 3 unanswered clicks, then an observation, then 3 more unanswered clicks:
    # neither burst reaches threshold=4, and they must NOT be merged across
    # the observation.
    events += [_click(), _click(), _click(), _slide_obs(slide=2, shot=1)]
    events += [_click(), _click(), _click()]
    assert d._pure_repeat(events) is False, (
        "an intervening observation must break the pure-repeat run "
        "(the model demonstrably waited and the world answered)"
    )


def test_page_text_screenshot_paths_are_not_normalized():
    """codex four-fix defect #3: normalization is END-anchored — the browser tool
    appends its screenshot line AFTER the content fence (always last), while page
    TEXT lives inside the fence. A page whose own copy shows a changing
    'screenshot: /assets/screenshots/frame-N.png' line is genuinely CHANGING
    content and must NOT be flattened into a false repeat."""
    d = StuckDetector()
    events = [user_msg("click through the gallery")]
    for i in range(4):
        page_text = f"Gallery\nscreenshot: /assets/screenshots/frame-{i:03d}.png\nnext"
        events += [
            _click(),
            observation(
                tool="browser",
                # page text INSIDE the fence (mid-content), volatile tool line at END
                content=(
                    f"TEXT:\n{page_text}\n[END]\n"
                    f"screenshot: .pmx/screenshots/{20 + i:04d}-click.png"
                ),
            ),
        ]
    assert d.is_stuck(events) is False, (
        "changing PAGE content mentioning screenshot paths must not be "
        "normalized into a false pattern-1 repeat (end-anchored volatiles only)"
    )
