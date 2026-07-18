"""Verify-on-finish — the post-condition gate (Claude-Code 'run the tests before
you claim done'). When the agent attaches a `verify` shell command to `finish`,
the loop RUNS it and refuses the finish unless it passes. The check is not
privileged: it passes the same hard-deny gate and confirmation policy as any
action.
"""

from __future__ import annotations

import hashlib

import pytest
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    SecurityRisk,
    ToolCall,
    ToolResult,
)
from disco.core.llm import DefaultLLMRouter, ProposedToolCall
from disco.core.loop import AgentStep, ConfirmRisky, NeverConfirm, RouterAgent
from disco.core.loop.control import Disp
from llm_fakes import simple_config
from loop_fakes import FakeAnalyzer, FakeExecutor, SequenceProvider, build_loop

CID = "conv"


def _agent(scripted):
    provider = SequenceProvider(scripted)
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    return RouterAgent(router, conversation_id=CID)


def _finish(verify=None, summary="done"):
    args = {"summary": summary}
    if verify is not None:
        args["verify"] = verify
    return {"tool_calls": [ProposedToolCall(tool_name="finish", arguments=args)]}


async def test_verify_passes_then_run_finishes_and_check_is_in_the_trace():
    agent = _agent([_finish(verify="pytest -q")])
    executor = FakeExecutor()  # default: every execute() succeeds
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("ship it")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    # The verify command actually ran, through the executor, as a shell action.
    assert len(executor.calls) == 1
    assert executor.calls[0].tool_name == "shell"
    assert executor.calls[0].arguments == {"command": "pytest -q"}
    # It's visible in the trace: a verify ActionEvent + its successful Observation.
    events = await store.get_events(CID)
    verify_actions = [
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "shell"
    ]
    assert len(verify_actions) == 1
    assert "Verifying completion" in verify_actions[0].thought
    assert any(isinstance(e, ObservationEvent) for e in events)


async def test_verify_fails_then_finish_is_refused_and_loop_continues():
    # Call 1: finish with a verify that FAILS → refused, loop keeps going.
    # Call 2: finish WITHOUT verify → the run finishes.
    agent = _agent([_finish(verify="pytest -q"), _finish(verify=None)])
    failing = ToolResult(
        call_id="c", tool_name="shell", success=False, content="2 failed", error="exit 1"
    )
    executor = FakeExecutor(result=failing)
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("ship it")
    state = await loop.run()

    # It ultimately finished (via the second, verify-less finish)...
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    # ...but the failed verify was recorded as an error (the agent saw it)...
    assert any(isinstance(e, AgentErrorEvent) for e in events)
    # ...and there is exactly ONE FINISHED status (the first finish did NOT land).
    finishes = [e for e in events if getattr(e, "status", None) == ConversationStatus.FINISHED]
    assert len(finishes) == 1


async def test_hard_denied_verify_is_refused_without_running():
    # A catastrophic verify command is refused BEFORE execution.
    agent = _agent([_finish(verify="rm -rf /"), _finish(verify=None)])
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    # The dangerous command NEVER reached the executor.
    assert executor.calls == []
    events = await store.get_events(CID)
    assert any(isinstance(e, AgentErrorEvent) and "hard-denied" in e.error for e in events)


async def test_verify_needing_confirmation_is_refused_not_silently_run():
    # Under ConfirmRisky, a verify command the analyzer flags HIGH must NOT be
    # silently auto-run as a 'verification' — it's refused with guidance to run
    # it as a normal, gated action first.
    agent = _agent([_finish(verify="deploy --prod"), _finish(verify=None)])
    executor = FakeExecutor()
    loop, store = build_loop(
        agent,
        executor=executor,
        analyzer=FakeAnalyzer(SecurityRisk.HIGH),  # everything assesses HIGH
        policy=ConfirmRisky(),  # gates HIGH
    )

    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert executor.calls == []  # never silently executed
    events = await store.get_events(CID)
    assert any(isinstance(e, AgentErrorEvent) and "needs confirmation" in e.error for e in events)


async def test_finish_without_verify_is_unchanged():
    # Back-compat: no verify → finishes immediately, no shell action.
    agent = _agent([_finish(verify=None)])
    executor = FakeExecutor()
    loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    assert executor.calls == []


# ---- E4: static-site verify (no server) -------------------------------------


async def test_static_verify_translates_to_a_serverfree_html_check():
    """`verify="static"` runs a files-present + HTML-parses check, NOT a server
    curl — the honest post-condition for a static page build."""
    agent = _agent([_finish(verify="static")])
    executor = FakeExecutor()
    loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())
    await loop.send_message("ship the page")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    assert len(executor.calls) == 1
    cmd = executor.calls[0].arguments["command"]
    # translated to a server-free python3 HTML check over index.html
    assert cmd.startswith("python3 -c")
    assert "html.parser" in cmd and "index.html" in cmd
    assert "curl" not in cmd


async def test_static_verify_honours_a_custom_path():
    agent = _agent([_finish(verify="static:about.html")])
    executor = FakeExecutor()
    loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())
    await loop.send_message("ship about")
    await loop.run()
    assert "about.html" in executor.calls[0].arguments["command"]


async def test_app_verify_targets_the_detected_preview_port_not_8000():
    """`verify="app"` runs a SERVER-AWARE check (GET the app, require HTTP 200 +
    non-empty body) — the verify_app gap. Crucially it targets the ACTUAL platform-
    assigned preview port the live deliverable serves on, NEVER a fixed :8000 (the
    platform chooses the port; there is no auto-served :8000 inside the sandbox)."""
    agent = _agent([_finish(verify="app")])
    executor = FakeExecutor()
    loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())

    # The live preview is detected on a platform-assigned port (the same backend-aware
    # detection the verify_web_app gate uses); the app-check must bind to THAT port.
    async def _fake_detect() -> str:
        return "http://127.0.0.1:54321/"

    loop._finish._detect_preview_url = _fake_detect  # type: ignore[assignment]

    await loop.send_message("ship the app")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    cmd = executor.calls[0].arguments["command"]
    assert cmd.startswith("python3 -c")
    assert "urllib.request" in cmd  # it actually fetches the URL
    assert "127.0.0.1:54321" in cmd and "code==200" in cmd  # the DETECTED port
    assert "8000" not in cmd  # never the dead fixed port
    assert "html.parser" not in cmd  # not the static check


async def test_app_verify_without_detectable_preview_degrades_to_static_not_8000():
    """A bare `verify="app"` no longer assumes a fixed :8000 — the platform assigns the
    preview port. With no live preview detectable, the gate degrades to the server-free
    static check rather than asserting :8000 (a 404 there would be a FALSE failure that
    spins the model into re-serve/re-verify loops)."""
    agent = _agent([_finish(verify="app")])
    executor = FakeExecutor()  # no sandbox → no preview detectable
    loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())
    await loop.send_message("ship the app")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    cmd = executor.calls[0].arguments["command"]
    assert cmd.startswith("python3 -c")
    assert "8000" not in cmd  # never a guessed port
    assert "html.parser" in cmd  # degraded to the static (server-free) check


async def test_app_verify_honours_a_custom_url():
    agent = _agent([_finish(verify="app:http://localhost:3000/health")])
    executor = FakeExecutor()
    loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())
    await loop.send_message("ship the api")
    await loop.run()
    assert "localhost:3000/health" in executor.calls[0].arguments["command"]


# ---- E4a: static:<non-html> validation ---------------------------------------


async def test_static_verify_with_non_html_path_is_ignored_with_reminder():
    """`verify="static:primes.py"` reaches FINISHED, executes no HTML probe,
    and records an explicit ignored-directive environment message with
    stable metadata."""
    agent = _agent([_finish(verify="static:primes.py")])
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    # No shell command executed (the optional verifier was omitted).
    assert executor.calls == []
    # The environment reminder with stable metadata was emitted.
    events = await store.get_events(CID)
    ignored_reminders = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("finish_verify_ignored") is True
    ]
    assert len(ignored_reminders) == 1
    rem = ignored_reminders[0]
    assert "static:primes.py" in rem.message.content
    assert "ignored" in rem.message.content.lower()
    assert rem.meta.get("verify_kind") == "static"
    assert rem.meta.get("directive") == "static:primes.py"


async def test_static_verify_with_mixed_case_html_extensions():
    """Mixed-case `.HTML` and `.HTM` custom paths still translate to the
    HTML parser (case-insensitive extension check)."""
    for ext in (".HTML", ".HTM", ".Html", ".Htm"):
        agent = _agent([_finish(verify=f"static:about{ext}")])
        executor = FakeExecutor()
        loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())

        await loop.send_message(f"ship {ext}")
        await loop.run()

        assert len(executor.calls) == 1
        cmd = executor.calls[0].arguments["command"]
        assert cmd.startswith("python3 -c")
        assert f"about{ext}" in cmd


async def test_raw_shell_verify_with_non_html_filename_still_executes():
    """An ordinary raw shell verifier that mentions a non-HTML filename
    (e.g. ``pytest primes.py``) executes unchanged — only the ``static:``
    prefix triggers the extension validation."""
    agent = _agent([_finish(verify="pytest primes.py")])
    executor = FakeExecutor()
    loop, _ = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(executor.calls) == 1
    assert executor.calls[0].arguments["command"] == "pytest primes.py"


async def test_ignored_static_directive_does_not_bypass_external_dod():
    """Prove that the ignored ``static:primes.py`` directive does NOT bypass
    the external DoD gate.  ``normalize_finish_step`` skips the optional
    verifier but the DoD gate still runs and refuses the finish."""
    from disco.core.dod import DoDSpec, FileExistsPredicate
    from disco.core.dod_evaluator import DoDPredicateResult, DoDVerdict

    class _FixedEvaluator:
        """A fake DoD evaluator that always fails its predicate."""

        async def evaluate(self, spec, *, conversation_id):  # noqa: ARG002
            pred = FileExistsPredicate(path="out.txt")
            result = DoDPredicateResult(predicate=pred, passed=False, reason="out.txt is missing")
            return DoDVerdict(
                passed=False,
                unmet=[pred],
                results=[result],
                spec_predicate_count=1,
                evaluated_at="2026-06-13T00:00:00Z",
                spec_fingerprint="fp-test",
            )

    agent = _agent([_finish(verify="static:primes.py")])
    executor = FakeExecutor()
    loop, store = build_loop(
        agent,
        executor=executor,
        policy=NeverConfirm(),
        dod_evaluator_factory=lambda: _FixedEvaluator(),
    )
    await store.set_dod_spec(CID, DoDSpec(predicates=[FileExistsPredicate(path="out.txt")]))

    # Drive normalize_finish_step directly (same path the loop takes).
    step = AgentStep(
        finished=False,
        tool_call=ToolCall(
            tool_name="finish",
            arguments={"summary": "done", "verify": "static:primes.py"},
        ),
    )
    events_before = await store.get_events(CID)
    step_out, _ = await loop._finish.normalize_finish_step(step, events_before)

    # The DoD gate refused the finish (step is NOT marked finished).
    assert not step_out.finished
    # No shell command was executed (the verify was ignored, not run).
    assert executor.calls == []
    # The ignored-directive reminder was emitted.
    events = await store.get_events(CID)
    ignored = [
        e
        for e in events
        if isinstance(e, MessageEvent) and e.meta.get("finish_verify_ignored") is True
    ]
    assert len(ignored) == 1
    assert ignored[0].meta.get("verify_kind") == "static"


def test_app_verify_command_distinguishes_serving_from_not(tmp_path):
    """The generated command genuinely passes against a live server and fails when
    nothing serves — run it as a real subprocess against a threaded http.server."""
    import http.server
    import socket
    import subprocess
    import threading

    from disco.core.loop.engine import _app_verify_command

    # a real one-request server on an ephemeral port, serving a non-trivial body
    try:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
    except PermissionError as exc:
        pytest.skip(f"local socket binding unavailable in this sandbox: {exc}")

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html><body>hello from the app</body></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        ok = subprocess.run(
            _app_verify_command(f"http://127.0.0.1:{port}/"), shell=True, capture_output=True
        )
        assert ok.returncode == 0, ok.stderr.decode()
        assert b"OK" in ok.stdout
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
        assert not t.is_alive()

    # nothing serving on that port now → the verify fails (nonzero)
    bad = subprocess.run(
        _app_verify_command(f"http://127.0.0.1:{port}/"), shell=True, capture_output=True
    )
    assert bad.returncode != 0


# ---- H567: verify receipt reuse ----------------------------------------------


async def test_h567_reuse_exact_latest_paired_shell_success():
    """H567 Product rule 1 — the most recent ActionEvent of any tool is a
    matching shell command with a correlated successful ObservationEvent,
    so the receipt is reused without re-execution."""
    agent = _agent([_finish(verify=None)])
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    # Emit a shell ActionEvent with the command we will verify against.
    call = ToolCall(tool_name="shell", arguments={"command": "pytest -q"}, call_id="c1")
    action = ActionEvent(thought="run tests", tool_call=call)
    await loop._emit(action)

    # Emit its correlated successful ObservationEvent.
    await loop._emit(
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="ok"),
        )
    )

    # Call normalize_finish_step with the same verify command.
    step = AgentStep(
        finished=False,
        tool_call=ToolCall(
            tool_name="finish",
            arguments={"summary": "done", "verify": "pytest -q"},
        ),
    )
    events = await store.get_events(CID)
    step_out, disp = await loop._finish.normalize_finish_step(step, events)

    # Reuse succeeded — step finished, no re-execution.
    assert step_out.finished
    assert executor.calls == []
    assert disp is Disp.FALLTHROUGH

    # Audit marker was emitted with correct fields.
    after = await store.get_events(CID)
    markers = [
        e
        for e in after
        if isinstance(e, MessageEvent) and e.meta.get("finish_verify_receipt_reused") is True
    ]
    assert len(markers) == 1
    m = markers[0]
    assert m.meta["prior_action_id"] == action.id
    assert "command_fingerprint_sha256" in m.meta
    assert m.meta["command_fingerprint_sha256"] == hashlib.sha256(b"pytest -q").hexdigest()
    # No raw command metadata in the marker.
    assert "command" not in m.meta


@pytest.mark.parametrize(
    "scenario",
    [
        "failed_observation",
        "unpaired",
        "mismatched_command",
        "mismatched_call_id",
        "observation_before_action",
        "intervening_later_action",
    ],
)
async def test_h567_reuse_rejected(scenario: str):
    """Various non-qualifying evidence patterns must NOT trigger receipt reuse,
    forcing real shell execution of the verify command."""
    agent = _agent([_finish(verify=None)])
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    matching_call = ToolCall(tool_name="shell", arguments={"command": "pytest -q"}, call_id="c1")

    if scenario == "failed_observation":
        # Observation has success=False → must not reuse.
        action = ActionEvent(thought="run tests", tool_call=matching_call)
        await loop._emit(action)
        await loop._emit(
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(
                    call_id="c1", tool_name="shell", success=False, content="FAILED"
                ),
            )
        )

    elif scenario == "unpaired":
        # Shell action with no ObservationEvent at all → must not reuse.
        await loop._emit(ActionEvent(thought="run tests", tool_call=matching_call))

    elif scenario == "mismatched_command":
        # Shell action has a different command → must not reuse.
        different_call = ToolCall(
            tool_name="shell", arguments={"command": "make test"}, call_id="c1"
        )
        action = ActionEvent(thought="run tests", tool_call=different_call)
        await loop._emit(action)
        await loop._emit(
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="ok"),
            )
        )

    elif scenario == "mismatched_call_id":
        action = ActionEvent(thought="run tests", tool_call=matching_call)
        await loop._emit(action)
        await loop._emit(
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(
                    call_id="wrong-call", tool_name="shell", success=True, content="ok"
                ),
            )
        )

    elif scenario == "observation_before_action":
        action = ActionEvent(thought="run tests", tool_call=matching_call)
        await loop._emit(
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="ok"),
            )
        )
        await loop._emit(action)

    elif scenario == "intervening_later_action":
        # Matching shell + observation, then a LATER non-shell ActionEvent.
        action = ActionEvent(thought="run tests", tool_call=matching_call)
        await loop._emit(action)
        await loop._emit(
            ObservationEvent(
                action_id=action.id,
                tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="ok"),
            )
        )
        # A later file_write action means the shell action is no longer latest.
        later = ToolCall(tool_name="file_write", arguments={"path": "extra.txt"}, call_id="c2")
        await loop._emit(ActionEvent(thought="write extra", tool_call=later))

    # Call normalize_finish_step with the verify command.
    step = AgentStep(
        finished=False,
        tool_call=ToolCall(
            tool_name="finish",
            arguments={"summary": "done", "verify": "pytest -q"},
        ),
    )
    events = await store.get_events(CID)
    await loop._finish.normalize_finish_step(step, events)

    # Reuse must NOT have happened — the executor ran the verify command
    # (FakeExecutor returns success by default, so the verify passes and
    # the step would finish, but the key check is that execute() was called).
    assert len(executor.calls) == 1
    assert executor.calls[0].arguments["command"] == "pytest -q"


async def test_h567_reused_optional_receipt_cannot_bypass_external_dod(tmp_path):
    """The model-owned receipt is only an optimization; immutable external
    acceptance still runs after it and refuses an unmet deliverable."""
    from disco.core.dod import DoDSpec, FileExistsPredicate
    from disco.core.dod_evaluator import DoDEvaluator

    executor = FakeExecutor()
    loop, store = build_loop(
        _agent([]),
        executor=executor,
        policy=NeverConfirm(),
        dod_evaluator_factory=lambda: DoDEvaluator(tmp_path),
    )
    await store.set_dod_spec(
        CID,
        DoDSpec(predicates=[FileExistsPredicate(path="harness-required.txt")]),
        set_by="harness",
    )
    call = ToolCall(tool_name="shell", arguments={"command": "pytest -q"}, call_id="c1")
    action = ActionEvent(thought="run tests", tool_call=call)
    await loop._emit(action)
    await loop._emit(
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(call_id="c1", tool_name="shell", success=True, content="ok"),
        )
    )

    normalized, disp = await loop._finish.normalize_finish_step(
        AgentStep(
            tool_call=ToolCall(
                tool_name="finish",
                arguments={"summary": "done", "verify": "pytest -q"},
            )
        ),
        await store.get_events(CID),
    )

    assert disp is Disp.CONTINUE
    assert normalized.finished is False
    assert executor.calls == []
    events = await store.get_events(CID)
    assert any(
        isinstance(event, MessageEvent) and event.meta.get("finish_verify_receipt_reused") is True
        for event in events
    )
    assert not any(
        getattr(event, "status", None) == ConversationStatus.FINISHED for event in events
    )
