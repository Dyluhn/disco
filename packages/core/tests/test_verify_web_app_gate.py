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
  (e) FAIL with CONSTANT fingerprint + a navigate in between → AWAITING_USER
      with verify_no_progress legacy detail; the navigate does NOT reset it
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
    assert_blocked_question_landing,
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
    url: str = "http://127.0.0.1:8000/",
) -> dict:
    return {
        "passed": passed,
        "verdict": "pass" if passed else verdict,
        "url": url,
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


class _FakeExecResult:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.exit_code = 0


class _FakePreviewSandbox:
    """Duck-typed sandbox whose `exec_shell` answers the gate's preview-port probe
    with a fixed listening port (so `_detect_preview_url` resolves to a known
    target_url). Records calls so the test can assert the probe ran."""

    def __init__(self, port: int):
        self.port = port
        self.exec_calls = 0

    async def exec_shell(self, cmd, *, timeout_s):  # noqa: ARG002 — signature parity
        self.exec_calls += 1
        return _FakeExecResult(f"{self.port}\n")


class SandboxedVerifyExecutor(VerifyExecutor):
    """VerifyExecutor that also exposes a `sandbox` so the finish gate can detect
    the CURRENT preview URL (P1-1 binding). Without this, `executor.sandbox` is
    None → binding disabled (the other tests' pre-binding behavior)."""

    def __init__(self, verdicts, *, preview_port=8000, has_verify=True):
        super().__init__(verdicts, has_verify=has_verify)
        self._sandbox = _FakePreviewSandbox(preview_port)

    @property
    def sandbox(self):
        return self._sandbox


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


def test_advisory_vision_field_never_blocks_finish():
    """H1 non-negotiable: the finish decision is keyed on the STRUCTURED verdict
    (verdict/passed) only. A passing host verdict with a failing/empty advisory
    `vision` field still yields a PASS finish label — the visual review can never
    flip a passing verdict to fail."""
    from disco.core.loop.finish.verify_gates import _HostVerifyGateMixin

    label = _HostVerifyGateMixin._verdict_label
    passing = {"verdict": "pass", "passed": True}
    assert label(passing) == "pass"
    # A failing advisory vision block does NOT change the finish label.
    with_failing_vision = {
        **passing,
        "vision": {"used": True, "passed": False, "notes": ["looks cramped"]},
    }
    assert label(with_failing_vision) == "pass"
    # An empty/unused vision block likewise leaves the pass intact.
    with_empty_vision = {**passing, "vision": {"used": False, "passed": None, "notes": []}}
    assert label(with_empty_vision) == "pass"


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
    # fingerprint, with no productive edit, must HALT and ask — not reload.
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
    assert_blocked_question_landing(events)
    terminal = [e for e in events if isinstance(e, StatusEvent)][-1]
    assert str(terminal.meta.get("legacy_detail", "")).startswith("verify_no_progress:")
    assert not any(s == "FINISHED" for s, _ in sts), sts
    # the cached verdict was reused — verify was driven once, not re-run on the 2nd finish
    assert execu.verify_calls == 1, (
        f"expected ONE driven verify (cache hit), got {execu.verify_calls}"
    )
    env = _env(events)
    assert any("same failure" in m.lower() for m in env), env


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


# ---- P1-1: verdict bound to the CURRENT preview url/port --------------------


def test_preview_key_normalizes_loopback_and_port():
    from disco.core.loop.finish import _preview_key

    assert _preview_key("http://127.0.0.1:8000/") == ("127.0.0.1", 8000)
    # localhost / 0.0.0.0 collapse to the same loopback host as 127.0.0.1
    assert _preview_key("http://localhost:8000/") == _preview_key("http://127.0.0.1:8000/")
    assert _preview_key("http://0.0.0.0:8000/") == ("127.0.0.1", 8000)
    # a different PORT is a different target
    assert _preview_key("http://127.0.0.1:3000/") != _preview_key("http://127.0.0.1:8000/")
    # empty / unparseable → None (can never bind to a target)
    assert _preview_key("") is None
    assert _preview_key(None) is None  # type: ignore[arg-type]


def test_latest_verify_verdict_binds_to_target_url():
    from disco.core.loop.finish import _latest_verify_verdict

    def obs(url, seq):
        res = ToolResult(
            call_id="c",
            tool_name="verify_web_app",
            success=True,
            content="v",
            structured={"url": url, "passed": True, "failure_fingerprint": "CLEAN"},
        )
        return ObservationEvent(tool_result=res, action_id="a").model_copy(update={"seq": seq})

    foreign = [obs("http://127.0.0.1:3000/", 15)]
    # target known + verdict url MISMATCHES → not accepted (gate must drive fresh)
    assert _latest_verify_verdict(foreign, 10, "http://127.0.0.1:8000/") is None
    # target known + verdict url MATCHES (localhost≡127.0.0.1) → accepted
    matching = [obs("http://localhost:8000/", 15)]
    v = _latest_verify_verdict(matching, 10, "http://127.0.0.1:8000/")
    assert v is not None and v["url"] == "http://localhost:8000/"
    # target None (undetectable) → binding disabled → accepted regardless of url
    assert _latest_verify_verdict(foreign, 10, None) is not None


@pytest.mark.asyncio
async def test_matching_url_pass_is_accepted_without_driving():
    # The agent verifies the LIVE preview (8000) itself with a PASS verdict, then
    # finishes. The gate detects the current preview (8000), the cached verdict
    # BINDS, and the run finishes WITHOUT the gate driving a fresh verify.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            action_step(tool="verify_web_app", args={}),
            finish_step(),
        ]
    )
    execu = SandboxedVerifyExecutor(
        [_verdict(passed=True, fp="CLEAN", url="http://127.0.0.1:8000/")],
        preview_port=8000,
    )
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    # exactly ONE verify call (the agent's) — the gate accepted the bound verdict
    # and did not drive its own probe.
    assert execu.verify_calls == 1, f"gate should not drive a fresh verify: {execu.verify_calls}"
    assert not any(isinstance(e, ActionEvent) and e.meta.get("verify_probe") for e in events), (
        "gate must not drive a verify_probe when a bound verdict exists"
    )
    assert ("FINISHED", None) in _statuses(events)


@pytest.mark.asyncio
async def test_foreign_url_pass_is_not_accepted_drives_fresh_verify():
    # The agent's cached PASS verdict is for a DIFFERENT port (3000) than the live
    # preview (8000). A stale/foreign PASS must NOT satisfy the finish gate — it
    # drives a FRESH verify against the real preview (which then passes on 8000).
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            action_step(tool="verify_web_app", args={}),
            finish_step(),
        ]
    )
    execu = SandboxedVerifyExecutor(
        [
            _verdict(passed=True, fp="CLEAN", url="http://127.0.0.1:3000/"),  # agent's (foreign)
            _verdict(passed=True, fp="CLEAN", url="http://127.0.0.1:8000/"),  # gate-driven (live)
        ],
        preview_port=8000,
    )
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    # TWO verify calls: the agent's foreign one was rejected, so the gate drove a
    # fresh verify against the real preview.
    assert execu.verify_calls == 2, (
        f"foreign-url verdict must not satisfy the gate; expected a driven verify: "
        f"{execu.verify_calls}"
    )
    assert any(isinstance(e, ActionEvent) and e.meta.get("verify_probe") for e in events), (
        "the gate must drive its own verify_probe when the cached verdict is foreign"
    )
    assert ("FINISHED", None) in _statuses(events)


# ---- P1-2: verifier failure must NOT cleanly finish ------------------------


class FailingVerifyExecutor(VerifyExecutor):
    """`verify_web_app` is advertised but ALWAYS fails to execute (success=False,
    no structured verdict) — the verifier-execution-error path. Drives the P1-2
    'no usable verdict' disposition."""

    async def execute(self, call):
        if call.tool_name == "verify_web_app":
            self.verify_calls += 1
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name="verify_web_app",
                success=False,
                content="",
                error="verify_web_app error: boom",
            )
        return await super().execute(call)


@pytest.mark.asyncio
async def test_verifier_failure_does_not_cleanly_finish():
    # verify_web_app is advertised but cannot produce a usable verdict. The gate
    # must NOT fall through to a clean FINISH (W-32): it refuses-and-continues with
    # a "verification could not run" reminder, then releases EXPLICITLY as
    # unverified at the cap — never a silent clean done.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            finish_step(),  # ScriptedAgent repeats finish on each CONTINUE
        ]
    )
    execu = FailingVerifyExecutor([_verdict(passed=False, fp="X")])
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    env = _env(events)
    sts = _statuses(events)
    reminders = [m for m in env if "verification could not run" in m]
    # the gate continued (did not clean-finish) at least once
    assert reminders, f"expected a 'verification could not run' reminder: {env}"
    # any terminal FINISHED must be accompanied by the explicit unverified marker —
    # never a plain clean finish for a build that never verified.
    assert any(d == "unverified_release" for _, d in sts), sts
    assert any("UNVERIFIED" in m for m in env), env


# ---- P1-3: the 3-refusal release is an EXPLICIT incomplete outcome ----------


@pytest.mark.asyncio
async def test_fail_release_emits_explicit_unverified_marker():
    # A FAIL verdict with a CHANGING fingerprint across real edits exhausts the
    # 3-refusal cap and releases. The release must carry a DISTINCT terminal signal
    # (StatusEvent detail="unverified_release") so the run cannot present as a clean
    # verified FINISHED — the agent's pre-gate summary text is not relied upon.
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
    sts = _statuses(events)
    # the distinct, queryable incomplete signal precedes the terminal FINISHED
    assert any(d == "unverified_release" for _, d in sts), sts
    assert ("FINISHED", None) in sts  # still bounded — releases, doesn't hang
    env = _env(events)
    assert any("INCOMPLETE" in m for m in env), env


# ---- Bug 7 + Bug 6: process backend — the gate targets the conversation's served
#      port (never the agent-server's 8000), and a delivered-but-unverifiable build
#      FINISHES instead of pausing/STUCKing --------------------------------------


class _ProcessGateSandbox:
    """A process-backend-shaped sandbox for the finish gate: `workspace_path` is set
    (Bug 7's shared-host signal), `exec_shell` answers the ownership probe (8000 owned
    by a NON-conversation agent-server, 8080 owned — or not — by THIS conversation),
    and `file_exists` reports the static deliverable."""

    conversation_id = "conv"  # cid8 == "conv" → session prefix disco-conv-
    workspace_path = "/tmp/sbx-proc"

    def __init__(self, *, conv_owns_8080=True, index_exists=True):
        self._conv_owns_8080 = conv_owns_8080
        self._index_exists = index_exists
        self.exec_calls = 0

    async def exec_shell(self, cmd, *, timeout_s):  # noqa: ARG002 — signature parity
        self.exec_calls += 1
        eighty80 = (
            '{"port": 8080, "pid": 9, "session": "disco-conv-preview"}'
            if self._conv_owns_8080
            else '{"port": 8080, "pid": null, "session": null}'
        )
        return _FakeExecResult(
            '[{"port": 8000, "pid": 7, "session": "disco-other777-preview"},'
            f" {eighty80},"
            ' {"port": 5173, "pid": null, "session": null},'
            ' {"port": 3000, "pid": null, "session": null},'
            ' {"port": 5000, "pid": null, "session": null},'
            ' {"port": 4321, "pid": null, "session": null}]'
        )

    async def file_exists(self, path):
        return self._index_exists and path in ("index.html", "./index.html")


class _ProcessVerifyExecutor(VerifyExecutor):
    """VerifyExecutor on a PROCESS-shaped sandbox. `has_browser=False` models the
    browserless process backend (no `browser` tool) for the Bug 6 honest-finish."""

    def __init__(self, verdicts, *, sandbox, has_verify=True, has_browser=True):
        super().__init__(verdicts, has_verify=has_verify)
        if not has_browser:
            self._tools = [t for t in self._tools if t.name != "browser"]
        self._sandbox = sandbox

    @property
    def sandbox(self):
        return self._sandbox


def _not_serving_verdict(url="http://127.0.0.1:8080/"):
    """A 'not serving' fail: server unreachable, browser never ran (no console/
    network errors) — the UNVERIFIABLE-infra shape, NOT a broken app."""
    return {
        "passed": False,
        "verdict": "fail",
        "url": url,
        "http_status": 0,
        "title": "",
        "meaningful_content": False,
        "visible_text_chars": 0,
        "elements_count": 0,
        "console_errors": [],
        "console_warnings": [],
        "network_failures": [],
        "screenshot_path": "",
        "vision": {"used": False, "passed": None, "notes": []},
        "failure_fingerprint": "CLEAN",
        "summary": "App not serving: http://127.0.0.1:8080/ returned no response.",
        "next_action": "Start the dev server on the preview port.",
    }


@pytest.mark.asyncio
async def test_process_gate_drives_verify_against_conversation_port_not_8000():
    # Bug 7: the gate detects the conversation's served port (8080) — never the
    # agent-server's 8000 — and DRIVES verify_web_app with that explicit url.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            finish_step(),  # finish WITHOUT a fresh verdict → the gate drives one
        ]
    )
    sbx = _ProcessGateSandbox(conv_owns_8080=True)
    execu = _ProcessVerifyExecutor(
        [_verdict(passed=True, fp="CLEAN", url="http://127.0.0.1:8080/")], sandbox=sbx
    )
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    driven = [c for c in execu.calls if c.tool_name == "verify_web_app"]
    assert driven, "the gate must drive a verify when the agent finishes without one"
    # the driven verify targets 8080 (the conversation's port), NEVER 8000.
    assert driven[-1].arguments == {"url": "http://127.0.0.1:8080/"}, driven[-1].arguments
    assert "8000" not in str(driven[-1].arguments)
    assert ("FINISHED", None) in _statuses(events)


@pytest.mark.asyncio
async def test_process_browser_unavailable_static_build_finishes_not_stuck():
    # Bug 6: a complete static build whose preview is NOT reachable AND whose backend
    # cannot run a headless browser (no `browser` tool) FINISHES honestly-unverifiable
    # — index.html exists — instead of pausing/STUCKing.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "<h1>x</h1>"}),
            finish_step(),
        ]
    )
    sbx = _ProcessGateSandbox(conv_owns_8080=False, index_exists=True)
    execu = _ProcessVerifyExecutor([_not_serving_verdict()], sandbox=sbx, has_browser=False)
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    events = await store.get_events("conv")
    sts = _statuses(events)
    # the honest-unverifiable marker precedes a clean FINISHED — never STUCK/PAUSED.
    assert any(d == "unverifiable_static_finish" for _, d in sts), sts
    assert ("FINISHED", None) in sts
    assert not any(s in ("STUCK", "PAUSED") for s, _ in sts), sts
    # gate None-path: the resolver returned None (no conversation-owned port), so the
    # gate drove a fresh verify with {} (auto-detect) — never a guessed url — and the
    # not-serving verdict routed to the honest path. No crash, no spurious PASS verdict.
    driven = [c for c in execu.calls if c.tool_name == "verify_web_app"]
    assert driven and driven[-1].arguments == {}, driven
    assert not any(
        isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "verify_web_app"
        and (e.tool_result.structured or {}).get("passed") is True
        for e in events
    ), "the None-path must never accept a passing verdict against a guessed port"


@pytest.mark.asyncio
async def test_process_browser_unavailable_but_no_deliverable_does_not_finish():
    # Negative: same browserless not-serving case but NO index.html on disk → the
    # honest finish must NOT fire (a missing deliverable is not an unverifiable one).
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "x"}),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    sbx = _ProcessGateSandbox(conv_owns_8080=False, index_exists=False)
    execu = _ProcessVerifyExecutor([_not_serving_verdict()], sandbox=sbx, has_browser=False)
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    sts = _statuses(await store.get_events("conv"))
    assert not any(d == "unverifiable_static_finish" for _, d in sts), sts


@pytest.mark.asyncio
async def test_process_real_console_error_does_not_honest_finish():
    # Negative (W-45 preserved): a REAL fail (console error — the browser DID run)
    # must never be converted to an honest-unverifiable finish, even browserless.
    agent = ScriptedAgent(
        [
            action_step(tool="file_write", args={"path": "index.html", "content": "x"}),
            finish_step(),
            finish_step(),
            finish_step(),
            finish_step(),
        ]
    )
    sbx = _ProcessGateSandbox(conv_owns_8080=True, index_exists=True)
    # reachable + a real console error → verdict "fail" with console_errors set.
    broken = _verdict(passed=False, fp="BUG", url="http://127.0.0.1:8080/")
    execu = _ProcessVerifyExecutor([broken], sandbox=sbx, has_browser=False)
    loop, store = _gate_loop(agent, execu)
    await loop.send_message("build me a page")
    await loop.run()

    sts = _statuses(await store.get_events("conv"))
    assert not any(d == "unverifiable_static_finish" for _, d in sts), sts
