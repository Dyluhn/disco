import pytest
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
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
from disco.core.loop.finish import (
    _browser_content_meaningful,
    _latest_browser_structured,
)
from disco.core.loop.signals import (
    actions_since_last_resume,
    productive_actions_since_approval,
)
from event_fakes import action, agent_error, observation, with_seqs
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
    failed = action(tool="file_edit", args={"path": "x"})
    write = action(tool="file_write", args={"path": "x"})
    preview_controls = [
        action(tool=name)
        for name in ("preview_start", "preview_status", "preview_logs", "preview_stop")
    ]
    events = with_seqs(
        [
            action(tool="file_read"),  # non-productive
            failed,  # productive-looking, but failed
            agent_error("FRESH_READ_REQUIRED", action_id=failed.id),
            write,  # seq 4 - productive and successful
            observation(action_id=write.id, tool="file_write"),
            action(tool="browser"),  # non-productive
            action(tool="server_status"),  # non-productive
            *[
                event
                for preview in preview_controls
                for event in (
                    preview,
                    observation(action_id=preview.id, tool=preview.tool_call.tool_name),
                )
            ],
        ]
    )
    assert _last_productive_seq(events) == 4


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


# ---- H357: preview_start action/observation pair signals web intent ---------


def _preview_action() -> ActionEvent:
    return ActionEvent(
        thought="preview",
        tool_call=ToolCall(tool_name="preview_start", arguments={}),
    )


def _preview_observation(action_id: str, *, success: bool = True) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id="c",
            tool_name="preview_start",
            success=success,
            content="preview started on port 8000" if success else "preview failed",
            structured={"status": "running" if success else "crashed"},
        ),
        action_id=action_id,
    )


def test_is_web_deliverable_preview_start_success():
    """preview_start action + matching successful observation → True."""
    act = _preview_action()
    obs = _preview_observation(act.id)
    assert _is_web_deliverable([act, obs]) is True


def test_is_web_deliverable_preview_start_failed_observation():
    """preview_start action with a failed (success=False) observation → False."""
    act = _preview_action()
    obs = _preview_observation(act.id, success=False)
    assert _is_web_deliverable([act, obs]) is False


def test_is_web_deliverable_preview_start_agent_error():
    """preview_start action with an AgentErrorEvent → False."""
    act = _preview_action()
    err = AgentErrorEvent(error="preview crashed", action_id=act.id)
    assert _is_web_deliverable([act, err]) is False


def test_is_web_deliverable_preview_start_orphan_observation():
    """Observation with action_id that matches no action → False."""
    obs = _preview_observation("nonexistent-id")
    assert _is_web_deliverable([obs]) is False


def test_is_web_deliverable_preview_start_action_only():
    """Action without any matching observation → False."""
    act = _preview_action()
    assert _is_web_deliverable([act]) is False


def test_is_web_deliverable_preview_start_wrong_tool_observation():
    """Observation with wrong tool_name (preview_stop) → False even when
    action_id matches a preview_start action."""
    act = _preview_action()
    obs = ObservationEvent(
        tool_result=ToolResult(
            call_id="c", tool_name="preview_stop", success=True, content="stopped"
        ),
        action_id=act.id,
    )
    assert _is_web_deliverable([act, obs]) is False


def test_is_web_deliverable_preview_start_observation_before_action():
    """Observation before its matching action must NOT count — a single forward
    scan should reject the reordered pair."""
    act = _preview_action()
    obs = _preview_observation(act.id, success=True)
    # Observation comes BEFORE the action in the event list
    assert _is_web_deliverable([obs, act]) is False


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
            action_step(
                tool="browser", args={"action": "navigate", "url": "http://127.0.0.1:8000/"}
            ),
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
            action_step(
                tool="browser", args={"action": "navigate", "url": "http://127.0.0.1:8000/"}
            ),
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
            action_step(
                tool="browser", args={"action": "navigate", "url": "http://127.0.0.1:8000/"}
            ),
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


# ---- FIX 1: ACTIVE finish-browser probe (verification-overclaim) ------------
#
# The gate no longer waits for the agent to browse. When no clean :8000 obs
# exists since the last edit, the gate DRIVES `browser navigate` itself and
# judges on ground truth — so an overclaiming agent that never browsed can't
# land a JS-broken/blank page as FINISHED.


class ConfigurableBrowserExecutor(FakeExecutor):
    """Browser executor whose `browser` result payload is fully configurable so
    the gate's OWN driven probe can be exercised with clean / console-error /
    blank renders. `has_browser=False` removes the browser tool entirely
    (process-backend shape) to exercise the degrade-to-passive path. Non-browser
    calls get the stock ok result."""

    def __init__(
        self,
        *,
        console=None,
        title="t",
        text="t",
        elements=None,
        success=True,
        has_browser=True,
        url="http://127.0.0.1:8000/",
    ):
        tools = [
            ToolSpec(name="file_write", description="write", parameters_schema={}),
            ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
        ]
        if has_browser:
            tools.append(ToolSpec(name="browser", description="browse", parameters_schema={}))
        super().__init__(tools=tools)
        self._console = console or []
        self._title = title
        self._text = text
        self._elements = elements or []
        self._browser_success = success
        # The URL the daemon REPORTS in its observation. The platform assigns a
        # random preview port — the finish gate must accept the observation on THAT
        # port, not only :8000 (and reject a foreign port even when console-clean).
        self._url = url
        self.browser_calls = 0

    async def execute(self, call):
        if call.tool_name == "browser":
            self.browser_calls += 1
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="browser",
                success=self._browser_success,
                content=f"[UNTRUSTED WEB CONTENT]\nURL: {self._url}",
                structured={
                    "ok": True,
                    "url": self._url,
                    "title": self._title,
                    "console": self._console,
                    "elements": self._elements,
                    "text": self._text,
                    "screenshot_path": ".pmx/screenshots/0001-navigate.png",
                },
            )
        return await super().execute(call)


def _never_browses_agent():
    # Web deliverable written, then the agent insists on finishing WITHOUT ever
    # browsing. ScriptedAgent repeats the last step (finish) on every refusal.
    return ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            finish_step(),
        ]
    )


def test_browser_content_meaningful_pure():
    # title+text >= 20 chars → meaningful
    assert _browser_content_meaningful({"title": "My App", "text": "Welcome to the page"})
    # blank: sparse title/text + no elements → NOT meaningful
    assert not _browser_content_meaningful({"title": "t", "text": "", "elements": []})
    # interactive structure (non-empty elements) → meaningful even with sparse text
    assert _browser_content_meaningful({"title": "", "text": "", "elements": ["1[:] <a>Home</a>"]})
    # explicit links/forms count fields honored
    assert _browser_content_meaningful({"title": "", "text": "", "forms": 1})
    # A short rendered semantic element is genuine content even below the legacy
    # 20-character threshold (H335's live <h1>Live Server Up</h1> case).
    assert _browser_content_meaningful(
        {
            "title": "",
            "text": "Live Server Up",
            "elements": [],
            "visible_semantic_elements": 1,
        }
    )
    # Missing, zero, malformed, or boolean evidence must not bless an empty shell.
    for value in (0, -1, True, "1", None):
        assert not _browser_content_meaningful(
            {"title": "", "text": "", "elements": [], "visible_semantic_elements": value}
        )
    # _latest_browser_structured picks the latest qualifying :8000 obs
    events = [
        browser_obs("http://127.0.0.1:8000/", [], seq=5),
        browser_obs("http://google.com", [], seq=9),  # ignored (wrong URL)
    ]
    assert _latest_browser_structured(events) is not None
    assert _latest_browser_structured([]) is None


@pytest.mark.asyncio
async def test_active_probe_drives_browser_when_agent_never_browsed_console_error():
    # (a) Agent never browses; the gate DRIVES the probe, which surfaces a console
    # error → finish REFUSED and the nudge quotes the error. Bounded: 3 nudges
    # then the ⚠ valve releases (no hang).
    execu = ConfigurableBrowserExecutor(
        console=[{"level": "error", "text": "Uncaught ReferenceError: boom"}],
        text="A rendered page with plenty of visible text content here",
    )
    loop, store = _gate_loop(_never_browses_agent(), execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    quoting = [m for m in env if "The last load had errors: Uncaught ReferenceError: boom" in m]
    assert len(quoting) == 3, f"gate should drive + quote the error 3×: {env}"
    warns = [m for m in env if m.startswith("⚠ finished WITHOUT a clean browser verification")]
    assert len(warns) == 1 and "Uncaught ReferenceError: boom" in warns[0]
    # the gate itself drove the browse (agent never emitted a browser step)
    assert execu.browser_calls >= 1
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses  # valve released — no deadlock


@pytest.mark.asyncio
async def test_active_probe_clean_render_finishes():
    # (b) Agent never browses; the gate drives a probe that loads clean console +
    # meaningful content → finish PASSES first try, zero nudges.
    execu = ConfigurableBrowserExecutor(
        console=[],
        title="Todo App",
        text="A fully rendered to-do application with a list of tasks",
    )
    loop, store = _gate_loop(_never_browses_agent(), execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    assert not any(m.startswith("Before finishing:") for m in env), env
    assert not any(m.startswith("⚠ finished WITHOUT") for m in env), env
    assert execu.browser_calls >= 1  # the gate verified for the overclaiming agent
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses


@pytest.mark.asyncio
async def test_active_probe_blank_render_refused():
    # (c) Clean console BUT the page rendered nothing (empty text, no elements) —
    # serves-200-but-blank. The blank-render guard refuses the finish even though
    # the console is clean.
    execu = ConfigurableBrowserExecutor(console=[], title="t", text="", elements=[])
    loop, store = _gate_loop(_never_browses_agent(), execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    nudges = [m for m in env if m == _NUDGE_VERBATIM]
    assert len(nudges) == 3, f"blank render should be refused (clean console ≠ works): {env}"
    warns = [m for m in env if m.startswith("⚠ finished WITHOUT a clean browser verification")]
    assert len(warns) == 1
    assert execu.browser_calls >= 1
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses


@pytest.mark.asyncio
async def test_active_probe_browser_unavailable_degrades():
    # (d) Browserless backend (no `browser` tool): the probe is a no-op and the
    # gate degrades to today's passive nudge/release — no hang, no crash, and the
    # gate never emits a browser action it can't run.
    execu = ConfigurableBrowserExecutor(has_browser=False)
    loop, store = _gate_loop(_never_browses_agent(), execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    nudges = [m for m in env if m == _NUDGE_VERBATIM]
    assert len(nudges) == 3, f"degrade path should still nudge 3×: {env}"
    warns = [m for m in env if m.startswith("⚠ finished WITHOUT a clean browser verification")]
    assert len(warns) == 1 and "none seen" in warns[0]
    # the probe never emitted a browser action (no browser backend to drive)
    assert execu.browser_calls == 0
    from disco.core import ActionEvent

    assert not any(
        isinstance(e, ActionEvent) and e.tool_call and e.tool_call.tool_name == "browser"
        for e in events
    )
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses  # no deadlock


@pytest.mark.asyncio
async def test_active_probe_not_counted_as_agent_work():
    # (e) The gate's driven probe is tagged verify_probe → it must NOT count as
    # agent work (the re-run #6 leak). Only the single file_write is productive.
    from disco.core import ActionEvent

    execu = ConfigurableBrowserExecutor(console=[], title="t", text="", elements=[])
    loop, store = _gate_loop(_never_browses_agent(), execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    # the gate DID drive a probe (browser ActionEvent tagged verify_probe exists)
    probe_actions = [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call and e.tool_call.tool_name == "browser"
    ]
    assert probe_actions, "the gate should have driven a browser probe"
    assert all(e.meta.get("verify_probe") for e in probe_actions)
    # despite N driven probes, agent-work accounting counts only the file_write
    assert productive_actions_since_approval(events) == 1
    assert actions_since_last_resume(events) == 1


# ---- pure-reader port binding (target_key) ----------------------------------
#
# The platform assigns a RANDOM preview port; the readers must accept an
# observation on the RESOLVED preview port and REJECT a foreign one — while
# preserving the historical :8000 fallback when the preview is undetectable
# (target_key=None, the path the legacy/sandbox-less unit tests rely on).


def test_url_targets_preview_binding():
    from disco.core.loop.finish import _preview_key, _url_targets_preview

    key = _preview_key("http://127.0.0.1:54321/")
    # bound to the resolved random port → accept that port, reject others (incl 8000)
    assert _url_targets_preview("http://127.0.0.1:54321/", key) is True
    assert _url_targets_preview("http://localhost:54321/", key) is True  # loopback alias
    assert _url_targets_preview("http://127.0.0.1:8000/", key) is False
    assert _url_targets_preview("http://127.0.0.1:9999/", key) is False
    # undetectable preview (None) → historical :8000 acceptance only
    assert _url_targets_preview("http://127.0.0.1:8000/", None) is True
    assert _url_targets_preview("http://localhost:8000/", None) is True
    assert _url_targets_preview("http://127.0.0.1:54321/", None) is False


def test_browser_readers_accept_resolved_nondefault_port():
    from disco.core.loop.finish import (
        _browser_verified,
        _latest_browser_error,
        _latest_browser_structured,
    )

    key = ("127.0.0.1", 54321)
    errs = [{"level": "error", "text": "boom on the random port"}]
    # clean obs on the RESOLVED random port is accepted
    events = [browser_obs("http://127.0.0.1:54321/", [], seq=25)]
    assert _browser_verified(events, 20, key) == (True, None)
    assert _latest_browser_structured(events, key) is not None
    # error obs on the resolved port is surfaced
    events = [browser_obs("http://127.0.0.1:54321/", errs, seq=25)]
    assert _browser_verified(events, 20, key) == (False, "boom on the random port")
    assert _latest_browser_error(events, key) == "boom on the random port"
    # a FOREIGN port (incl :8000) is rejected when bound to :54321
    events = [browser_obs("http://127.0.0.1:8000/", [], seq=25)]
    assert _browser_verified(events, 20, key) == (False, None)
    assert _latest_browser_structured(events, key) is None
    assert _latest_browser_error(events, key) is None


# ---- gate end-to-end on a RANDOM (non-:8000) preview port -------------------


def _patch_detect(loop, url: str | None):
    async def _fake_detect() -> str | None:
        return url

    loop._finish._detect_preview_url = _fake_detect  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_active_probe_drives_resolved_random_port_and_finishes():
    # The preview serves on a platform-assigned random port (54321). The agent
    # never browses; the gate must DRIVE the probe AGAINST 54321 (not a dead :8000),
    # accept the clean+meaningful render on that port, and finish — zero nudges.
    execu = ConfigurableBrowserExecutor(
        console=[],
        title="Todo App",
        text="A fully rendered to-do application with a list of tasks",
        url="http://127.0.0.1:54321/",
    )
    loop, store = _gate_loop(_never_browses_agent(), execu)
    _patch_detect(loop, "http://127.0.0.1:54321/")
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    assert not any(m.startswith("Before finishing:") for m in env), env
    assert not any(m.startswith("⚠ finished WITHOUT") for m in env), env
    # the gate drove the probe against the RESOLVED port, never :8000
    from disco.core import ActionEvent

    probe_urls = [
        e.tool_call.arguments.get("url")
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call and e.tool_call.tool_name == "browser"
    ]
    assert probe_urls and all(u == "http://127.0.0.1:54321/" for u in probe_urls), probe_urls
    assert all("8000" not in str(u) for u in probe_urls)
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses


@pytest.mark.asyncio
async def test_active_probe_foreign_port_observation_is_rejected():
    # The preview is resolved to :54321 but the browser observation comes back on a
    # FOREIGN port (:9999) with a clean console + meaningful content. Binding to the
    # resolved preview must REJECT it — so the gate refuses (3 nudges, pointed at the
    # resolved :54321) then releases the valve, never landing the foreign page as done.
    execu = ConfigurableBrowserExecutor(
        console=[],
        title="Unrelated",
        text="A clean and meaningful page served on the wrong port entirely",
        url="http://127.0.0.1:9999/",
    )
    loop, store = _gate_loop(_never_browses_agent(), execu)
    _patch_detect(loop, "http://127.0.0.1:54321/")
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env_messages(events)
    nudges = [
        m for m in env if m.startswith("Before finishing:") and "http://127.0.0.1:54321/" in m
    ]
    assert len(nudges) == 3, f"foreign-port obs must be rejected + nudge at :54321: {env}"
    assert not any("8000" in m for m in nudges)  # never the dead fixed port
    warns = [m for m in env if m.startswith("⚠ finished WITHOUT a clean browser verification")]
    assert len(warns) == 1
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses  # valve released — no deadlock


# ---- H357: preview_start triggers the verify gate end-to-end -----------------


class PreviewBuildExecutor(FakeExecutor):
    """Executor with file_write, preview_start, shell, code_exec, and
    verify_web_app.  preview_start returns success; verify_web_app returns a
    structured PASS verdict.  All other tools fall through to the default
    success=True."""

    def __init__(self):
        tools = [
            ToolSpec(name="file_write", description="write", parameters_schema={}),
            ToolSpec(name="preview_start", description="start preview", parameters_schema={}),
            ToolSpec(name="shell", description="shell", parameters_schema={}),
            ToolSpec(name="code_exec", description="execute code", parameters_schema={}),
            ToolSpec(name="verify_web_app", description="verify web app", parameters_schema={}),
            ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
        ]
        super().__init__(tools=tools)
        self.verify_app_calls = 0

    async def execute(self, call):
        if call.tool_name == "preview_start":
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="preview_start",
                success=True,
                content="preview started on http://127.0.0.1:8000/",
                structured={
                    "status": "running",
                    "url": "http://127.0.0.1:8000/",
                    "port": 8000,
                    "command": "python3 server.py",
                },
            )
        if call.tool_name == "verify_web_app":
            self.verify_app_calls += 1
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="verify_web_app",
                success=True,
                content="verified",
                structured={
                    "passed": True,
                    "verdict": "pass",
                    "url": "http://127.0.0.1:8000/",
                    "http_status": 200,
                    "title": "Test App",
                    "meaningful_content": True,
                    "visible_text_chars": 50,
                    "elements_count": 3,
                    "console_errors": [],
                    "console_warnings": [],
                    "network_failures": [],
                    "checks": [],
                    "summary": "App verified successfully",
                },
            )
        return await super().execute(call)


@pytest.mark.asyncio
async def test_preview_start_triggers_verify_gate():
    """H357 regression: Build writes server.py, preview_start with command
    python3 server.py succeeds (returns structured running preview with
    URL/port), code_exec and shell local probes succeed, then the agent
    attempts tool-less finish without any browser/verifier observation.
    The preview_start signal must route through the existing verify gate:
    the gate auto-drives verify_web_app and lands a clean FINISHED.
    Assert a gate-owned verify probe (ActionEvent with verify_probe meta)
    causally precedes FINISHED and no preexisting agent verifier proof
    made the test vacuous."""
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "server.py", "content": "..."}),
            action_step(tool="preview_start", args={"command": "python3 server.py"}),
            action_step(tool="code_exec", args={"code": "..."}),
            action_step(tool="shell", args={"command": "curl localhost:8000"}),
            finish_step(),
        ]
    )
    execu = PreviewBuildExecutor()
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build a web server")
    await loop.run()

    events = await store.get_events("conv")

    # The gate must have driven verify_web_app because of the preview_start signal.
    # Assert the OBSERVATION exists.
    verify_obs = [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.tool_name == "verify_web_app"
    ]
    assert verify_obs, (
        "fix should route through verify_web_app gate — "
        "verify_web_app observation must exist in events"
    )

    # The verify_web_app observation (gate-driven) must causally precede FINISHED.
    finished_seq: int | None = None
    for e in events:
        if isinstance(e, StatusEvent) and e.status.value == "FINISHED":
            finished_seq = e.seq
            break
    assert finished_seq is not None, "must land FINISHED"
    for obs in verify_obs:
        obs_seq = getattr(obs, "seq", None)
        assert obs_seq is not None and obs_seq < finished_seq, (
            f"verify_web_app obs seq {obs_seq} must precede FINISHED seq {finished_seq}"
        )

    # Assert the verify_web_app action is GATE-OWNED (tagged verify_probe)
    # — no preexisting agent verifier proof made the test vacuous.
    verify_actions = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call
        and e.tool_call.tool_name == "verify_web_app"
    ]
    assert verify_actions, "verify_web_app action must exist (gate-driven probe)"
    assert execu.verify_app_calls == 1, "the finish gate must drive exactly one verifier call"
    assert all(
        e.meta.get("verify_probe") for e in verify_actions
    ), "all verify_web_app actions must be gate-owned (verify_probe)"
    # The gate-driven action precedes FINISHED
    for act in verify_actions:
        act_seq = getattr(act, "seq", None)
        assert act_seq is not None and act_seq < finished_seq, (
            f"verify_web_app action seq {act_seq} must precede FINISHED seq {finished_seq}"
        )

    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" in statuses

    # Without the fix (no preview_start detection) _is_web_deliverable
    # returns False for these events — verify the fix is causal.
    # server.py is NOT index.html and no server_status observation exists.
    assert _is_web_deliverable(events) is True, "fix must mark events as web deliverable"
