"""Bug 6 — honest unverifiable-static finish at the ACTIONLESS valve.

The dominant bare-Build reliability bug surfaced by the Build Soak: a
substantively-complete static build (deliverable written, non-verify plan steps
done) PAUSED "actionless" instead of FINISHING when the plan's final verify step
was blocked by browser-unavailable on the process backend. The model never
reaches the finish gate (it churns on the unsatisfiable browser-verify step), so
the existing finish-gate honest-unverifiable path (finish.py
`_maybe_honest_unverifiable_static_finish`) never fires — the actionless valve
preempts it.

The fix extends that SAME honest-finish concept to the actionless valve
(`Valve.actionless_valve` → `FinishGate.maybe_honest_unverifiable_static_actionless_finish`).
These tests drive the REAL AgentLoop end-to-end via loop_fakes and assert the
reproduction is closed (positive) and the must-not-regress guards hold
(negatives): a missing deliverable, a FAILED non-browser validation, and a
zero-work run never honest-finish.
"""

import pytest
from disco.core import (
    ConversationStatus,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.tools.builtin.browser import BROWSER_UNAVAILABLE_MSG
from loop_fakes import (
    FakeExecutor,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

CID = "conv"


def _statuses(events):
    return [(e.status, e.detail) for e in events if isinstance(e, StatusEvent)]


def _has_honest_marker(events) -> bool:
    return any(
        isinstance(e, StatusEvent) and e.detail == "unverifiable_static_finish"
        for e in events
    )


def _notify(msg):
    return action_step("notify_user", {"message": msg})


class _StaticSandbox:
    """Process-backend-shaped sandbox: `file_exists` reports the static deliverable
    (decoupled from the fake file_write, exactly like the finish-gate tests)."""

    generation = 0

    def __init__(self, *, index_exists=True):
        self._index_exists = index_exists

    async def file_exists(self, path):
        return self._index_exists and path in ("index.html", "./index.html")


class _BrowserlessStaticExecutor(FakeExecutor):
    """A backend that advertises `browser` (so the model CAN call it — forcing the
    'observed-unavailable' detection path rather than the no-tool path) but whose
    browser is UNAVAILABLE: it returns the typed browser-unavailable outcome the
    real process backend produces. `shell` answers a static index.html validation
    with a configurable pass/fail."""

    def __init__(self, *, index_exists=True, validation_ok=True):
        tools = [
            ToolSpec(name=n, description=n, parameters_schema={})
            for n in ("file_write", "shell", "browser")
        ]
        super().__init__(tools=tools)
        self._sandbox = _StaticSandbox(index_exists=index_exists)
        self._validation_ok = validation_ok

    @property
    def sandbox(self):
        return self._sandbox

    async def execute(self, call):
        self.calls.append(call)
        if call.tool_name == "browser":
            # Mirrors browser.py's BrowserUnavailableError outcome (success=False,
            # error=BROWSER_UNAVAILABLE_MSG, structured browser_unavailable flag).
            return ToolResult(
                call_id=call.call_id,
                tool_name="browser",
                success=False,
                content="",
                structured={"browser_unavailable": True},
                error=BROWSER_UNAVAILABLE_MSG,
            )
        if call.tool_name == "shell":
            cmd = str((call.arguments or {}).get("command", ""))
            if "index.html" in cmd:
                return ToolResult(
                    call_id=call.call_id,
                    tool_name="shell",
                    success=self._validation_ok,
                    content="index.html parsed cleanly" if self._validation_ok else "",
                    error=None if self._validation_ok else "HTMLParseError: malformed tag",
                )
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


_PLAN = {
    "summary": "bakery landing page",
    "steps": [
        {"title": "Build the HTML structure in index.html"},
        {"title": "Add the CSS styling in style.css"},
        {"title": "Verify the page renders correctly in the browser"},
    ],
}


def _deliver_then_idle_steps():
    """Write both deliverables, mark the two build steps done + the verify step
    active, attempt browser-verify (unavailable) + a shell HTMLParser validation,
    then go idle (notify spam) — the EXACT Bug 6 reproduction shape."""
    html = "<html><body><h1>Bakery</h1></body></html>"
    validate = (
        "python3 -c \"from html.parser import HTMLParser as P; "
        "P().feed(open('index.html').read())\""
    )
    return [
        action_step("submit_plan", _PLAN),
        action_step("file_write", {"path": "index.html", "content": html}),
        action_step("file_write", {"path": "style.css", "content": "h1{color:#a30}"}),
        action_step("plan_step", {"index": 1, "state": "done"}),
        action_step("plan_step", {"index": 2, "state": "done"}),
        action_step("plan_step", {"index": 3, "state": "active"}),
        action_step("browser", {"action": "navigate", "url": "http://127.0.0.1:8000/"}),
        action_step("shell", {"command": validate}),
        _notify("Files delivered; browser verification isn't available on this backend."),
        _notify("The static deliverable is in place; I validated the HTML parses."),
        _notify("Done — the page is built; render couldn't be browser-checked here."),
        _notify("Standing by."),
        finish_step(),
    ]


async def _approve_and_run(executor, agent):
    loop, store = build_loop(
        executor=executor, agent=agent, planning_tools=frozenset(["file_read"])
    )
    loop.mode = OperatingMode.PLANNING
    await loop.send_message("Create a simple landing page for a local bakery")
    await loop.run()  # consumes submit_plan, halts at AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    state = await loop.run()  # executes
    events = await store.get_events(CID)
    return state, events


@pytest.mark.asyncio
async def test_positive_complete_static_build_honest_finishes_not_paused():
    """POSITIVE — Bug 6 closed. Deliverables written, only the verify step
    remains, browser is unavailable, a non-browser validation PASSED → the loop
    FINISHES with the honest `unverifiable_static_finish` marker, NOT
    PAUSED/actionless and NOT STUCK."""
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    state, events = await _approve_and_run(execu, ScriptedAgent(_deliver_then_idle_steps()))

    sts = _statuses(events)
    assert _has_honest_marker(events), sts
    assert state.execution_status == ConversationStatus.FINISHED, sts
    assert (ConversationStatus.FINISHED, None) in sts, sts
    assert not any(
        s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts
    ), sts
    assert not any(s == ConversationStatus.STUCK for s, _ in sts), sts


@pytest.mark.asyncio
async def test_negative_no_deliverable_does_not_honest_finish():
    """NEGATIVE (no deliverable) — same idle pattern but index.html does NOT exist
    on disk → no honest finish; the actionless valve PAUSES as before."""
    execu = _BrowserlessStaticExecutor(index_exists=False, validation_ok=True)
    state, events = await _approve_and_run(execu, ScriptedAgent(_deliver_then_idle_steps()))

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(
        s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts
    ), sts


@pytest.mark.asyncio
async def test_negative_failed_validation_does_not_honest_finish():
    """NEGATIVE (real failure) — the deliverable exists but the non-browser
    validation FAILS (a broken parse). W-45: a build with a failing validation is
    BROKEN, not unverifiable — no honest finish; PAUSE/actionless instead."""
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=False)
    state, events = await _approve_and_run(execu, ScriptedAgent(_deliver_then_idle_steps()))

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(
        s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts
    ), sts


@pytest.mark.asyncio
async def test_negative_zero_work_after_approval_stucks_not_honest_finish():
    """NEGATIVE (no work / APPROVE_PLAN_NO_EXECUTION) — a plan is approved but the
    model takes ZERO productive actions then tries to finish → STUCK
    (approve_plan_no_execution) via the execution-nudge gate, never an honest
    finish."""
    agent = ScriptedAgent(
        [
            action_step("submit_plan", _PLAN),
            # No file_write / shell — zero productive work.
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    state, events = await _approve_and_run(execu, agent)

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status == ConversationStatus.STUCK, sts
    assert any(d == "approve_plan_no_execution" for _, d in sts), sts
