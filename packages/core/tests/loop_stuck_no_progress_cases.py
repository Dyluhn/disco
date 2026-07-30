"""Private companion for test_loop_stuck.py — failed-verifier/semantic-no-progress cases.

This module is a mechanical split target (PY-0702). It holds the 25
failed-verifier/semantic-no-progress test implementations moved out of
test_loop_stuck.py. Every historical test name is renamed to
``_impl_test_<historical-name>`` and collected by the parent module via a
thin ``functools.wraps`` forwarding wrapper.

``__test__ = False`` prevents pytest from collecting this module directly,
so the companion contributes zero collected/static test IDs.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import ModelExecutionPolicy
from disco.core.loop import StuckDetector, StuckThresholds
from disco.core.loop.control import Disp
from disco.core.loop.stuck import (
    VerifierEvidenceInvalid,
    VerifierFailureNoProgress,
    repeated_failed_verifier_no_progress,
    repeated_verify_no_progress,
)
from event_fakes import action, observation, user_msg
from loop_fakes import (
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
    finish_step,
)
from loop_stuck_recovery_cases import _failed_patch

__test__ = False

CID = "conv"


# ---- WALK-19 — semantic no-progress detector (failure-independent) ----------
#
# The black-screen-game: a capable model writes a DIFFERENT edit each turn (so
# patterns 1-4 never fire), each edit "succeeds" (writes apply, dev server 200 —
# so the failure-keyed circuit breaker never trips), and it grinds to
# max_iterations. The semantic signal: the same probe/verify OUTCOME recurs
# across many VARIED edits = no real progress.


def _edit(i: int):
    """A distinct (varied) file edit — patterns 1-4 stay quiet on these."""
    return action(thought=f"edit {i}", tool="file_edit", args={"path": "App.tsx", "patch": f"v{i}"})


def _probe(content: str = "HTTP 200, no console errors"):
    """A probe action (server_status) + its observation carrying the outcome."""
    a = action(thought="check the app", tool="server_status", args={})
    o = observation(action_id=a.id, content=content, tool="server_status")
    return [a, o]


def _verify_pass_probe():
    """A verify_web_app PASS probe that also participates in no-progress detection."""
    a = action(thought="verify the app", tool="verify_web_app", args={})
    o = ObservationEvent(
        tool_result=ToolResult(
            call_id="c",
            tool_name="verify_web_app",
            success=True,
            content="VERIFY_WEB_APP: PASS (app renders)",
            structured={"passed": True, "verdict": "pass"},
        ),
        action_id=a.id,
    )
    return [a, o]


def _structured_verifier_probe(
    *, fp: object = "SAME", passed: bool = False, tool: str = "verify_appkit_app"
):
    a = action(thought="verify the app", tool=tool, args={})
    o = ObservationEvent(
        tool_result=ToolResult(
            call_id="c",
            tool_name=tool,
            success=True,
            content="verifier executed",
            structured={
                "passed": passed,
                "verdict": "pass" if passed else "fail",
                "failure_fingerprint": fp,
                "summary": "all checks passed" if passed else "worker contract failed",
                "next_action": "" if passed else "repair the worker route",
            },
        ),
        action_id=a.id,
    )
    return [a, o]


def _mutation_observation(
    *, tool: str, structured: dict | None, success: bool = True
) -> list[Event]:
    a = action(thought="repair", tool=tool, args={})
    return [
        a,
        ObservationEvent(
            tool_result=ToolResult(
                call_id="c",
                tool_name=tool,
                success=success,
                content="mutation executed",
                structured=structured,
            ),
            action_id=a.id,
        ),
    ]


def _impl_test_failed_verifier_fingerprint_trips_across_varied_diagnostics():
    events = [user_msg("build the app")]
    events += _structured_verifier_probe(fp="SAME")
    events += _probe("server is running")
    read = action(thought="inspect", tool="file_read", args={"path": "src/App.tsx"})
    events += [read, observation(action_id=read.id, tool="file_read", content="source")]
    events += _structured_verifier_probe(fp="SAME")

    finding = repeated_failed_verifier_no_progress(events)
    assert finding is not None
    assert finding.tool_name == "verify_appkit_app"
    assert finding.failure_fingerprint == "SAME"
    assert finding.repeats == 2


def _impl_test_failed_verifier_fingerprint_resets_on_effective_mutation_change_or_pass():
    events = [user_msg("build the app")]
    events += _structured_verifier_probe(fp="SAME")
    events += _structured_verifier_probe(fp="SAME")
    assert repeated_failed_verifier_no_progress(events) is not None

    events += _mutation_observation(
        tool="app_update_content", structured={"files_written": ["src/content.ts"]}
    )
    events += _structured_verifier_probe(fp="SAME")
    assert repeated_failed_verifier_no_progress(events) is None

    events += _structured_verifier_probe(fp="BETTER")
    assert repeated_failed_verifier_no_progress(events) is None


@pytest.mark.parametrize(
    ("tool", "structured", "success"),
    [
        ("run_project_script", {"applied": []}, True),
        ("app_update_content", {"files_written": []}, True),
        ("safe_write_file", {"path": "src/App.tsx", "sha256": "a" * 64}, True),
        ("file_str_replace", {"path": "src/App.tsx", "sha256": "a" * 64}, True),
        ("file_write", None, True),
        ("file_write", {"path": "src/App.tsx", "sha256": "a" * 64}, False),
    ],
)
def _impl_test_failed_verifier_noop_or_receipt_empty_mutation_does_not_reset(
    tool: str, structured: dict | None, success: bool
):
    events = [user_msg("build the app")]
    events += _structured_verifier_probe(fp="SAME")
    events += _mutation_observation(tool=tool, structured=structured, success=success)
    events += _structured_verifier_probe(fp="SAME")
    assert isinstance(repeated_failed_verifier_no_progress(events), VerifierFailureNoProgress)


@pytest.mark.parametrize(
    ("tool", "structured"),
    [
        ("run_project_script", {"applied": ["src/App.tsx"]}),
        ("app_update_content", {"files_written": ["src/content.ts"]}),
        ("file_write", {"path": "src/App.tsx", "sha256": "a" * 64}),
        ("custom_mutator", {"state_changed": True}),
    ],
)
def _impl_test_failed_verifier_concrete_mutation_receipt_resets(tool: str, structured: dict):
    events = [user_msg("build the app")]
    events += _structured_verifier_probe(fp="SAME")
    events += _mutation_observation(tool=tool, structured=structured)
    events += _structured_verifier_probe(fp="SAME")
    assert repeated_failed_verifier_no_progress(events) is None
    events += _structured_verifier_probe(fp="BETTER", passed=True)
    events += _structured_verifier_probe(fp="BETTER")
    assert repeated_failed_verifier_no_progress(events) is None


async def _impl_test_failed_verifier_gate_nudges_then_halts_after_varied_diagnostic():
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, autonomous=True)
    await store.append(CID, user_msg("build the app"))
    for event in _structured_verifier_probe(fp="SAME"):
        await store.append(CID, event)
    for event in _probe("server is running"):
        await store.append(CID, event)
    for event in _structured_verifier_probe(fp="SAME"):
        await store.append(CID, event)

    disp = await loop._valve.gate_no_progress(await store.get_events(CID))
    assert disp is Disp.CONTINUE
    nudged = await store.get_events(CID)
    assert any(
        isinstance(event, StatusEvent)
        and (event.detail or "").startswith("verifier_no_progress:verify_appkit_app:")
        for event in nudged
    )
    reminder = next(
        event
        for event in nudged
        if isinstance(event, MessageEvent)
        and event.source == EventSource.ENVIRONMENT
        and "same failed verification fingerprint" in (event.message.content or "").lower()
    )
    assert "do not call `finish`" in (reminder.message.content or "").lower()
    assert "repair the worker route" in (reminder.message.content or "").lower()

    read = action(thought="inspect another file", tool="file_read", args={"path": "worker.ts"})
    await store.append(CID, read)
    await store.append(CID, observation(action_id=read.id, tool="file_read", content="source"))
    disp = await loop._valve.gate_no_progress(await store.get_events(CID))
    assert disp is Disp.HALT
    events = await store.get_events(CID)
    assert_blocked_question_landing(
        events,
        legacy_detail="verifier_no_progress",
        flavor="terminal",
    )
    assert not any(
        isinstance(event, StatusEvent) and event.status == ConversationStatus.FINISHED
        for event in events
    )
    assert agent.calls == 0, "host-owned convergence terminal must not spend another model call"


async def _impl_test_failed_verifier_cross_tool_r9_shape_retains_unresolved_web_streak():
    """Sanitized RUN-548: another verifier diagnostic cannot hide web failure A."""

    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, autonomous=True)
    await store.append(CID, user_msg("build the app"))
    for event in _structured_verifier_probe(fp="WEB-A", tool="verify_web_app"):
        await store.append(CID, event)
    for event in _mutation_observation(
        tool="file_write",
        structured={"path": "index.html", "sha256": "b" * 64},
    ):
        await store.append(CID, event)
    for _ in range(2):
        for event in _structured_verifier_probe(fp="WEB-A", tool="verify_web_app"):
            await store.append(CID, event)

    assert await loop._valve.gate_no_progress(await store.get_events(CID)) is Disp.CONTINUE

    # The one bounded diagnostic is a different verifier and a different
    # fingerprint. It is not evidence that the unresolved web verdict improved.
    for event in _structured_verifier_probe(fp="APPKIT-B", tool="verify_appkit_app"):
        await store.append(CID, event)
    assert await loop._valve.gate_no_progress(await store.get_events(CID)) is Disp.HALT

    events = await store.get_events(CID)
    assert any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.STUCK
        and event.detail == "verifier_no_progress"
        for event in events
    )
    assert not any(
        isinstance(event, StatusEvent) and event.status == ConversationStatus.FINISHED
        for event in events
    )
    assert agent.calls == 0


async def _impl_test_failed_verifier_old_marker_does_not_survive_new_mutation_streak():
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, autonomous=True)
    await store.append(CID, user_msg("build the app"))
    for _ in range(2):
        for event in _structured_verifier_probe(fp="SAME", tool="verify_web_app"):
            await store.append(CID, event)
    assert await loop._valve.gate_no_progress(await store.get_events(CID)) is Disp.CONTINUE

    for event in _mutation_observation(
        tool="file_write",
        structured={"path": "index.html", "sha256": "c" * 64},
    ):
        await store.append(CID, event)
    for _ in range(2):
        for event in _structured_verifier_probe(fp="SAME", tool="verify_web_app"):
            await store.append(CID, event)

    assert await loop._valve.gate_no_progress(await store.get_events(CID)) is Disp.CONTINUE
    markers = [
        event
        for event in await store.get_events(CID)
        if isinstance(event, StatusEvent)
        and (event.detail or "").startswith("verifier_no_progress:verify_web_app:")
    ]
    assert len(markers) == 2
    assert agent.calls == 0


async def _impl_test_failed_verifier_marker_metadata_cannot_substitute_for_semantic_detail():
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, autonomous=True)
    await store.append(CID, user_msg("build the app"))
    for _ in range(2):
        for event in _structured_verifier_probe(fp="SAME", tool="verify_web_app"):
            await store.append(CID, event)
    await store.append(
        CID,
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="verifier_no_progress",
            meta={
                "verifier_tool": "verify_web_app",
                "failure_fingerprint": "SAME",
                "streak_start_seq": 1,
            },
        ),
    )

    assert await loop._valve.gate_no_progress(await store.get_events(CID)) is Disp.CONTINUE
    assert any(
        isinstance(event, StatusEvent)
        and (event.detail or "").startswith("verifier_no_progress:verify_web_app:")
        for event in await store.get_events(CID)
    )
    assert agent.calls == 0


@pytest.mark.parametrize("fingerprint", [None, "", 7])
async def _impl_test_failed_verifier_malformed_fingerprint_halts_evidence_invalid(
    fingerprint: object,
):
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, autonomous=True)
    await store.append(CID, user_msg("build the app"))
    for event in _structured_verifier_probe(fp=fingerprint):
        await store.append(CID, event)

    finding = repeated_failed_verifier_no_progress(await store.get_events(CID))
    assert isinstance(finding, VerifierEvidenceInvalid)
    assert await loop._valve.gate_no_progress(await store.get_events(CID)) is Disp.HALT
    assert any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.STUCK
        and event.detail == "verifier_evidence_invalid"
        for event in await store.get_events(CID)
    )
    assert agent.calls == 0


@pytest.mark.parametrize("structured", [None, {}, {"passed": "false"}])
async def _impl_test_failed_verifier_malformed_verdict_schema_halts_evidence_invalid(
    structured: dict | None,
):
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent, autonomous=True)
    await store.append(CID, user_msg("build the app"))
    verify = action(thought="verify", tool="verify_appkit_app", args={})
    await store.append(CID, verify)
    await store.append(
        CID,
        ObservationEvent(
            tool_result=ToolResult(
                call_id="c",
                tool_name="verify_appkit_app",
                success=True,
                content="verifier executed",
                structured=structured,
            ),
            action_id=verify.id,
        ),
    )

    assert await loop._valve.gate_no_progress(await store.get_events(CID)) is Disp.HALT
    assert any(
        isinstance(event, StatusEvent)
        and event.status == ConversationStatus.STUCK
        and event.detail == "verifier_evidence_invalid"
        for event in await store.get_events(CID)
    )
    assert agent.calls == 0


def _impl_test_no_progress_trips_on_varied_edits_same_symptom():
    """4 DISTINCT edits, each followed by the SAME probe outcome ⇒ trip."""
    events = [user_msg("build the app")]
    for i in range(4):
        events.append(_edit(i))
        events += _probe()  # identical outcome every time
    assert repeated_verify_no_progress(events) is True


def _impl_test_no_progress_does_not_trip_when_outcome_changes():
    """Genuine progress: the final edit CHANGES the probe outcome ⇒ no trip
    (the trailing constant-outcome run is just the new, single observation)."""
    events = [user_msg("build the app")]
    for i in range(3):
        events.append(_edit(i))
        events += _probe("HTTP 200, blank page")
    events.append(_edit(3))
    events += _probe("HTTP 200, heading now visible")  # outcome finally changed
    assert repeated_verify_no_progress(events) is False


def _impl_test_no_progress_below_distinct_edit_threshold_does_not_trip():
    """Only 3 distinct edits against a stable outcome (< the default 4) ⇒ no trip."""
    events = [user_msg("go")]
    for i in range(3):
        events.append(_edit(i))
        events += _probe()
    assert repeated_verify_no_progress(events) is False


def _impl_test_no_progress_requires_two_probes():
    """Many varied edits but only ONE probe observation ⇒ no trip (a single
    outcome is not a RECURRING symptom)."""
    events = [user_msg("go"), _edit(0), _edit(1), _edit(2), _edit(3)]
    events += _probe()
    assert repeated_verify_no_progress(events) is False


def _impl_test_no_progress_identical_edits_are_not_distinct():
    """Byte-identical edits are pattern-1's job, NOT this detector's — they must
    NOT be double-counted as distinct varied edits, so an identical-edit loop
    against a stable outcome does NOT trip here."""
    events = [user_msg("go")]
    for _ in range(4):
        events.append(action(thought="same", tool="file_edit", args={"path": "a", "patch": "x"}))
        events += _probe()
    assert repeated_verify_no_progress(events) is False


def _impl_test_no_progress_resets_on_user_message():
    """A new USER instruction is a new goal — the pre-message symptom does not
    count toward the post-message window."""
    events = [user_msg("go")]
    for i in range(4):
        events.append(_edit(i))
        events += _probe()
    events.append(user_msg("new direction"))
    events.append(_edit(99))
    events += _probe()
    assert repeated_verify_no_progress(events) is False


async def _impl_test_no_progress_gate_nudges_then_halts():
    """Loop integration via the real Valve gate + store: first trip emits a
    corrective nudge (CONTINUE); a second trip after the model made MORE varied
    edits with the SAME symptom explains and asks (instead of grinding to the ceiling)."""
    loop, store = build_loop(ScriptedAgent([finish_step()]))
    await store.append(CID, user_msg("build the app"))
    for i in range(4):
        await store.append(CID, _edit(i))
        for e in _probe():
            await store.append(CID, e)

    events = await store.get_events(CID)
    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.CONTINUE  # first trip → nudge, not halt
    events = await store.get_events(CID)
    assert any(isinstance(e, StatusEvent) and e.detail == "no_progress" for e in events), (
        "first trip must drop a no_progress marker"
    )
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "outcome has NOT changed" in (e.message.content or "")
        for e in events
    ), "first trip must inject the corrective reminder"

    # The model acts again (more varied edits) but the symptom is unchanged.
    await store.append(CID, _edit(5))
    for e in _probe():
        await store.append(CID, e)
    events = await store.get_events(CID)
    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.HALT
    events = await store.get_events(CID)
    assert_blocked_question_landing(events, legacy_detail="no_progress")


async def _impl_test_t2_no_progress_reminder_carries_search_and_environment_guidance():
    """T2 — the no-progress corrective reminder now also points the model at
    search-to-escape (look the recurring error up) + environment-vs-your-code.
    Reuses the gate_no_progress nudge setup; the existing 'outcome has NOT
    changed' reframe stays intact (asserted here too, so the append didn't
    rewrite it)."""
    loop, store = build_loop(ScriptedAgent([finish_step()]))
    await store.append(CID, user_msg("build the app"))
    for i in range(4):
        await store.append(CID, _edit(i))
        for e in _probe():
            await store.append(CID, e)

    events = await store.get_events(CID)
    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.CONTINUE  # first trip → nudge (unchanged behavior)
    events = await store.get_events(CID)
    reminder = next(
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "outcome has NOT changed" in (e.message.content or "")
    )
    content = (reminder.message.content or "").lower()
    # existing reframe preserved…
    assert "outcome has not changed" in content
    # …plus the two new ideas.
    assert "search" in content
    assert "environment" in content


async def _impl_test_t2_circuit_breaker_recovery_message_carries_search_and_environment_guidance():
    """T2 — the circuit-breaker recovery nudge (already 'try a DIFFERENT
    technical path') now also mentions searching the recurring error and the
    environment-vs-your-code distinction. Drives the real breaker (non-autonomous
    build_loop) with distinct failing actions and inspects the emitted recovery
    reminder in the log; detection/halt behavior is unchanged."""
    from disco.core import ToolResult
    from loop_fakes import FakeExecutor

    failing = ToolResult(call_id="c", tool_name="shell", success=False, content="", error="boom")
    agent = ScriptedAgent(
        [
            action_step(args={"cmd": "a"}),
            action_step(args={"cmd": "b"}),
            action_step(args={"cmd": "c"}),
            action_step(args={"cmd": "d"}),
            action_step(args={"cmd": "e"}),
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    recovery = next(
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "times in a row" in (e.message.content or "")
    )
    content = (recovery.message.content or "").lower()
    # existing recovery framing preserved…
    assert "times in a row" in content
    # …plus the two new ideas.
    assert "search" in content
    assert "environment" in content


async def _impl_test_no_progress_gate_finish_hints_when_latest_verify_passes_despite_stale_plan():
    """#37d live regression: a stable PASS verifier outcome means DONE, not a
    blocked user question, even when effective plan marks are stale/incomplete."""
    from disco.core.events import PlanEvent, PlanStep

    loop, store = build_loop(ScriptedAgent([finish_step()]))
    await store.append(CID, user_msg("build the app"))
    await store.append(
        CID,
        PlanEvent(
            source=EventSource.AGENT,
            summary="build",
            steps=[PlanStep(title="make app"), PlanStep(title="verify app")],
        ),
    )
    for i in range(4):
        await store.append(CID, _edit(i))
        for e in _verify_pass_probe():
            await store.append(CID, e)

    disp = await loop._valve.gate_no_progress(await store.get_events(CID))
    assert disp is Disp.CONTINUE

    await store.append(CID, _edit(5))
    for e in _verify_pass_probe():
        await store.append(CID, e)

    disp = await loop._valve.gate_no_progress(await store.get_events(CID))
    assert disp is Disp.CONTINUE
    events = await store.get_events(CID)
    assert any(isinstance(e, StatusEvent) and e.detail == "no_progress_finish_hint" for e in events)
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.AWAITING_USER_QUESTION
        and e.meta.get("legacy_detail") == "no_progress"
        for e in events
    )


async def _impl_test_no_progress_gate_silent_on_genuine_progress():
    """The gate must NOT fire when the outcome is changing (real progress)."""
    loop, store = build_loop(ScriptedAgent([finish_step()]))
    await store.append(CID, user_msg("build it"))
    for i in range(4):
        await store.append(CID, _edit(i))
        for e in _probe(f"render #{i}"):  # outcome changes every edit
            await store.append(CID, e)
    events = await store.get_events(CID)
    disp = await loop._valve.gate_no_progress(events)
    assert disp is Disp.FALLTHROUGH
    assert not any(
        isinstance(e, StatusEvent) and e.detail == "no_progress"
        for e in await store.get_events(CID)
    )


def _impl_test_f6_per_file_rewrite_directive_failed_observation_also_counts_assist_on():
    """F6 — a non-success ObservationEvent on a file-mutating tool ALSO counts
    as a failure (the tool returned success=False). The detector covers both
    shapes: a tool that raised (paired AgentErrorEvent) AND a tool that
    returned a failure result (paired ObservationEvent with success=False)."""
    d = StuckDetector(
        StuckThresholds(per_file_rewrite_failures=3, per_file_rewrite_min_attempts=3),
        assist=True,
    )
    out = []
    for i in range(3):
        a = action(
            thought="write " + str(i),
            tool="file_write",
            args={"path": "a/x.py", "content": "v" + str(i)},
        )
        out.append(a)
        out.append(observation(action_id=a.id, content="ERROR: write refused", success=False))
    result = d.evaluate(out)
    assert result.rewrite_directive is not None
    assert result.rewrite_directive.path == "a/x.py"
    assert result.rewrite_directive.failures == 3
    assert result.rewrite_directive.attempts == 3


# ---- Order A wiring test: weak model_policy threads assist=True into StuckDetector ----
#
# Previously `AgentLoop.__init__` always constructed `StuckDetector(thresholds)`
# WITHOUT passing `assist`, so the detector defaulted to assist=False even when
# the loop's `_assist` flag was later flipped to True. The result: the F6
# patch-spiral directive was BROKEN-CLOSED — it never fired from a real loop run.
#
# After Order A the constructor calls `StuckDetector(thresholds, assist=model_policy.assist)`,
# so a weak `model_policy` (assist=True) propagates into the detector's _assist gate.
# This test verifies that end-to-end wiring: the loop's stuck detector must fire
# the F6 directive when the loop was built with a weak model_policy.


def _impl_test_f6_loop_wiring_weak_model_policy_enables_rewrite_directive():
    """Order A acceptance: a weak model_policy passed to AgentLoop must make the
    loop's StuckDetector emit the F6 rewrite_directive for a patch-spiraling file.

    This was BROKEN before Order A because AgentLoop always passed assist=False
    (the default) to StuckDetector regardless of its own _assist value. The fix:
    `StuckDetector(stuck_thresholds, assist=model_policy.assist)`.
    """
    from disco.core import NoOpCondenser, SqliteEventStore
    from disco.core.llm import OperatingMode
    from disco.core.loop import NeverConfirm
    from disco.core.loop.engine import AgentLoop
    from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer

    _TIGHT = StuckThresholds(per_file_rewrite_failures=2, per_file_rewrite_min_attempts=2)

    loop = AgentLoop(
        "conv",
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),  # never stepped — we inspect _stuck directly
        FakeExecutor(),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=ModelExecutionPolicy(tier="weak"),
        stuck_thresholds=_TIGHT,
    )

    # Build a spiral: 2 failed patches on the same file (threshold = 2,2).
    events = _failed_patch("src/app.py", 2)
    result = loop._stuck.evaluate(events)

    assert result.rewrite_directive is not None, (
        "StuckDetector inside AgentLoop(model_policy=ModelExecutionPolicy(tier='weak')) "
        "must fire the F6 rewrite_directive — the loop wiring was broken before Order A "
        "(assist defaulted to False regardless of _assist). "
        f"events={[type(e).__name__ for e in events]}"
    )
    assert result.rewrite_directive.path == "src/app.py"


def _impl_test_f6_loop_wiring_standard_model_policy_suppresses_rewrite_directive():
    """Order A acceptance (inverse): a standard model_policy must NOT make the
    StuckDetector emit an F6 directive — the gate must stay closed for capable models."""
    from disco.core import NoOpCondenser, SqliteEventStore
    from disco.core.llm import OperatingMode
    from disco.core.loop import NeverConfirm
    from disco.core.loop.engine import AgentLoop
    from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer

    _TIGHT = StuckThresholds(per_file_rewrite_failures=2, per_file_rewrite_min_attempts=2)

    loop = AgentLoop(
        "conv",
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),
        FakeExecutor(),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=ModelExecutionPolicy.standard(),
        stuck_thresholds=_TIGHT,
    )

    events = _failed_patch("src/app.py", 2)
    result = loop._stuck.evaluate(events)

    assert result.rewrite_directive is None, (
        "StuckDetector inside AgentLoop(model_policy=ModelExecutionPolicy.standard()) "
        "must NOT fire the F6 directive — the gate is closed for capable models. "
        f"Got: {result.rewrite_directive!r}"
    )


def _impl_test_plan_done_and_verified_discriminator():
    """dt3 autopsy: unchanged verify outcome is DONE, not stuck, when every plan
    step is done and the last verify PASSED — the gate must hint finish instead
    of halting STUCK."""
    from disco.core.events import (
        EventSource,
        ObservationEvent,
        PlanEvent,
        PlanStep,
        ToolResult,
    )
    from disco.core.loop.turn_control import (
        _last_verify_web_app_passed,
        _plan_done_and_verified,
    )

    plan = PlanEvent(
        source=EventSource.AGENT,
        summary="s",
        steps=[PlanStep(title="a"), PlanStep(title="b")],
    )

    def obs(content: str) -> ObservationEvent:
        return ObservationEvent(
            source=EventSource.ENVIRONMENT,
            action_id="a1",
            tool_result=ToolResult(
                call_id="c1", tool_name="verify_web_app", success=True, content=content
            ),
        )

    from disco.core.events import ActionEvent, ToolCall

    done = [
        ActionEvent(
            source=EventSource.AGENT,
            thought="",
            tool_call=ToolCall(
                tool_name="update_plan_progress",
                arguments={
                    "steps": [
                        {"index": 1, "state": "done"},
                        {"index": 2, "state": "done"},
                    ]
                },
                call_id="p1",
            ),
        )
    ]
    # all steps done + PASS → True
    assert _plan_done_and_verified([plan, *done, obs("VERIFY_WEB_APP: PASS (pass)")])
    # FAIL verify → False
    assert not _plan_done_and_verified([plan, *done, obs("VERIFY_WEB_APP: FAIL (broken)")])
    # steps not done → False
    assert not _plan_done_and_verified([plan, obs("VERIFY_WEB_APP: PASS (pass)")])
    # latest verify PASS is independent of stale plan bookkeeping
    assert _last_verify_web_app_passed([plan, obs("VERIFY_WEB_APP: PASS (pass)")])