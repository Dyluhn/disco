"""C18 — per-step `done_condition` advisory predicate.

# Contract

`PlanStepInput` gains an OPTIONAL `done_condition` predicate that reuses the
`disco.core.dod.DoDPredicate` discriminated union (file_exists / command /
http_ok). When the agent later marks the step done via
`plan_step(idx, 'done')`, the engine evaluates the predicate and emits a
visible pass/fail note in the trace. ADVISORY ONLY:

  * never blocks the run;
  * never nudges the agent (no <system-reminder>, no auto-continue, no
    speak-back to the model);
  * never duplicates the C1c finish gate (the C18 check uses a lightweight
    inline evaluator; C1c uses the heavy fresh-context DoDEvaluator and
    gates `finish` itself).

Steps WITHOUT a predicate behave exactly as today (back-compat): no
lookup, no note, no extra event. The check fires only on `state="done"`,
never on `state="active"`.

# Acceptance (this file)

  1. Step with a SATISFIED predicate → the run completes cleanly AND a
     `met` advisory note is emitted in the trace.
  2. Step with an UNSATISFIED predicate → a `NOT met` advisory note is
     emitted (visible — no silent pass).
  3. Step WITHOUT a predicate → no advisory note, no extra events;
     behavior is byte-identical to the pre-C18 path.

Tests use only fakes (no real model, no real container) — same pattern as
`test_c5_pmx_memory_persistence.py` and `test_c16_hard_reset_pointer_flush.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as _dc_field

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step

CID = "conv-c18"
_H342_PROCESS_SUBSTITUTION_PREDICATE = (
    'python3 -c "import sys; lines=sys.stdin.read().split(); '
    "assert len(lines)==20, f'expected 20 primes, got {len(lines)}'; "
    "assert lines[-1]=='71', f'last prime was {lines[-1]}, expected 71'; "
    "print('VERIFIED: 20 primes, ending with 71')\" < <(python3 primes.py)"
)


# ---------------------------------------------------------------------------
# Helper fakes — a sandbox-backed executor that lets the test plant a
# workspace and inject a successful `execute()` for every tool call (so
# plan_step completes and the post-execute C18 hook runs).
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """Minimal sandbox surface the C18 evaluator needs: `workspace_path` for
    `file_exists` path resolution. The list of files in the workspace is
    what the test plants up front, so the predicate's outcome is
    deterministic."""

    def __init__(self, workspace_path: str, files: dict[str, bytes] | None = None) -> None:
        self.workspace_path = workspace_path
        self._files: dict[str, bytes] = dict(files or {})
        self.file_exists_calls: list[str] = []

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = bytes(data)

    async def file_exists(self, path: str) -> bool:
        """B4 — mirrors the real process/session backend: resolve against the
        host-side `workspace_path` (this fake plants real files there) and check
        the FS. The C18 check now calls THIS instead of a literal host path."""
        from pathlib import Path

        self.file_exists_calls.append(path)
        return (Path(self.workspace_path) / path).exists()


class _SandboxExecutor:
    """ToolExecutor stub: any `execute()` returns success (so plan_step's
    in-process tool completes), and a `sandbox` attribute is exposed so the
    C18 evaluator can resolve `file_exists` paths against the workspace.

    `available_tools()` offers `shell` so the Rung-7 unknown-tool requery
    doesn't trip on tool names the agent uses between `submit_plan` and
    `plan_step`."""

    def __init__(self, sandbox: _FakeSandbox) -> None:
        self.sandbox = sandbox
        self.execute_calls: list[ToolCall] = []

    def available_tools(self):
        from disco.core.llm import ToolSpec

        return [
            ToolSpec(name="shell", description="run a shell command", parameters_schema={}),
            ToolSpec(name="plan_step", description="mark plan step progress", parameters_schema={}),
            ToolSpec(
                name="update_plan_progress",
                description="declarative full-state plan progress",
                parameters_schema={},
            ),
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        self.execute_calls.append(call)
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


def _advisory_notes(events: list) -> list[MessageEvent]:
    """Pull the C18 advisory notes out of the event log. They are
    `MessageEvent(source=ENVIRONMENT, meta={"advisory": "plan_step_done_condition", ...})`
    — NOT wrapped in <system-reminder> (so they are visible but
    non-nudging)."""
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and isinstance(e.meta, dict)
        and e.meta.get("advisory") == "plan_step_done_condition"
    ]


def _has_no_advisory_note(events: list) -> bool:
    """Back-compat assertion helper: the trace contains zero C18 notes."""
    return _advisory_notes(events) == []


# ---------------------------------------------------------------------------
# Test 1 — step with a SATISFIED predicate → clean done + visible "met" note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c18_satisfied_predicate_emits_met_note(tmp_path):
    """A plan step whose `done_condition` (file_exists) is satisfied at the
    moment the agent marks it done → the C18 advisory note says `met`. The
    run completes cleanly; the note is visible in the trace."""
    # Plant a file the predicate will look for.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "artifact.txt").write_text("ok")
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            # submit_plan: one step with a satisfied file_exists predicate
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "artifact.txt",
                            },
                        }
                    ],
                },
            ),
            # A real productive action so the run executes the plan (else the
            # approve→no-execution gate terminalizes STUCK before finish).
            action_step("shell", {"command": "echo done"}),
            # plan_step(1, 'done') — the C18 hook evaluates the predicate
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    # The run landed FINISHED (the single step was marked done; finish was
    # accepted because the plan is complete).
    final_status = next(
        (e for e in reversed(events) if isinstance(e, StatusEvent)),
        None,
    )
    assert final_status is not None
    assert getattr(final_status, "status", None) == ConversationStatus.FINISHED
    # The C18 advisory note is present AND says "met".
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    assert "done-condition" in note.message.content
    assert "met" in note.message.content
    assert "NOT met" not in note.message.content
    assert note.meta.get("passed") is True
    # And the note is NOT wrapped in <system-reminder> (no nudge).
    assert "<system-reminder>" not in note.message.content


@pytest.mark.parametrize("guest_path", ["workspace/artifact.txt", "/workspace/artifact.txt"])
@pytest.mark.asyncio
async def test_c18_workspace_prefixed_file_exists_uses_process_jail(tmp_path, guest_path):
    """Guest /workspace spellings resolve inside the known host workspace.

    pathlib must not interpret the absolute guest spelling as a host absolute
    path, while genuine ../ and /etc escapes remain covered by the negative test.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "artifact.txt").write_text("ok")
    sbx = _FakeSandbox(str(workspace))
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {"kind": "file_exists", "path": guest_path},
                        }
                    ],
                },
            ),
            action_step("shell", {"command": "echo done"}),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    notes = _advisory_notes(await store.get_events(CID))
    assert len(notes) == 1
    assert "met" in notes[0].message.content
    assert "NOT met" not in notes[0].message.content
    assert notes[0].meta.get("passed") is True


# ---------------------------------------------------------------------------
# Test 2 — step with an UNSATISFIED predicate → visible "NOT met" note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c18_unsatisfied_predicate_emits_not_met_note(tmp_path):
    """A plan step whose `done_condition` is NOT satisfied at the moment
    the agent marks it done → the C18 advisory note says `NOT met`. The
    note is visible (no silent pass) AND the agent is NOT nudged (no
    <system-reminder>, no auto-continue, no speak-back to the model)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # No artifact.txt planted — the predicate will fail.
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "artifact.txt",
                            },
                        }
                    ],
                },
            ),
            action_step("shell", {"command": "echo done"}),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    # The note is visible: a C18 advisory note saying "NOT met".
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    assert "done-condition" in note.message.content
    assert "NOT met" in note.message.content
    assert note.meta.get("passed") is False
    # And the note is NOT wrapped in <system-reminder> (no nudge, no
    # speak-back to the model — the run proceeds as if the step were
    # done, which is exactly the C18 "advisory, not a gate" contract).
    assert "<system-reminder>" not in note.message.content
    # The run still lands FINISHED (the plan is complete; the C18 note
    # is advisory only and does not duplicate the C1c finish gate).
    final_status = next(
        (e for e in reversed(events) if isinstance(e, StatusEvent)),
        None,
    )
    assert final_status is not None
    assert getattr(final_status, "status", None) == ConversationStatus.FINISHED


# ---------------------------------------------------------------------------
# Test 3 — step WITHOUT a predicate → no note, behavior unchanged (back-compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c18_no_predicate_emits_no_note(tmp_path):
    """A plan step with NO `done_condition` → no C18 advisory note, no
    extra event. This is the explicit back-compat target: today's
    plan_step flow is silent, and it stays silent when no predicate is
    attached."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            # submit_plan: a step with NO done_condition field
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {"title": "do the thing"},
                    ],
                },
            ),
            action_step("shell", {"command": "echo done"}),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    # Zero C18 advisory notes — back-compat preserved.
    assert _has_no_advisory_note(events), (
        f"expected no C18 notes for a no-predicate step, got "
        f"{[n.message.content for n in _advisory_notes(events)]}"
    )
    # The run still lands FINISHED (plan complete; finish accepted).
    final_status = next(
        (e for e in reversed(events) if isinstance(e, StatusEvent)),
        None,
    )
    assert final_status is not None
    assert getattr(final_status, "status", None) == ConversationStatus.FINISHED
    # The plan_step ActionEvent + its ObservationEvent are the only
    # trace artifacts for the mark-done — exactly as today.
    plan_step_actions = [
        e
        for e in events
        if isinstance(e, ActionEvent) and e.tool_call and e.tool_call.tool_name == "plan_step"
    ]
    assert len(plan_step_actions) == 1
    plan_step_observations = [
        e
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result is not None
        and e.tool_result.tool_name == "plan_step"
    ]
    assert len(plan_step_observations) == 1


# ---------------------------------------------------------------------------
# B4 — container backend: workspace_path is None, yet the files are REAL inside
# the box. The old code fell through to a literal host Path() check (the
# agent-server cwd), where the container's files don't exist, and emitted a
# FALSE "NOT met / missing" advisory. The fix asks the sandbox via file_exists.
# ---------------------------------------------------------------------------


class _FakeContainerSandbox:
    """A CONTAINER-backend sandbox surface: `workspace_path` is None (the host
    has no view of the box FS), and existence is answered by `file_exists`
    (which, for a real container, execs `test -f` INSIDE the box). The test
    controls the in-box file set explicitly so the predicate outcome is
    deterministic — independent of the agent-server's host cwd."""

    workspace_path = None

    def __init__(self, present: set[str] | None = None) -> None:
        self._present: set[str] = set(present or ())
        self.file_exists_calls: list[str] = []

    async def read_file(self, path: str) -> bytes:
        if path not in self._present:
            raise FileNotFoundError(path)
        return b""

    async def write_file(self, path: str, data: bytes) -> None:
        self._present.add(path)

    async def file_exists(self, path: str) -> bool:
        # Records the call so the test can prove the sandbox was consulted
        # (rather than the host FS). Resolution happens "inside the box".
        self.file_exists_calls.append(path)
        return path in self._present


@pytest.mark.asyncio
async def test_c18_container_backend_present_file_emits_met_note(tmp_path):
    """B4 regression: on the container backend (`workspace_path is None`) a file
    that exists INSIDE the box must yield a `met` note — NOT the old false
    `missing`. The advisory must consult `sbx.file_exists`, never the host cwd."""
    # The agent-server cwd does NOT contain `artifact.txt`; only the box does.
    sbx = _FakeContainerSandbox(present={"artifact.txt"})

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "artifact.txt",
                            },
                        }
                    ],
                },
            ),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    assert "met" in note.message.content
    assert "NOT met" not in note.message.content, (
        "B4 regression: a container file that exists was falsely reported missing"
    )
    assert note.meta.get("passed") is True
    # Prove the sandbox was actually consulted (not the host FS).
    assert sbx.file_exists_calls == ["artifact.txt"]


@pytest.mark.asyncio
async def test_c18_container_backend_absent_file_still_not_met(tmp_path):
    """B4: the fix must NOT mask genuine absences — a file that is missing INSIDE
    the box still yields a `NOT met` advisory (no false positives the other way)."""
    sbx = _FakeContainerSandbox(present=set())  # nothing in the box

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "artifact.txt",
                            },
                        }
                    ],
                },
            ),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    assert "NOT met" in notes[0].message.content
    assert notes[0].meta.get("passed") is False
    assert sbx.file_exists_calls == ["artifact.txt"]


@pytest.mark.asyncio
async def test_c18_real_workspace_path_escape_is_rejected_before_approval(tmp_path):
    """An escaping predicate never reaches approval or the sandbox evaluator.

    The lower-level evaluator retains fail-closed traversal coverage in
    test_dod_evaluator.py for legacy/persisted specs. The public planning boundary
    is now stricter: it rejects the unsafe immutable gate and lets the model submit
    a corrected plan.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Plant a file OUTSIDE the workspace that the escaping path would reach.
    (tmp_path / "secret.txt").write_text("x")
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "escape",
                            "done_condition": {
                                "kind": "file_exists",
                                "path": "../secret.txt",
                            },
                        }
                    ],
                },
            ),
            action_step(
                "submit_plan",
                {
                    "summary": "corrected",
                    "steps": [{"title": "safe step"}],
                },
            ),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    plans = [event for event in events if isinstance(event, PlanEvent)]
    assert [plan.summary for plan in plans] == ["corrected"]
    assert any(
        isinstance(event, StatusEvent) and event.detail == "invalid_plan_done_conditions"
        for event in events
    )
    assert any(
        isinstance(event, MessageEvent)
        and event.source == EventSource.ENVIRONMENT
        and "safe exact workspace file" in event.message.content
        for event in events
    )
    assert _advisory_notes(events) == []
    assert sbx.file_exists_calls == []


@pytest.mark.asyncio
async def test_c18_placeholder_http_gate_is_rejected_before_plan_persistence(tmp_path):
    """A future single-label URL never becomes an immutable external gate.

    The live failure reached a correct durable build, then paused after three DNS
    failures because ``http://should-be-verified-later`` had been accepted during
    planning. The public boundary must reject that plan, give actionable feedback,
    and accept a corrected plan that omits the unverifiable condition.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sbx = _FakeSandbox(str(workspace))
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "unsafe placeholder",
                    "steps": [
                        {
                            "title": "Verify the future preview",
                            "done_condition": {
                                "kind": "http_ok",
                                "url": "http://should-be-verified-later",
                            },
                        }
                    ],
                },
            ),
            action_step(
                "submit_plan",
                {
                    "summary": "corrected",
                    "steps": [{"title": "Build and verify the exact files"}],
                },
            ),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    plans = [event for event in events if isinstance(event, PlanEvent)]
    assert [plan.summary for plan in plans] == ["corrected"]
    assert await store.get_dod_spec(CID) is None
    assert any(
        isinstance(event, StatusEvent) and event.detail == "invalid_plan_done_conditions"
        for event in events
    )
    feedback = [
        event.message.content
        for event in events
        if isinstance(event, MessageEvent) and event.source == EventSource.ENVIRONMENT
    ]
    assert any(
        "concrete, already-known fully qualified hostname or IP address" in message
        and "omit the condition" in message
        for message in feedback
    )
    assert _advisory_notes(events) == []
    assert sbx.file_exists_calls == []


@pytest.mark.parametrize(
    ("bad_command", "expected_reason"),
    [
        (
            "python3 -m http.server 8080 >/tmp/site.log 2>&1 & "
            "sleep 1; curl -fsS http://localhost:8080/",
            "starts background work",
        ),
        (
            "nohup npm run dev >/tmp/site.log 2>&1 &",
            "starts background work",
        ),
        ("sh -lc 'sleep 30 &'", "starts background work"),
        ("curl -fsS http://[::1]", "probes a loopback/local preview URL"),
        ("curl -fsS http://app:8080", "probes a loopback/local preview URL"),
        ("npm run dev", "launches a local server"),
        ("sh -lc 'npm --prefix app run dev'", "launches a local server"),
        (
            '/usr/bin/curl -fsS "$PREVIEW_URL/health"',
            "probes a runtime-selected preview URL",
        ),
        ("timeout 5 npm run dev", "launches a local server"),
        ("env -u FOO npm run dev", "launches a local server"),
        ("bash -ec 'npm run dev'", "launches a local server"),
        ("python manage.py runserver", "launches a local server"),
        ("python -m flask run", "launches a local server"),
        ("npm run --silent dev", "launches a local server"),
        ("yarn run --cwd app dev", "launches a local server"),
        ("command -- npm exec vite", "launches a local server"),
    ],
)
@pytest.mark.asyncio
async def test_c18_local_preview_command_gate_is_rejected_then_corrected_plan_is_accepted(
    tmp_path, bad_command, expected_reason
):
    """A plan cannot freeze a server lifecycle or preview address chosen at runtime.

    The public ``submit_plan`` boundary must reject the bad gate, provide one
    bounded/actionable correction, then accept an ordinary finite read-only gate.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sbx = _FakeSandbox(str(workspace))
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "unsafe preview gate",
                    "steps": [
                        {
                            "title": "Start and verify the local preview",
                            "done_condition": {
                                "kind": "command",
                                "cmd": bad_command,
                                "expect_exit": 0,
                            },
                        }
                    ],
                },
            ),
            action_step(
                "submit_plan",
                {
                    "summary": "corrected finite gate",
                    "steps": [
                        {
                            "title": "Build and test the static site",
                            "done_condition": {
                                "kind": "command",
                                "cmd": "test -f index.html && npm test",
                                "expect_exit": 0,
                            },
                        }
                    ],
                },
            ),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    plans = [event for event in events if isinstance(event, PlanEvent)]
    assert [plan.summary for plan in plans] == ["corrected finite gate"]
    assert plans[0].steps[0].done_condition is not None
    assert plans[0].steps[0].done_condition.cmd == "test -f index.html && npm test"

    feedback = [
        event.message.content
        for event in events
        if isinstance(event, MessageEvent)
        and event.source == EventSource.ENVIRONMENT
        and event.meta.get("blocking") == "invalid_plan_done_conditions"
    ]
    assert len(feedback) == 1
    assert expected_reason in feedback[0]
    assert "finite, deterministic verification" in feedback[0]
    assert "`test -f index.html`" in feedback[0]
    assert "`npm test`" in feedback[0]
    assert "`preview_start`" in feedback[0]
    assert "Invalid plan attempt 1/3" in feedback[0]
    assert len(feedback[0]) < 1_800
    assert _advisory_notes(events) == []
    assert sbx.file_exists_calls == []


# ---------------------------------------------------------------------------
# F-2 — container backend: command predicate ran on the HOST (cwd=None), not
# in the box. Fix: when sbx has `exec_shell`, run the predicate INSIDE THE BOX.
# ---------------------------------------------------------------------------


@dataclass
class _FakeExecResult:
    """Minimal duck-type of tools.sandbox.base.ExecResult for these tests.
    Uses only the fields the C18 evaluator reads: exit_code and timed_out."""

    exit_code: int
    stdout: str = _dc_field(default="")
    stderr: str = _dc_field(default="")
    timed_out: bool = _dc_field(default=False)


class _FakeContainerSandboxWithExecShell:
    """Container backend (workspace_path=None) that exposes exec_shell. Records
    every call so tests can prove exec_shell — not host subprocess — was used.
    The outcome is controlled by the _FakeExecResult passed at construction."""

    workspace_path = None

    def __init__(self, result: _FakeExecResult) -> None:
        self._result = result
        self.exec_shell_calls: list[tuple[str, int]] = []

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> _FakeExecResult:
        self.exec_shell_calls.append((cmd, timeout_s))
        return self._result

    async def file_exists(self, path: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_c18_container_command_uses_exec_shell(tmp_path):
    """F-2: on a container backend (workspace_path=None, exec_shell present), a
    command predicate is evaluated via exec_shell, NOT host subprocess.
    The advisory note reflects the sandbox result and stamps '(sandbox)'."""
    sbx = _FakeContainerSandboxWithExecShell(result=_FakeExecResult(exit_code=0))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "check build output",
                            "done_condition": {
                                "kind": "command",
                                "cmd": "test -d css && test -f index.html",
                                "expect_exit": 0,
                            },
                        }
                    ],
                },
            ),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    # exec_shell returned exit_code=0 matching expect_exit=0 → met.
    assert "met" in note.message.content
    assert "NOT met" not in note.message.content
    assert note.meta.get("passed") is True
    # Prove exec_shell (not host subprocess) was consulted — exactly once.
    assert len(sbx.exec_shell_calls) == 1
    assert sbx.exec_shell_calls[0][0] == "test -d css && test -f index.html"
    # Sandbox path stamps "(sandbox)" in the reason (not the subprocess "(in X.XXs)").
    assert "(sandbox)" in note.message.content


@pytest.mark.asyncio
async def test_c18_container_command_timed_out_fails(tmp_path):
    """F-2 / timeout caveat: timed_out=True on the ExecResult is always a
    non-pass, even when exit_code coincidentally matches expect_exit (e.g. 124).
    The note must name 'timed out' so the user sees the actual failure reason."""
    # exit_code=0 equals expect_exit=0, but timed_out=True → must fail.
    sbx = _FakeContainerSandboxWithExecShell(result=_FakeExecResult(exit_code=0, timed_out=True))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "check dist",
                            "done_condition": {
                                "kind": "command",
                                "cmd": "test -d dist",
                                "expect_exit": 0,
                            },
                        }
                    ],
                },
            ),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    # timed_out=True → NOT met, even though exit_code would have matched.
    assert "NOT met" in note.message.content
    assert note.meta.get("passed") is False
    # The note names 'timed out' so the user knows why it failed.
    assert "timed out" in note.message.content
    # exec_shell was still called (the failure is detected after the call, not before).
    assert len(sbx.exec_shell_calls) == 1


@pytest.mark.asyncio
async def test_h342_c18_and_finish_fallback_share_bash_process_substitution(tmp_path):
    """The advisory and authoritative finish gate use identical Bash semantics.

    H342 froze this exact process-substitution predicate into the DoD, while the
    public proof worked only when the agent added ``bash -c``. The C18 fallback
    and fresh DoD runner both selected ambient ``/bin/sh`` and rejected a
    correct workspace. Keep the exact predicate here so either fallback drifting
    away from the shared Bash contract blocks FINISHED.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "primes.py").write_text(
        "def primes(n):\n"
        "    out = []\n"
        "    candidate = 2\n"
        "    while len(out) < n:\n"
        "        if all(candidate % p for p in out if p * p <= candidate):\n"
        "            out.append(candidate)\n"
        "        candidate += 1\n"
        "    return out\n"
        "print(*primes(20), sep='\\n')\n"
    )
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "verify the first twenty primes",
                            "done_condition": {
                                "kind": "command",
                                "cmd": _H342_PROCESS_SUBSTITUTION_PREDICATE,
                                "expect_exit": 0,
                            },
                        }
                    ],
                },
            ),
            # Cross the approved-plan execution boundary before marking it done.
            action_step("shell", {"command": "python3 primes.py"}),
            action_step("plan_step", {"index": 1, "state": "done"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    note = notes[0]
    assert "met" in note.message.content
    assert note.meta.get("passed") is True
    # Subprocess path stamps "in X.XXs"; sandbox path stamps "(sandbox)".
    assert "(sandbox)" not in note.message.content
    assert "in " in note.message.content
    final_status = next(event for event in reversed(events) if isinstance(event, StatusEvent))
    assert final_status.status == ConversationStatus.FINISHED


async def test_h342_c18_host_fallback_pins_bash_and_raw_command(tmp_path, monkeypatch):
    import os
    import subprocess

    from disco.core.dod import CommandExitPredicate
    from disco.core.loop.plan_conditions import PlanStepConditions

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sbx = _FakeSandbox(str(workspace))
    loop, _ = build_loop(
        ScriptedAgent([]), executor=_SandboxExecutor(sbx), conversation_id="conv-h342-c18"
    )
    captured: dict[str, object] = {}

    def _run(command: str, **kwargs):  # noqa: ANN003, ANN202
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="verified", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    command = "read -r value < <(printf 71); [[ $value == 71 ]]"
    passed, _note = await PlanStepConditions(loop).check_command_for_plan_step(
        CommandExitPredicate(cmd=command)
    )

    assert passed is True
    assert captured["command"] == command
    assert captured["shell"] is True
    assert captured["executable"] == "/bin/bash"
    assert captured["cwd"] == str(workspace)
    assert captured["timeout"] == 5.0
    assert captured["env"] == {"PATH": os.environ.get("PATH", "")}


# ---------------------------------------------------------------------------
# update_plan_progress (the #3 declarative tool) drives C18 the same as plan_step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c18_update_plan_progress_satisfied_emits_met_note(tmp_path):
    """A capable model marks the step done via the DECLARATIVE update_plan_progress
    snapshot (not plan_step). C18 must still evaluate the step's done_condition and emit
    the advisory — before the unify fix the capable path got NO done-condition checks."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "artifact.txt").write_text("ok")
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {"kind": "file_exists", "path": "artifact.txt"},
                        }
                    ],
                },
            ),
            action_step("update_plan_progress", {"steps": [{"index": 1, "state": "done"}]}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    notes = _advisory_notes(events)
    assert len(notes) == 1, f"expected exactly 1 C18 note, got {len(notes)}"
    assert "done-condition for plan step 1" in notes[0].message.content
    assert "met" in notes[0].message.content and "NOT met" not in notes[0].message.content
    assert notes[0].meta.get("passed") is True


@pytest.mark.asyncio
async def test_c18_update_plan_progress_no_respam_on_repeat_snapshot(tmp_path):
    """An update_plan_progress snapshot re-lists already-done steps every time; the C18
    hook fires ONLY for steps that TRANSITION to done, so a second identical snapshot
    must NOT emit a duplicate advisory for the same step."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "artifact.txt").write_text("ok")
    sbx = _FakeSandbox(str(workspace))

    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        {
                            "title": "write artifact",
                            "done_condition": {"kind": "file_exists", "path": "artifact.txt"},
                        },
                        {"title": "second step"},
                    ],
                },
            ),
            action_step("update_plan_progress", {"steps": [{"index": 1, "state": "done"}]}),
            # repeat snapshot re-lists step 1 done — must NOT re-emit its advisory
            action_step("update_plan_progress", {"steps": [{"index": 1, "state": "done"}]}),
            action_step(
                "update_plan_progress",
                {"steps": [{"index": 1, "state": "done"}, {"index": 2, "state": "done"}]},
            ),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    events = await store.get_events(CID)
    notes = _advisory_notes(events)
    # Exactly ONE note: step 1 transitioned once (step 2 has no predicate → no note).
    assert len(notes) == 1, f"expected 1 C18 note (no re-spam), got {len(notes)}"
    assert "plan step 1" in notes[0].message.content
