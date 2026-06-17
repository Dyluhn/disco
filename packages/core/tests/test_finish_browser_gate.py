import pytest
from event_fakes import action, with_seqs
from disco.core import (
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
from disco.core.loop.engine import (
    _browser_verified,
    _is_web_deliverable,
    _last_productive_seq,
    _latest_browser_error,
)
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)


def browser_obs(url: str, console: list, success: bool = True, seq: int = 10) -> ObservationEvent:
    res = ToolResult(
        call_id="c",
        tool_name="browser",
        success=success,
        content="faked",
        structured={"url": url, "console": console},
    )
    return ObservationEvent(tool_result=res, action_id="a").model_copy(update={"seq": seq})


def test_last_productive_seq():
    # Only actions not in _NON_PRODUCTIVE_TOOLS count
    events = with_seqs(
        [
            action(tool="file_read"),  # non-productive
            action(tool="file_write", args={"path": "x"}),  # seq 2 - productive
            action(tool="browser"),  # non-productive
            action(tool="server_status"),  # non-productive
        ]
    )
    assert _last_productive_seq(events) == 2


def test_is_web_deliverable_index_html():
    events = with_seqs([action(tool="file_write", args={"path": "index.html"})])
    assert _is_web_deliverable(events) is True

    events = with_seqs([action(tool="file_edit", args={"path": "./index.html"})])
    assert _is_web_deliverable(events) is True

    events = with_seqs([action(tool="file_write", args={"path": "other.py"})])
    assert _is_web_deliverable(events) is False


def test_is_web_deliverable_server_status():
    # Case: port 8000 owned by 'dev' session
    res = ToolResult(
        call_id="c",
        tool_name="server_status",
        success=True,
        content="SERVER STATUS\nports:\n  - 8000: OWNED by pid 123 (python) [session: dev]",
    )
    events = [ObservationEvent(tool_result=res, action_id="a")]
    assert _is_web_deliverable(events) is True

    # Case: port 8000 owned by 'preview' session
    res = ToolResult(
        call_id="c",
        tool_name="server_status",
        success=True,
        content="SERVER STATUS\nports:\n  - 8000: OWNED by pid 456 (static) [session: preview]",
    )
    events = [ObservationEvent(tool_result=res, action_id="a")]
    assert _is_web_deliverable(events) is False

    # Case: port 8000 FREE
    res = ToolResult(
        call_id="c",
        tool_name="server_status",
        success=True,
        content="SERVER STATUS\nports:\n  - 8000: FREE",
    )
    events = [ObservationEvent(tool_result=res, action_id="a")]
    assert _is_web_deliverable(events) is False


def test_browser_verified_matrix():
    # (a) no browser obs
    assert _browser_verified([], 0) == (False, None)

    # (b) browser_obs BEFORE last edit (since_seq=20)
    events = [browser_obs("http://localhost:8000/", [], seq=15)]
    assert _browser_verified(events, 20) == (False, None)

    # (c) clean obs after last edit
    events = [browser_obs("http://localhost:8000/", [], seq=25)]
    assert _browser_verified(events, 20) == (True, None)

    # (d) obs with console errors
    errs = [{"level": "error", "text": "Uncaught SyntaxError"}]
    events = [browser_obs("http://127.0.0.1:8000/", errs, seq=25)]
    assert _browser_verified(events, 20) == (False, "Uncaught SyntaxError")

    # Multiple obs: latest with errors wins for error message
    errs2 = [{"level": "error", "text": "Second Error"}]
    events = [
        browser_obs("http://localhost:8000/", errs, seq=25),
        browser_obs("http://localhost:8000/", errs2, seq=30),
    ]
    ok, err = _browser_verified(events, 20)
    assert ok is False
    assert err == "Second Error"

    # Mixed obs: one clean after last edit satisfies the gate
    events = [
        browser_obs("http://localhost:8000/", errs, seq=25),
        browser_obs("http://localhost:8000/", [], seq=30),
    ]
    ok, err = _browser_verified(events, 20)
    assert ok is True

    # Wrong URL doesn't count
    events = [browser_obs("http://google.com", [], seq=25)]
    assert _browser_verified(events, 20) == (False, None)


def test_latest_browser_error_full_history():
    errs = [{"level": "error", "text": "Uncaught SyntaxError"}]
    # Never browsed → None
    assert _latest_browser_error([]) is None
    # Latest qualifying obs has errors → quoted, regardless of any since_seq scope
    events = [browser_obs("http://127.0.0.1:8000/", errs, seq=5)]
    assert _latest_browser_error(events) == "Uncaught SyntaxError"
    # Latest obs is CLEAN → None even if an older one errored ("last load" is true)
    events = [
        browser_obs("http://127.0.0.1:8000/", errs, seq=5),
        browser_obs("http://127.0.0.1:8000/", [], seq=9),
    ]
    assert _latest_browser_error(events) is None
    # Non-:8000 obs are ignored entirely
    events = [browser_obs("http://google.com", errs, seq=5)]
    assert _latest_browser_error(events) is None


# ---- gate behavior through the REAL loop (order matrix a, c, d, e) ----------
#
# ScriptedAgent + a per-tool executor drive AgentLoop.run() end-to-end; the
# assertions read the persisted event log — nudges/⚠ are checked VERBATIM, not
# re-derived from the helpers.

_NUDGE_VERBATIM = (
    "Before finishing: verify your app the way a user would. "
    "Use the browser tool to navigate to http://127.0.0.1:8000/, "
    "read the CONSOLE output, and fix any errors you see. "
    "Finish only after a clean load."
)


class BrowserExecutor(FakeExecutor):
    """FakeExecutor that answers `browser` calls with a daemon-shaped structured
    payload (BP-04 contract); everything else gets the stock ok result."""

    def __init__(self, console: list):
        super().__init__(
            tools=[
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="browser", description="browse", parameters_schema={}),
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
            ]
        )
        self._console = console

    async def execute(self, call):
        if call.tool_name == "browser":
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="browser",
                success=True,
                content="[UNTRUSTED WEB CONTENT]\nURL: http://127.0.0.1:8000/\nTITLE: t",
                structured={
                    "ok": True,
                    "url": "http://127.0.0.1:8000/",
                    "title": "t",
                    "console": self._console,
                    "elements": [],
                    "text": "t",
                    "screenshot_path": ".pmx/screenshots/0001-navigate.png",
                },
            )
        return await super().execute(call)


def _gate_loop(agent, executor):
    """AgentLoop in Build-execution shape: planning_tools configured, mode is
    NOT PLANNING — the exact guard the gate shares with the execution gate."""
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


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


@pytest.mark.asyncio
async def test_loop_refuses_then_release_valve():
    # (a) + (e): web deliverable, agent NEVER browses, insists on finishing.
    # Expect exactly 3 verbatim nudges, then the ⚠ valve message, then FINISHED.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            finish_step(),  # ScriptedAgent repeats the last step on every refusal
        ]
    )
    loop, store = _gate_loop(agent, BrowserExecutor(console=[]))
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    nudges = [m for m in env if m == _NUDGE_VERBATIM]
    warns = [m for m in env if m.startswith("⚠ finished WITHOUT a clean browser verification")]
    assert len(nudges) == 3, f"expected 3 verbatim refusal nudges, got {len(nudges)}: {env}"
    assert len(warns) == 1, f"expected one ⚠ valve message, got: {env}"
    assert "none seen" in warns[0]  # agent never looked at all
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses  # the valve released the run — no deadlock


@pytest.mark.asyncio
async def test_loop_clean_browse_finishes_first_try():
    # (c): edit → clean browser load on :8000 → finish sails through, zero nudges.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            action_step(tool="browser", args={"action": "navigate", "url": "http://127.0.0.1:8000/"}),
            finish_step(),
        ]
    )
    loop, store = _gate_loop(agent, BrowserExecutor(console=[]))
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    assert not any(m.startswith("Before finishing:") for m in env), env
    assert not any(m.startswith("⚠ finished WITHOUT") for m in env), env
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses


@pytest.mark.asyncio
async def test_loop_error_console_quotes_error_in_nudge():
    # (d) through the loop: the page throws; every refusal quotes the first
    # error line, and the eventual ⚠ valve carries it too.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            action_step(tool="browser", args={"action": "navigate", "url": "http://127.0.0.1:8000/"}),
            finish_step(),
        ]
    )
    err_console = [{"level": "error", "text": "Uncaught SyntaxError: boom"}]
    loop, store = _gate_loop(agent, BrowserExecutor(console=err_console))
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    quoting = [m for m in env if "The last load had errors: Uncaught SyntaxError: boom" in m]
    warns = [m for m in env if m.startswith("⚠ finished WITHOUT a clean browser verification")]
    assert len(quoting) == 3, f"expected 3 error-quoting nudges, got {len(quoting)}: {env}"
    assert len(warns) == 1 and "Uncaught SyntaxError: boom" in warns[0]
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses


@pytest.mark.asyncio
async def test_loop_post_browse_edit_still_quotes_seen_error():
    # Regression (caught LIVE, behavioral run A conv_ac357a5c): the agent browsed,
    # SAW the error, then edited a file before insisting on finish. The edit moves
    # since_seq past the observation — the verification is rightly invalidated,
    # but the ⚠ valve must still quote what was seen, not claim "none seen".
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            action_step(tool="browser", args={"action": "navigate", "url": "http://127.0.0.1:8000/"}),
            action_step(tool="file_write", args={"path": "notes.txt", "content": "tried stuff"}),
            finish_step(),
        ]
    )
    err_console = [{"level": "error", "text": "BP05-SEEDED-ERROR"}]
    loop, store = _gate_loop(agent, BrowserExecutor(console=err_console))
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    warns = [m for m in env if m.startswith("⚠ finished WITHOUT a clean browser verification")]
    assert len(warns) == 1
    assert "BP05-SEEDED-ERROR" in warns[0], f"valve erased the seen error: {warns[0]!r}"
    quoting = [m for m in env if "The last load had errors: BP05-SEEDED-ERROR" in m]
    assert len(quoting) == 3, f"nudges should quote the seen error: {env}"
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses
