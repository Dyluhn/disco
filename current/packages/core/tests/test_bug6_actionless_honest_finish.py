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
    MessageEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import SealabilityProbeResult
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
        isinstance(e, StatusEvent) and e.detail == "unverifiable_static_finish" for e in events
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


class _BrokenThenUnavailableExecutor(_BrowserlessStaticExecutor):
    """The first browser navigate SUCCEEDS but the page is broken (a network
    failure, or a blank render); a later navigate finds the daemon gone
    (unavailable). This makes conditions 1-5 of the honest finish pass (the browser
    IS observed-unavailable on the second call) so the test isolates condition 6 —
    `_real_web_failure_evidence` must BLOCK on the first observation's failure."""

    def __init__(self, *, mode):
        super().__init__(index_exists=True, validation_ok=True)
        self._mode = mode  # "network" | "blank"
        self._browser_calls = 0

    async def execute(self, call):
        if call.tool_name == "browser":
            self.calls.append(call)
            self._browser_calls += 1
            if self._browser_calls == 1:
                if self._mode == "network":
                    structured = {
                        "url": "http://127.0.0.1:8000/",
                        "console": [],
                        "network": [
                            {"url": "http://127.0.0.1:8000/app.js", "failure": "net::ERR_FAILED"}
                        ],
                        "title": "Bakery",
                        "text": "Welcome to the bakery — fresh bread daily.",
                        "elements": [{"tag": "h1"}],
                    }
                else:  # blank render — served but nothing mounted
                    structured = {
                        "url": "http://127.0.0.1:8000/",
                        "console": [],
                        "network": [],
                        "title": "",
                        "text": "",
                        "elements": [],
                    }
                return ToolResult(
                    call_id=call.call_id,
                    tool_name="browser",
                    success=True,
                    content="navigated",
                    structured=structured,
                )
            # later navigate: the daemon died → unavailable (the condition-5 signal).
            return ToolResult(
                call_id=call.call_id,
                tool_name="browser",
                success=False,
                content="",
                structured={"browser_unavailable": True},
                error=BROWSER_UNAVAILABLE_MSG,
            )
        return await super().execute(call)


_PLAN = {
    "summary": "bakery landing page",
    "steps": [
        {"title": "Build the HTML structure in index.html"},
        {"title": "Add the CSS styling in style.css"},
        {"title": "Verify the page renders correctly in the browser"},
    ],
}
# A plan whose final not-done step is CONTENT (not verification) — for the
# verify-only-lexicon negative (a content step must never read as verify-only).
_PLAN_CONTENT_TAIL = {
    "summary": "bakery landing page",
    "steps": [
        {"title": "Build the HTML structure in index.html"},
        {"title": "Add the CSS styling in style.css"},
        {"title": "Add a testimonials section"},
    ],
}


def _plan_with_tail(title):
    return {
        "summary": "bakery landing page",
        "steps": [
            {"title": "Build the HTML structure in index.html"},
            {"title": "Add the CSS styling in style.css"},
            {"title": title},
        ],
    }


_HTML = "<html><body><h1>Bakery</h1></body></html>"
# A REAL content/structure validation (HTML parser) referencing index.html.
_VALIDATE_CMD = (
    "python3 -c \"from html.parser import HTMLParser as P; P().feed(open('index.html').read())\""
)
# A bare existence check — proves the file EXISTS, validates NOTHING about content.
_EXISTENCE_CMD = "ls -la index.html"
_NAVIGATE = action_step("browser", {"action": "navigate", "url": "http://127.0.0.1:8000/"})
_IDLE_TAIL = [
    _notify("Files delivered; browser verification isn't available on this backend."),
    _notify("The static deliverable is in place; I validated the HTML parses."),
    _notify("Done — the page is built; render couldn't be browser-checked here."),
    _notify("Standing by."),
    finish_step(),
]


def _deliver_then_idle_steps(
    *,
    plan=_PLAN,
    done_idxs=(1, 2),
    active_idx=3,
    validate_cmd=_VALIDATE_CMD,
    browser_steps=(_NAVIGATE,),
):
    """Write both deliverables, mark the build steps done + the active step active,
    attempt browser-verify (unavailable) + a shell validation, then go idle (notify
    spam) — the Bug 6 reproduction shape, parameterized for the negative variants."""
    steps = [
        action_step("submit_plan", plan),
        action_step("file_write", {"path": "index.html", "content": _HTML}),
        action_step("file_write", {"path": "style.css", "content": "h1{color:#a30}"}),
    ]
    steps += [action_step("plan_step", {"index": i, "state": "done"}) for i in done_idxs]
    steps.append(action_step("plan_step", {"index": active_idx, "state": "active"}))
    steps += list(browser_steps)
    steps.append(action_step("shell", {"command": validate_cmd}))
    steps += _IDLE_TAIL
    return steps


async def _approve_and_run(executor, agent, *, probe=None):
    loop, store = build_loop(
        executor=executor,
        agent=agent,
        planning_tools=frozenset(["file_read"]),
        finish_sealability_probe=probe,
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
    assert not any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts
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
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


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
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


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
    assert (
        state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    )  # terminal-collapse landing, sts
    assert any(d == "approve_plan_no_execution" for _, d in sts), sts


@pytest.mark.asyncio
async def test_negative_content_step_not_verify_does_not_honest_finish():
    """NEGATIVE (codex #1 — verify-only lexicon) — the build is otherwise
    finishable (deliverable on disk, a real validation passed, browser
    unavailable) but the only NOT-DONE step is CONTENT ("Add a testimonials
    section"), NOT verification. A content step must never read as verify-only →
    NO honest finish; the valve PAUSES so the remaining work isn't dropped."""
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    agent = ScriptedAgent(
        _deliver_then_idle_steps(plan=_PLAN_CONTENT_TAIL, done_idxs=(1, 2), active_idx=3)
    )
    state, events = await _approve_and_run(execu, agent)

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


@pytest.mark.asyncio
async def test_negative_existence_check_is_not_a_validation():
    """NEGATIVE (codex #2 — real validation required) — same finishable shape but
    the only post-write shell is `ls index.html`, which proves the file EXISTS
    (condition 3 already), NOT that its content is valid. A bare existence check is
    NOT a content validation → NO honest finish; PAUSE/actionless."""
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    agent = ScriptedAgent(_deliver_then_idle_steps(validate_cmd=_EXISTENCE_CMD))
    state, events = await _approve_and_run(execu, agent)

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["network", "blank"])
async def test_negative_browser_failure_evidence_blocks_honest_finish(mode):
    """NEGATIVE (codex #3 — real web-failure evidence) — conditions 1-5 all hold
    (the second navigate is observed-unavailable), but a SUCCESSFUL browser
    observation shows a NETWORK failure (mode=network) or a BLANK render
    (mode=blank). Such evidence means the app is BROKEN, not merely unverifiable →
    NO honest finish (W-45); PAUSE/actionless instead."""
    execu = _BrokenThenUnavailableExecutor(mode=mode)
    # Two navigates: the first observes the broken page, the second is unavailable.
    agent = ScriptedAgent(_deliver_then_idle_steps(browser_steps=(_NAVIGATE, _NAVIGATE)))
    state, events = await _approve_and_run(execu, agent)

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tail",
    [
        "Create product renders",  # "renders" the NOUN (images/output), not a phrase
        "Add product renders",
        "Add input validation",  # "validation" the content noun (a feature), not verify
        "Add form validation",
        "Set up linting",  # "linting" the content noun (tooling config), not verify
        "Configure ESLint",
        "Add a testimonials section",  # "test" inside a content noun
    ],
)
async def test_negative_verification_word_as_content_noun_not_verify(tail):
    """NEGATIVE (codex re-review — the content-noun CLASS) — the only not-done step is
    CONTENT whose text contains a verification-ish WORD as a noun or a creation verb
    ("Create product renders", "Add input validation", "Set up linting"). The
    structural classifier (verification-ACTION framing AND no creation verb) must read
    these as NON-verify → NO honest finish; PAUSE/actionless. Kills the whole
    test→testimonials / render→renders / validation→input-validation class."""
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    agent = ScriptedAgent(
        _deliver_then_idle_steps(plan=_plan_with_tail(tail), done_idxs=(1, 2), active_idx=3)
    )
    state, events = await _approve_and_run(execu, agent)

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["echo validate index.html", 'printf "markup" index.html'])
async def test_negative_echoed_validation_word_is_not_a_validation(cmd):
    """NEGATIVE (codex re-review #2 — echoed word ≠ validation) — the only post-write
    shell merely ECHOES the word "validate"/"markup" alongside index.html (the shell
    succeeds), but no parser/validator is actually invoked. Structural classification
    rejects it → NO honest finish; PAUSE/actionless."""
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    agent = ScriptedAgent(_deliver_then_idle_steps(validate_cmd=cmd))
    state, events = await _approve_and_run(execu, agent)

    sts = _statuses(events)
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


class _SealProbe:
    """Constant-result sealability probe with a call counter."""

    def __init__(self, result: SealabilityProbeResult) -> None:
        self.result = result
        self.calls = 0

    async def __call__(self) -> SealabilityProbeResult:
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_negative_seal_blocking_probe_valve_never_lands_unsealed_finished():
    """NEGATIVE (REL-27 / F-27 — the valve is an affirmative FINISHED site) — the
    exact positive honest-finish scenario, but the workspace cannot be sealed
    (symlinked deliverable, the 590005 signature). The valve must consult the
    finish-time seal gate and DECLINE: the refusal reminder names the blocking
    entry, no FINISHED lands, and the preexisting safe PAUSE/actionless terminal
    is preserved. Deleting the valve's gate call would land an unsealed FINISHED
    and fail this test."""
    probe = _SealProbe(
        SealabilityProbeResult(sealable=False, blocking=("index.html: symlink excluded",))
    )
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    agent = ScriptedAgent(_deliver_then_idle_steps())
    state, events = await _approve_and_run(execu, agent, probe=probe)

    sts = _statuses(events)
    assert probe.calls >= 1, "the valve never consulted the sealability gate"
    refusals = [
        e
        for e in events
        if isinstance(e, MessageEvent) and e.meta.get("blocking") == "finish_seal_refused"
    ]
    assert refusals, sts
    # Typed field first (the contract), prose only for the human remedy.
    assert tuple(refusals[0].meta["seal_blocking"]) == ("index.html: symlink excluded",)
    assert "finish again" in refusals[0].message.content
    assert not _has_honest_marker(events), sts
    assert state.execution_status != ConversationStatus.FINISHED, sts
    assert any(s == ConversationStatus.PAUSED and d == "actionless" for s, d in sts), sts


@pytest.mark.asyncio
async def test_positive_sealable_probe_valve_still_honest_finishes():
    """POSITIVE (REL-27 control) — with a live probe reporting SEALABLE, the valve
    behaves byte-for-byte like the probe-less positive: honest
    `unverifiable_static_finish` marker + FINISHED, no refusal, no pause."""
    probe = _SealProbe(SealabilityProbeResult(sealable=True))
    execu = _BrowserlessStaticExecutor(index_exists=True, validation_ok=True)
    agent = ScriptedAgent(_deliver_then_idle_steps())
    state, events = await _approve_and_run(execu, agent, probe=probe)

    sts = _statuses(events)
    assert probe.calls >= 1, "the valve never consulted the sealability gate"
    assert _has_honest_marker(events), sts
    assert state.execution_status == ConversationStatus.FINISHED, sts
    assert not any(
        isinstance(e, MessageEvent) and e.meta.get("blocking") == "finish_seal_refused"
        for e in events
    ), sts
