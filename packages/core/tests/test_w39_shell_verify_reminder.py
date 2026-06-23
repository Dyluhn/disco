"""W-39 — GATED advisory shell-verify reminder (assist-tier).

# Contract

A weak model that re-runs an IDEMPOTENT verify it already passed (Dylan saw
DeepSeek re-run ``ls -la``) is the SHELL analogue of the F9 read-loop. F9 +
``collapse_superseded_reads`` cover ONLY ``file_read``; the shell channel is
not covered, so once condensation drops the earlier success observation the
model re-runs the verify it already passed.

The fix is ADVISORY — unlike F9 (which SKIPS the duplicate read), a shell
command may have side effects, so it is NEVER auto-skipped. When a shell
command is byte-identical to one that produced a SUCCESSFUL observation
earlier AND no workspace mutation (the A8 mutating-tool set — the same
"no mutation since" tracking F9 reuses) has happened since, ONE advisory
``<system-reminder>`` is injected after the (still-executed) command's
observation; the model decides whether to keep re-verifying.

Gated on ``self._assist`` (mirrors F9). Assist OFF (capable-model default) →
the W-39 code path is never entered; byte-identical to today.

# Acceptance (this file)

  (a) assist ON + a byte-identical shell command that previously SUCCEEDED,
      no workspace mutation since → the advisory reminder is injected exactly
      ONCE, and the command STILL executes.
  (b) assist ON + a file_write between the two identical shell commands → NO
      reminder (re-running is legit); the command executes.
  (c) assist ON + the earlier identical shell command FAILED → NO "already
      passed" reminder; the command executes.
  (d) the command is NEVER skipped/blocked in any case (the executor is always
      called).
  (e) anti-spam: looping the same passed command 3× emits at most ONE reminder
      per passed-and-unmutated streak.
  (f) assist OFF → the W-39 path is never entered (no reminder ever).

Plus pure-helper contracts for the byte-identical match, the mutation gate,
the path-agnostic mutation tracking, and the anti-spam scan.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
)
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.loop.dedup import (
    _W39_REMINDER_SENTINEL,
    _w39_latest_mutation_seq,
    _w39_reminder_emitted_after,
    _w39_shell_verify_reminder,
)
from event_fakes import user_msg, with_seqs
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    NeverConfirm,
    ScriptedAgent,
)

CID = "conv"


class _NoOpCondenser:
    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


class _EchoExecutor(FakeExecutor):
    """Echoes the call's tool_name + call_id so shell vs. file_write
    observations stay distinguishable, and lets a per-call failure be
    forced (for the failed-prior-run test)."""

    def __init__(self, *, fail_calls: frozenset[str] | None = None):
        super().__init__()
        self._fail_calls = fail_calls or frozenset()

    async def execute(self, call):
        from disco.core import ToolResult as _TR

        self.calls.append(call)
        if call.call_id in self._fail_calls:
            return _TR(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=False,
                content="",
                error="command failed",
            )
        return _TR(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="command output",
        )

    def readonly_tool_names(self):
        return frozenset({"file_read"})


def _make_loop(*, model_policy: ModelExecutionPolicy | None = None, executor=None):
    from disco.core.loop.engine import AgentLoop

    if executor is None:
        executor = _EchoExecutor()
    return AgentLoop(
        CID,
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        _NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=model_policy or ModelExecutionPolicy.standard(),
    )


def _shell_action(call_id: str, cmd: str = "ls -la") -> ActionEvent:
    return ActionEvent(
        thought="running a verify",
        tool_call=ToolCall(tool_name="shell", call_id=call_id, arguments={"command": cmd}),
    )


def _write_action(call_id: str, path: str = "src/foo.py", content: str = "x") -> ActionEvent:
    return ActionEvent(
        thought="writing the file",
        tool_call=ToolCall(
            tool_name="file_write",
            call_id=call_id,
            arguments={"path": path, "content": content},
        ),
    )


def _shell_obs(action: ActionEvent, *, success: bool = True) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="shell",
            success=success,
            content="command output" if success else "",
            error=None if success else "command failed",
        ),
        action_id=action.id,
    )


def _write_obs(action: ActionEvent) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="file_write",
            success=True,
            content="wrote 1 char",
        ),
        action_id=action.id,
    )


async def _drive_execute(loop, action: ActionEvent) -> list[Event]:
    persisted = await loop.store.append(CID, action)
    await loop._execute_and_observe(persisted)
    return await loop.store.get_events(CID)


def _reminders(events: list[Event]) -> list[str]:
    out: list[str] = []
    for e in events:
        if (
            isinstance(e, MessageEvent)
            and e.message is not None
            and isinstance(e.message.content, str)
            and _W39_REMINDER_SENTINEL in e.message.content
        ):
            out.append(e.message.content)
    return out


# ---------------------------------------------------------------------------
# (a) byte-identical passed shell, no mutation since → reminder once + executes
# ---------------------------------------------------------------------------


async def test_assist_on_repeat_passed_shell_injects_reminder_once():
    ex = _EchoExecutor()
    loop = _make_loop(model_policy=ModelExecutionPolicy(tier="weak"), executor=ex)
    first = _shell_action("call_shell_1", "ls -la")
    events = await _drive_execute(loop, first)
    # No reminder on the FIRST run.
    assert _reminders(events) == []
    assert len(ex.calls) == 1

    second = _shell_action("call_shell_2", "ls -la")
    events = await _drive_execute(loop, second)
    # The command STILL executed (advisory — never skipped). (d)
    assert len(ex.calls) == 2, "W-39 must never skip the shell command"
    # Exactly one advisory reminder was injected.
    reminders = _reminders(events)
    assert len(reminders) == 1
    assert "ls -la" in reminders[0]
    assert "already ran" in reminders[0]
    assert "<system-reminder>" in reminders[0]
    # The reminder is emitted AFTER the second observation (never splits the
    # action/result pair).
    obs_idx = [i for i, e in enumerate(events) if isinstance(e, ObservationEvent)]
    rem_idx = [
        i
        for i, e in enumerate(events)
        if isinstance(e, MessageEvent)
        and e.message is not None
        and isinstance(e.message.content, str)
        and _W39_REMINDER_SENTINEL in e.message.content
    ]
    assert rem_idx[0] > obs_idx[-1], "reminder must follow the observation"


# ---------------------------------------------------------------------------
# (b) a file_write between the two identical shell runs → NO reminder
# ---------------------------------------------------------------------------


async def test_assist_on_mutation_between_runs_suppresses_reminder():
    ex = _EchoExecutor()
    loop = _make_loop(model_policy=ModelExecutionPolicy(tier="weak"), executor=ex)
    await _drive_execute(loop, _shell_action("call_shell_1", "ls -la"))
    # A real file_write lands between the two identical verifies.
    await _drive_execute(loop, _write_action("call_write_1", "src/foo.py"))
    events = await _drive_execute(loop, _shell_action("call_shell_2", "ls -la"))
    # Re-running the verify is legit after a mutation → NO reminder.
    assert _reminders(events) == []
    # And the command executed.
    shell_calls = [c for c in ex.calls if c.tool_name == "shell"]
    assert len(shell_calls) == 2


# ---------------------------------------------------------------------------
# (c) the earlier identical shell run FAILED → no "already passed" reminder
# ---------------------------------------------------------------------------


async def test_assist_on_failed_prior_run_does_not_remind():
    # Force the FIRST shell call to fail.
    ex = _EchoExecutor(fail_calls=frozenset({"call_shell_1"}))
    loop = _make_loop(model_policy=ModelExecutionPolicy(tier="weak"), executor=ex)
    events = await _drive_execute(loop, _shell_action("call_shell_1", "ls -la"))
    # First run failed → an AgentErrorEvent, no success observation.
    assert not any(
        isinstance(e, ObservationEvent) and e.tool_result.success for e in events
    )
    events = await _drive_execute(loop, _shell_action("call_shell_2", "ls -la"))
    # No "already passed" reminder — the earlier run did NOT pass.
    assert _reminders(events) == []
    shell_calls = [c for c in ex.calls if c.tool_name == "shell"]
    assert len(shell_calls) == 2


# ---------------------------------------------------------------------------
# (d) explicit: command is never skipped even when the reminder fires
# ---------------------------------------------------------------------------


async def test_command_always_executes_when_reminder_fires():
    ex = _EchoExecutor()
    loop = _make_loop(model_policy=ModelExecutionPolicy(tier="weak"), executor=ex)
    await _drive_execute(loop, _shell_action("call_shell_1", "pytest -q"))
    events = await _drive_execute(loop, _shell_action("call_shell_2", "pytest -q"))
    # Reminder fired...
    assert len(_reminders(events)) == 1
    # ...AND the second command executed (two executor calls) with a real
    # observation (not a synthetic skip).
    shell_calls = [c for c in ex.calls if c.tool_name == "shell"]
    assert len(shell_calls) == 2
    shell_obs = [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.tool_name == "shell"
    ]
    assert len(shell_obs) == 2
    assert all(o.tool_result.content == "command output" for o in shell_obs)


# ---------------------------------------------------------------------------
# (e) anti-spam: looping the same passed command 3× → at most ONE reminder
# ---------------------------------------------------------------------------


async def test_anti_spam_one_reminder_per_streak():
    ex = _EchoExecutor()
    loop = _make_loop(model_policy=ModelExecutionPolicy(tier="weak"), executor=ex)
    events: list[Event] = []
    for i in range(4):
        events = await _drive_execute(loop, _shell_action(f"call_shell_{i + 1}", "ls -la"))
    # Four identical passing runs, no mutation between any → exactly ONE
    # reminder across the whole streak (no spam).
    assert len(_reminders(events)) == 1
    # All four executed.
    assert len([c for c in ex.calls if c.tool_name == "shell"]) == 4


async def test_anti_spam_mutation_resets_streak():
    ex = _EchoExecutor()
    loop = _make_loop(model_policy=ModelExecutionPolicy(tier="weak"), executor=ex)
    await _drive_execute(loop, _shell_action("s1", "ls -la"))
    await _drive_execute(loop, _shell_action("s2", "ls -la"))  # reminder #1
    # A write resets the streak.
    await _drive_execute(loop, _write_action("w1", "src/foo.py"))
    await _drive_execute(loop, _shell_action("s3", "ls -la"))  # no reminder (legit re-verify)
    events = await _drive_execute(loop, _shell_action("s4", "ls -la"))  # reminder #2 (new streak)
    assert len(_reminders(events)) == 2


# ---------------------------------------------------------------------------
# (f) assist OFF → the W-39 path is never entered
# ---------------------------------------------------------------------------


async def test_assist_off_never_reminds():
    ex = _EchoExecutor()
    loop = _make_loop(model_policy=ModelExecutionPolicy.standard(), executor=ex)
    for i in range(3):
        events = await _drive_execute(loop, _shell_action(f"call_shell_{i + 1}", "ls -la"))
    assert _reminders(events) == []
    assert len([c for c in ex.calls if c.tool_name == "shell"]) == 3


# ---------------------------------------------------------------------------
# different-args / non-shell tools → no reminder
# ---------------------------------------------------------------------------


async def test_different_command_does_not_remind():
    ex = _EchoExecutor()
    loop = _make_loop(model_policy=ModelExecutionPolicy(tier="weak"), executor=ex)
    await _drive_execute(loop, _shell_action("s1", "ls -la"))
    events = await _drive_execute(loop, _shell_action("s2", "ls -la /tmp"))
    assert _reminders(events) == []


# ---------------------------------------------------------------------------
# pure-helper contracts
# ---------------------------------------------------------------------------


def test_helper_returns_false_for_non_shell_tool():
    first = _shell_action("c1", "ls")
    events = with_seqs([user_msg("hi"), first, _shell_obs(first), _shell_action("c2", "ls")])
    # file_read is not a shell tool → never reminded.
    remind, _, _ = _w39_shell_verify_reminder("file_read", {"path": "x"}, events)
    assert remind is False


def test_helper_byte_identical_passed_no_mutation_reminds():
    first = _shell_action("c1", "ls -la")
    current = _shell_action("c2", "ls -la")
    events = with_seqs([user_msg("hi"), first, _shell_obs(first), current])
    remind, prior_seq, text = _w39_shell_verify_reminder(
        "shell", {"command": "ls -la"}, events
    )
    assert remind is True
    # with_seqs assigns seqs to COPIES; the prior shell action is events[1].
    assert prior_seq == events[1].seq
    assert _W39_REMINDER_SENTINEL in text
    assert "ls -la" in text


def test_helper_failed_prior_does_not_remind():
    first = _shell_action("c1", "ls -la")
    current = _shell_action("c2", "ls -la")
    events = with_seqs(
        [user_msg("hi"), first, _shell_obs(first, success=False), current]
    )
    remind, _, _ = _w39_shell_verify_reminder("shell", {"command": "ls -la"}, events)
    assert remind is False


def test_helper_mutation_between_does_not_remind():
    first = _shell_action("c1", "ls -la")
    write = _write_action("c2", "src/foo.py")
    current = _shell_action("c3", "ls -la")
    events = with_seqs(
        [
            user_msg("hi"),
            first,
            _shell_obs(first),
            write,
            _write_obs(write),
            current,
        ]
    )
    remind, _, _ = _w39_shell_verify_reminder("shell", {"command": "ls -la"}, events)
    assert remind is False


def test_latest_mutation_seq_helper():
    events = with_seqs(
        [user_msg("hi"), _shell_action("c0", "ls"), _write_action("c1", "src/foo.py")]
    )
    # The write (events[-1]) is the latest (and only) mutation; with_seqs
    # assigns seqs to COPIES, so read the seq off the copy in `events`.
    assert _w39_latest_mutation_seq(events) == events[-1].seq
    # No mutation at all → 0.
    none_events = with_seqs([user_msg("hi"), _shell_action("c0", "ls")])
    assert _w39_latest_mutation_seq(none_events) == 0


def test_reminder_emitted_after_helper():
    # An env reminder MessageEvent carrying the sentinel + command is detected
    # when its seq exceeds after_seq.
    text = f"<system-reminder>\n{_W39_REMINDER_SENTINEL} You already ran `ls -la` ..."
    env_msg = MessageEvent(
        source=EventSource.ENVIRONMENT, message=LLMMessage(role="user", content=text)
    )
    events = with_seqs([user_msg("hi"), env_msg])
    rem_seq = events[-1].seq or 0
    assert _w39_reminder_emitted_after(events, "ls -la", after_seq=rem_seq - 1) is True
    # after_seq at/above the reminder's seq → not counted (already-emitted is
    # strictly-after).
    assert _w39_reminder_emitted_after(events, "ls -la", after_seq=rem_seq) is False
    # A different command → not matched.
    assert _w39_reminder_emitted_after(events, "pytest -q", after_seq=0) is False
