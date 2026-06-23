"""W-45 — finish gate consumes the `verify_web_app` VERDICT (kills the verify-loop).

These tests drive the REAL AgentLoop end-to-end with a ScriptedAgent + a fake
executor that answers `verify_web_app` with a configurable structured verdict.
Assertions read the persisted event log (status transitions + env messages),
matching the existing test_finish_browser_gate.py harness.

Covered:
  (helpers) _latest_verify_verdict / _prior_verify_marker_fp pure readers
  (c) PASS verdict  → run FINISHES first try, refusals reset, no nudge
  (d/f) FAIL with CHANGING fingerprint across real edits → 3 next_action refusals
        then a NARROWED release that finishes only with an explicit INCOMPLETE
        summary (never a silent 'done')
  (e) FAIL with CONSTANT fingerprint + a navigate in between → STUCK/no_progress
      (the loop breaker); the navigate does NOT reset it
  (g) non-web deliverable → the verify gate is inert (never drives verify_web_app)
"""

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolResult,
)
from disco.core.events import EventSource
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import AgentLoop, NeverConfirm
from disco.core.loop.finish import _latest_verify_verdict, _prior_verify_marker_fp
from event_fakes import with_seqs
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)


def _verdict(
    *,
    passed: bool,
    fp: str,
    summary: str = "",
    next_action: str = "",
    verdict: str = "fail",
    console_errors=None,
    network_failures=None,
    screenshot="shot.png",
) -> dict:
    return {
        "passed": passed,
        "verdict": "pass" if passed else verdict,
        "url": "http://127.0.0.1:8000/",
        "http_status": 200,
        "title": "t",
        "meaningful_content": passed,
        "visible_text_chars": 50 if passed else 0,
        "elements_count": 3 if passed else 0,
        "console_errors": console_errors
        or ([] if passed else [{"text": "Boom", "source": "app.js:1", "stack": ""}]),
        "console_warnings": [],
        "network_failures": network_failures or [],
        "screenshot_path": screenshot,
        "vision": {"used": False, "passed": None, "notes": []},
        "failure_fingerprint": fp,
        "summary": summary or ("ok" if passed else "App threw a console error: Boom"),
        "next_action": next_action or ("" if passed else "Fix the console error: Boom"),
    }


class VerifyExecutor(FakeExecutor):
    """Answers `verify_web_app` with verdicts from a per-call list (repeats the
    last). `has_verify=False` removes the tool (degrade path). Browser/file_write
    get the stock ok result so navigate + edits succeed."""

    def __init__(self, verdicts, *, has_verify=True):
        tools = [
            ToolSpec(name="file_write", description="write", parameters_schema={}),
            ToolSpec(name="browser", description="browse", parameters_schema={}),
            ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
        ]
        if has_verify:
            tools.append(
                ToolSpec(name="verify_web_app", description="verify", parameters_schema={})
            )
        super().__init__(tools=tools)
        self._verdicts = list(verdicts)
        self.verify_calls = 0

    async def execute(self, call):
        if call.tool_name == "verify_web_app":
            i = self.verify_calls
            self.verify_calls += 1
            v = self._verdicts[min(i, len(self._verdicts) - 1)]
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="verify_web_app",
                success=True,
                content=f"VERIFY {v['verdict']} fp={v['failure_fingerprint']}",
                structured=v,
            )
        if call.tool_name == "browser":
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="browser",
                success=True,
                content="browsed",
                structured={"url": "http://127.0.0.1:8000/", "console": []},
            )
        return await super().execute(call)


def _gate_loop(agent, executor):
    store = SqliteEventStore(":memory:")
    loop = AgentLoop(
        "conv",
        store,
        agent,
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        planning_tools=frozenset({"submit_plan"}),
    )
    return loop, store


def _env(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


def _statuses(events):
    return [(e.status.value, e.detail) for e in events if isinstance(e, StatusEvent)]


# ---- pure readers -----------------------------------------------------------


def test_latest_verify_verdict_scoped_to_since_seq():
    def obs(fp, seq):
        res = ToolResult(
            call_id="c",
            tool_name="verify_web_app",
            success=True,
            content="v",
            structured={"failure_fingerprint": fp, "passed": False},
        )
        return ObservationEvent(tool_result=res, action_id="a").model_copy(update={"seq": seq})

    events = with_seqs([])  # empty
    assert _latest_verify_verdict([obs("X", 5)], 10) is None  # before since_seq
    v = _latest_verify_verdict([obs("X", 15)], 10)
    assert v is not None and v["failure_fingerprint"] == "X"
    # latest wins
    v = _latest_verify_verdict([obs("X", 15), obs("Y", 20)], 10)
    assert v["failure_fingerprint"] == "Y"
    assert _latest_verify_verdict(events, 0) is None


def test_prior_verify_marker_fp():
    def marker(detail, seq):
        return StatusEvent(status=ConversationStatus.RUNNING, detail=detail).model_copy(
            update={"seq": seq}
        )

    assert _prior_verify_marker_fp([], 0) is None
    evs = [marker("verify_no_progress:ABC", 12)]
    assert _prior_verify_marker_fp(evs, 5) == "ABC"
    # marker before since_seq is out of scope
    assert _prior_verify_marker_fp(evs, 20) is None
    # unrelated detail ignored
    assert _prior_verify_marker_fp([marker("resumed", 12)], 5) is None


# ---- (c) PASS finishes first try -------------------------------------------


@pytest.mark.asyncio
async def test_pass_verdict_finishes_and_resets_refusals():
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            finish_step(),
        ]
    )
    execu = VerifyExecutor([_verdict(passed=True, fp="CLEAN")])
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env(events)
    assert not any("did not pass" in m for m in env), env
    assert not any("INCOMPLETE" in m for m in env), env
    assert execu.verify_calls >= 1  # the gate drove verify for the overclaiming agent
    assert ("FINISHED", None) in _statuses(events)
    assert loop._browser_verify_refusals == 0


# ---- (d/f) varying fingerprint → refuse 3× then NARROWED release ------------


@pytest.mark.asyncio
async def test_fail_changing_fp_refuses_then_releases_with_incomplete_summary():
    # Each finish is preceded by a NEW productive edit, so since_seq advances and
    # the gate drives a FRESH verify whose fingerprint differs → the loop breaker
    # never trips → the 3-refusal cap-release path is exercised. The release must
    # finish only with an explicit INCOMPLETE summary (never a silent done).
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "v1"}),
            finish_step(),
            action_step(tool="file_write", args={"path": "index.html", "content": "v2"}),
            finish_step(),
            action_step(tool="file_write", args={"path": "index.html", "content": "v3"}),
            finish_step(),
            action_step(tool="file_write", args={"path": "index.html", "content": "v4"}),
            finish_step(),
        ]
    )
    verdicts = [
        _verdict(passed=False, fp=f"FP{i}", summary=f"broke {i}", next_action=f"fix {i}")
        for i in range(1, 6)
    ]
    execu = VerifyExecutor(verdicts)
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env(events)
    refusals = [m for m in env if "verify_web_app did not pass" in m]
    incompletes = [m for m in env if "INCOMPLETE" in m]
    assert len(refusals) == 3, f"expected 3 next_action refusals, got {len(refusals)}: {env}"
    assert any("next step:" in m for m in refusals), refusals
    assert len(incompletes) == 1, f"expected one explicit INCOMPLETE release: {env}"
    # narrowed release: it finished, but LOUDLY incomplete — not a silent pass
    assert ("FINISHED", None) in _statuses(events)


# ---- (e) constant fingerprint → STUCK/no_progress (loop breaker) ------------


@pytest.mark.asyncio
async def test_fail_same_fp_with_navigate_between_halts_stuck():
    # FAIL verdict with a CONSTANT fingerprint. After the first refusal the agent
    # only NAVIGATES (non-productive) and re-finishes: the cached verdict's same
    # fingerprint, with no productive edit, must HALT the run STUCK — not reload.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "broken"}),
            finish_step(),
            action_step(
                tool="browser", args={"action": "navigate", "url": "http://127.0.0.1:8000/"}
            ),
            finish_step(),
        ]
    )
    execu = VerifyExecutor(
        [_verdict(passed=False, fp="SAME", summary="still broken", next_action="fix it")]
    )
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    sts = _statuses(events)
    assert any(s == "STUCK" and (d or "").startswith("verify_no_progress:") for s, d in sts), sts
    assert not any(s == "FINISHED" for s, _ in sts), sts
    # the cached verdict was reused — verify was driven once, not re-run on the 2nd finish
    assert execu.verify_calls == 1, (
        f"expected ONE driven verify (cache hit), got {execu.verify_calls}"
    )
    env = _env(events)
    assert any("keeps returning the SAME failure" in m for m in env), env


# ---- (g) non-web deliverable: gate inert ------------------------------------


@pytest.mark.asyncio
async def test_non_web_deliverable_does_not_drive_verify():
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "main.py", "content": "print(1)"}),
            finish_step(),
        ]
    )
    execu = VerifyExecutor([_verdict(passed=False, fp="X")])
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("write a script")
    await loop.run()

    events = await store.get_events("conv")
    assert execu.verify_calls == 0, "verify must not run for a non-web deliverable"
    assert not any(
        isinstance(e, ActionEvent) and e.tool_call and e.tool_call.tool_name == "verify_web_app"
        for e in events
    )
    assert ("FINISHED", None) in _statuses(events)
