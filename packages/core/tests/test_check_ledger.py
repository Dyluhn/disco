"""Check ledger, phase 1 (observe only) — the record every shell run leaves behind.

Contract:
  (a) a `shell` / `shell_exec` run persists meta["check"] on its observation (or on its
      AgentErrorEvent when the command fails): fingerprint, source-tree digest before and
      after, exit code, busy sessions before and after;
  (b) no other tool gets a record;
  (c) a sandbox without a digest (no exec_shell, failing pipeline) records None and never
      counts as a memo hit;
  (d) `memo_hits` names the earlier run that already answered a repeat: same call, prior
      pass that changed nothing, same digest and sessions now as then — and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
)
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.loop import check_ledger
from event_fakes import with_seqs
from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer, NeverConfirm, ScriptedAgent

CID = "conv"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class _NoOpCondenser:
    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


@dataclass
class _Exec:
    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False


@dataclass
class _Info:
    name: str
    busy: bool


class _Sessions:
    def __init__(self, busy: list[str]) -> None:
        self.busy = busy

    async def list(self):
        return [_Info(name, True) for name in self.busy] + [_Info("idle-one", False)]


class _Sandbox:
    """Answers the digest pipeline from `digests` (popped in order) and lists sessions."""

    def __init__(self, digests: list[str | None], busy: list[str] | None = None) -> None:
        self.digests = list(digests)
        self.sessions = _Sessions(busy or [])
        self.commands: list[str] = []

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> _Exec:
        self.commands.append(cmd)
        digest = self.digests.pop(0) if self.digests else None
        if digest is None:
            return _Exec(exit_code=1, stdout="", stderr="find: broken")
        return _Exec(exit_code=0, stdout=digest + "\n")


class _ShellExecutor(FakeExecutor):
    def __init__(self, sandbox: _Sandbox | None, *, exit_code: int = 0) -> None:
        super().__init__()
        self.sandbox = sandbox
        self._exit_code = exit_code

    async def execute(self, call):
        self.calls.append(call)
        ok = self._exit_code == 0
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=ok,
            content="output" if ok else "",
            error=None if ok else f"command exited {self._exit_code}",
            structured={"exit_code": self._exit_code},
        )


def _make_loop(executor) -> object:
    from disco.core.loop.engine import AgentLoop

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
        model_policy=ModelExecutionPolicy.standard(),
    )


def _action(tool: str, call_id: str, **arguments) -> ActionEvent:
    return ActionEvent(thought="t", tool_call=ToolCall(tool_name=tool, call_id=call_id, arguments=arguments))


async def _drive(loop, action: ActionEvent) -> list[Event]:
    persisted = await loop.store.append(CID, action)
    await loop._execute_and_observe(persisted)
    return await loop.store.get_events(CID)


def _record(events: list[Event]) -> dict:
    tail = [e for e in events if isinstance(e, ObservationEvent | AgentErrorEvent)][-1]
    return tail.meta["check"]


# --- (a) the record ---------------------------------------------------------------


async def test_shell_run_records_digests_exit_code_and_sessions() -> None:
    sandbox = _Sandbox([DIGEST_A, DIGEST_A], busy=["server"])
    loop = _make_loop(_ShellExecutor(sandbox))
    events = await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))

    record = _record(events)
    assert record["fingerprint"] == 'shell:{"command":"bash smoke.sh"}'
    assert record["tree_before"] == DIGEST_A
    assert record["tree_after"] == DIGEST_A
    assert record["exit_code"] == 0
    assert record["sessions_before"] == ["server"]
    assert record["sessions_after"] == ["server"]
    assert len(sandbox.commands) == 2
    assert "sha256sum" in sandbox.commands[0]


async def test_failed_shell_run_records_on_the_error_event() -> None:
    loop = _make_loop(_ShellExecutor(_Sandbox([DIGEST_A, DIGEST_A]), exit_code=1))
    events = await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))

    error = [e for e in events if isinstance(e, AgentErrorEvent)][-1]
    assert error.meta["check"]["exit_code"] == 1
    assert error.meta["check"]["tree_before"] == DIGEST_A


async def test_shell_exec_is_recorded_too() -> None:
    loop = _make_loop(_ShellExecutor(_Sandbox([DIGEST_A, DIGEST_B])))
    events = await _drive(loop, _action("shell_exec", "c1", session="s", command="node server.js &"))

    record = _record(events)
    assert record["tree_before"] == DIGEST_A
    assert record["tree_after"] == DIGEST_B


# --- (b) other tools ------------------------------------------------------------------


async def test_non_shell_tools_get_no_record_and_no_digest_exec() -> None:
    sandbox = _Sandbox([DIGEST_A, DIGEST_A])
    loop = _make_loop(_ShellExecutor(sandbox))
    events = await _drive(loop, _action("file_read", "c1", path="a.py"))

    obs = [e for e in events if isinstance(e, ObservationEvent)][-1]
    assert "check" not in obs.meta
    assert sandbox.commands == []


# --- (c) fail closed ------------------------------------------------------------------


async def test_no_sandbox_records_none_digest() -> None:
    loop = _make_loop(_ShellExecutor(None))
    events = await _drive(loop, _action("shell", "c1", command="ls"))

    record = _record(events)
    assert record["tree_before"] is None and record["tree_after"] is None
    assert record["exit_code"] == 0


async def test_failing_digest_pipeline_records_none() -> None:
    loop = _make_loop(_ShellExecutor(_Sandbox([None, None])))
    events = await _drive(loop, _action("shell", "c1", command="ls"))

    record = _record(events)
    assert record["tree_before"] is None and record["tree_after"] is None


def test_digest_command_prunes_dependency_dirs_and_runtime_state() -> None:
    cmd = check_ledger.tree_digest_command()
    assert "-name node_modules" in cmd and "-name .git" in cmd
    assert "! -name '*.db'" in cmd and "! -name '*.log'" in cmd
    assert cmd.endswith("| sha256sum | cut -d' ' -f1")


# --- (d) memo hits over the record -------------------------------------------------


def _obs(call_id: str, command: str, *, before: str | None, after: str | None, exit_code: int = 0,
         sessions: tuple[str, ...] = ()) -> list[Event]:
    action = _action("shell", call_id, command=command)
    meta = check_ledger.check_meta(
        action,
        check_ledger.CheckSnapshot(before, sessions),
        check_ledger.CheckSnapshot(after, sessions),
        exit_code,
    )
    result = ToolResult(call_id=call_id, tool_name="shell", success=exit_code == 0, content="")
    if exit_code == 0:
        return [action, ObservationEvent(tool_result=result, action_id=action.id, meta={"check": meta})]
    return [action, AgentErrorEvent(error="exit", action_id=action.id, meta={"check": meta})]


def test_repeat_of_a_pure_pass_on_unchanged_tree_is_a_hit() -> None:
    events = with_seqs(
        _obs("c1", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
        + _obs("c2", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
    )
    hits = check_ledger.memo_hits(events)
    assert len(hits) == 1
    run, source = hits[0]
    assert run.seq == 4 and source.seq == 2


def test_no_hit_when_tree_changed_between_runs() -> None:
    events = with_seqs(
        _obs("c1", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
        + _obs("c2", "bash smoke.sh", before=DIGEST_B, after=DIGEST_B)
    )
    assert check_ledger.memo_hits(events) == []


def test_no_hit_when_the_prior_run_failed_or_mutated() -> None:
    failed = with_seqs(
        _obs("c1", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A, exit_code=1)
        + _obs("c2", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
    )
    assert check_ledger.memo_hits(failed) == []
    mutated = with_seqs(
        _obs("c1", "npm install", before=DIGEST_A, after=DIGEST_B)
        + _obs("c2", "npm install", before=DIGEST_B, after=DIGEST_B)
    )
    assert check_ledger.memo_hits(mutated) == []


def test_no_hit_without_a_digest_or_with_different_sessions() -> None:
    unknown = with_seqs(
        _obs("c1", "ls", before=None, after=None) + _obs("c2", "ls", before=None, after=None)
    )
    assert check_ledger.memo_hits(unknown) == []
    sessions = with_seqs(
        _obs("c1", "curl localhost:3000", before=DIGEST_A, after=DIGEST_A, sessions=("server",))
        + _obs("c2", "curl localhost:3000", before=DIGEST_A, after=DIGEST_A, sessions=())
    )
    assert check_ledger.memo_hits(sessions) == []


def test_only_the_latest_run_of_a_command_answers_it() -> None:
    # c2 repeats c1 on the same source and would have been answered from c1 — but it
    # then failed, so c3 finds a failed latest run and is executed for real.
    events = with_seqs(
        _obs("c1", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
        + _obs("c2", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A, exit_code=1)
        + _obs("c3", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
    )
    hits = check_ledger.memo_hits(events)
    assert [(run.seq, source.seq) for run, source in hits] == [(4, 2)]
    # …and that hit would have lied: the same call on the same source gave a different
    # exit. This is the flake/runtime-state signal the observe-only phase measures.
    disagreements = check_ledger.memo_disagreements(events)
    assert [(run.seq, source.seq) for run, source in disagreements] == [(4, 2)]


def test_agreeing_hits_are_not_disagreements() -> None:
    events = with_seqs(
        _obs("c1", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
        + _obs("c2", "bash smoke.sh", before=DIGEST_A, after=DIGEST_A)
    )
    assert check_ledger.memo_disagreements(events) == []


# --- phase 2: show the ledger --------------------------------------------------------

from disco.core.effects import ActionProfile, EffectCapability  # noqa: E402

MUTATE = ActionProfile(capabilities=frozenset({EffectCapability.WORKSPACE_MUTATE}))


def _edit(call_id: str, path: str) -> list[Event]:
    action = _action("file_edit", call_id, path=path, old="a", new="b")
    result = ToolResult(
        call_id=call_id, tool_name="file_edit", success=True, content="edited", action_profile=MUTATE
    )
    return [action, ObservationEvent(tool_result=result, action_id=action.id)]


def _pure(call_id: str, command: str, *, exit_code: int = 0, sessions: tuple[str, ...] = ()) -> list[Event]:
    return _obs(call_id, command, before=DIGEST_A, after=DIGEST_A, exit_code=exit_code, sessions=sessions)


def _running_session(call_id: str, name: str, command: str) -> list[Event]:
    action = _action("shell_exec", call_id, session=name, command=command)
    result = ToolResult(
        call_id=call_id, tool_name="shell_exec", success=True, content="still running",
        structured={"running": True},
    )
    return [action, ObservationEvent(tool_result=result, action_id=action.id)]


def test_mutations_are_edits_and_tree_changing_shell_runs_only() -> None:
    events = with_seqs(
        _edit("e1", "src/a.js")                                          # seq 1-2: mutation
        + _pure("c1", "bash smoke.sh")                                    # seq 3-4: pure, not a mutation
        + _obs("c2", "npm install", before=DIGEST_A, after=DIGEST_B)      # seq 5-6: changed tree
        + _obs("c3", "ls", before=None, after=None)                       # seq 7-8: unknown digest
    )
    found = check_ledger.mutations(events)
    assert [(m.seq, m.tool, m.path) for m in found] == [
        (2, "file_edit", "src/a.js"),
        (6, "shell", None),
        (8, "shell", None),
    ]


def test_check_statuses_current_stale_failed_and_excludes_mutating_runs() -> None:
    events = with_seqs(
        _pure("c1", "bash smoke.sh")                                      # seq 1-2 → stale (edit follows)
        + _pure("c2", "curl localhost:3000/api/me", exit_code=7)          # seq 3-4 → failed
        + _edit("e1", "src/email.js")                                     # seq 5-6
        + _obs("c4", "npm install", before=DIGEST_A, after=DIGEST_B)      # seq 7-8: excluded
        + _pure("c3", "node rt-test.mjs")                                 # seq 9-10 → current
    )
    statuses = check_ledger.check_statuses(events)
    assert [(s.command, s.state, s.seq) for s in statuses] == [
        ("bash smoke.sh", "stale", 2),
        ("curl localhost:3000/api/me", "failed", 4),
        ("node rt-test.mjs", "current", 10),
    ]
    assert statuses[0].staled_by is not None and statuses[0].staled_by.path == "src/email.js"
    rendered = check_ledger.render_checks(statuses)
    assert rendered is not None
    assert rendered.splitlines()[0] == check_ledger.CHECKS_HEADER
    assert "- [stale: file_edit src/email.js at step 6] exit 0 at step 2  bash smoke.sh" in rendered
    assert "- [failed] exit 7 at step 4  curl localhost:3000/api/me" in rendered
    assert "- [current] exit 0 at step 10  node rt-test.mjs" in rendered


def test_latest_run_wins_and_the_table_is_capped() -> None:
    runs: list[Event] = []
    for i in range(15):
        runs += _pure(f"c{i}", f"bash t{i}.sh")
    runs += _pure("again", "bash t0.sh")
    statuses = check_ledger.check_statuses(with_seqs(runs))
    assert len(statuses) == check_ledger.CHECKS_SHOWN
    assert statuses[-1].command == "bash t0.sh" and statuses[-1].seq == 32


def test_no_checks_renders_nothing() -> None:
    assert check_ledger.render_checks([]) is None
    assert check_ledger.staled_checks_line([]) is None


def test_staled_line_names_only_current_passing_checks() -> None:
    events = with_seqs(
        _pure("c1", "bash smoke.sh") + _pure("c2", "bash lint.sh", exit_code=1)
        + _edit("e0", "x.js") + _pure("c3", "node rt.mjs")
    )
    line = check_ledger.staled_checks_line(events)
    assert line == "[staled 1 recorded check: node rt.mjs — re-run after your changes]"


def test_stale_sessions_started_before_a_source_change() -> None:
    events = with_seqs(
        _running_session("s1", "server", "node server.js")   # seq 1-2
        + _edit("e1", "src/routes.js")                        # seq 3-4
        + _running_session("s2", "mailpit", "./mailpit")      # seq 5-6, after the edit
    )
    stale = check_ledger.stale_sessions(events, ["server", "mailpit", "unknown"])
    assert [(s.name, s.started_seq, s.changed_by.seq) for s in stale] == [("server", 2, 4)]
    assert "serving old code until restarted" in stale[0].render()


# --- phase 2 through the loop --------------------------------------------------------


class _ProfileExecutor(_ShellExecutor):
    """Shell runs as before; file_edit returns a mutate-profiled result."""

    async def execute(self, call):
        if call.tool_name == "file_edit":
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id, tool_name="file_edit", success=True,
                content="edited src/a.js", action_profile=MUTATE,
            )
        if call.tool_name == "shell_exec":
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id, tool_name="shell_exec", success=True,
                content="session 'server' — still running", structured={"running": True},
            )
        return await super().execute(call)


async def test_edit_result_carries_the_staled_checks_line() -> None:
    sandbox = _Sandbox([DIGEST_A, DIGEST_A])
    loop = _make_loop(_ProfileExecutor(sandbox))
    await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))
    events = await _drive(loop, _action("file_edit", "e1", path="src/a.js", old="a", new="b"))

    obs = [e for e in events if isinstance(e, ObservationEvent)][-1]
    assert obs.tool_result.content == (
        "edited src/a.js\n[staled 1 recorded check: bash smoke.sh — re-run after your changes]"
    )


async def test_edit_without_current_checks_is_untouched() -> None:
    loop = _make_loop(_ProfileExecutor(_Sandbox([])))
    events = await _drive(loop, _action("file_edit", "e1", path="src/a.js", old="a", new="b"))
    obs = [e for e in events if isinstance(e, ObservationEvent)][-1]
    assert obs.tool_result.content == "edited src/a.js"


async def test_shell_result_warns_about_a_session_serving_old_code() -> None:
    # the server session starts (digest A→A), an edit follows, then a shell check runs while
    # the session is still busy: the check's result names the stale process.
    sandbox = _Sandbox([DIGEST_A, DIGEST_A, DIGEST_A, DIGEST_A], busy=["server"])
    loop = _make_loop(_ProfileExecutor(sandbox))
    await _drive(loop, _action("shell_exec", "s1", session="server", command="node server.js"))
    await _drive(loop, _action("file_edit", "e1", path="src/routes.js", old="a", new="b"))
    events = await _drive(loop, _action("shell", "c1", command="curl localhost:3000"))

    obs = [e for e in events if isinstance(e, ObservationEvent)][-1]
    assert obs.tool_result.tool_name == "shell"
    assert "[session 'server' has been running since step" in obs.tool_result.content
    assert "file_edit src/routes.js" in obs.tool_result.content


# --- phase 3: answer a repeat from the record ---------------------------------------


class _SeqExecutor(_ShellExecutor):
    """Shell runs exit with the next code from `codes`; records every execution."""

    def __init__(self, sandbox: _Sandbox | None, codes: list[int]) -> None:
        super().__init__(sandbox)
        self._codes = list(codes)

    async def execute(self, call):
        self.calls.append(call)
        code = self._codes.pop(0) if self._codes else 0
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=code == 0,
            content=f"run #{len(self.calls)} output" if code == 0 else "",
            error=None if code == 0 else f"command exited {code}",
            structured={"exit_code": code},
        )


def _last_obs(events: list[Event]) -> ObservationEvent:
    return [e for e in events if isinstance(e, ObservationEvent)][-1]


def test_repeatable_needs_one_agreeing_repeat_and_no_disagreement() -> None:
    one = check_ledger.check_records(with_seqs(_pure("c1", "bash t.sh")))
    fp = one[0].fingerprint
    assert check_ledger.repeatable(fp, one) is False
    two = check_ledger.check_records(with_seqs(_pure("c1", "bash t.sh") + _pure("c2", "bash t.sh")))
    assert check_ledger.repeatable(fp, two) is True
    flaky = check_ledger.check_records(
        with_seqs(_pure("c1", "bash t.sh") + _pure("c2", "bash t.sh", exit_code=1) + _pure("c3", "bash t.sh"))
    )
    assert check_ledger.repeatable(fp, flaky) is False
    assert check_ledger.flaky_fingerprints(flaky) == frozenset({fp})


def test_check_fingerprint_ignores_force() -> None:
    plain = _action("shell", "a", command="ls")
    forced = _action("shell", "b", command="ls", force=True)
    assert check_ledger.check_fingerprint(plain.tool_call) == check_ledger.check_fingerprint(forced.tool_call)
    assert check_ledger.forced(forced) and not check_ledger.forced(plain)


async def test_third_identical_run_is_answered_from_the_record(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_CHECK_MEMO", raising=False)
    sandbox = _Sandbox([DIGEST_A] * 8)
    ex = _SeqExecutor(sandbox, [0, 0, 0, 0])
    loop = _make_loop(ex)
    await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))
    await _drive(loop, _action("shell", "c2", command="bash smoke.sh"))   # first repeat executes
    assert len(ex.calls) == 2
    events = await _drive(loop, _action("shell", "c3", command="bash smoke.sh"))
    assert len(ex.calls) == 2, "the proven-repeatable third run must not execute"

    obs = _last_obs(events)
    assert obs.tool_result.content.startswith("Unchanged since step 4: same command")
    assert "run #2 output" in obs.tool_result.content
    assert obs.tool_result.structured == {"exit_code": 0, "memo_of_seq": 4}
    assert obs.meta["check"]["memo_of"] == 4 and obs.meta["check"]["exit_code"] == 0
    # the memo is the notice: no W-39 reminder follows it
    assert not any(
        e.seq > obs.seq and getattr(e, "message", None) is not None for e in events
    )
    # and the record keeps counting hits without executing
    assert [(r.seq, s.seq) for r, s in check_ledger.memo_hits(events)] == [(4, 2), (obs.seq, 4)]


async def test_force_executes_and_stays_the_same_check(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_CHECK_MEMO", raising=False)
    ex = _SeqExecutor(_Sandbox([DIGEST_A] * 8), [0, 0, 0])
    loop = _make_loop(ex)
    await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))
    await _drive(loop, _action("shell", "c2", command="bash smoke.sh"))
    events = await _drive(loop, _action("shell", "c3", command="bash smoke.sh", force=True))
    assert len(ex.calls) == 3
    records = check_ledger.check_records(events)
    assert len({r.fingerprint for r in records}) == 1


async def test_a_disagreeing_repeat_makes_the_command_flaky_and_never_memoised(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_CHECK_MEMO", raising=False)
    ex = _SeqExecutor(_Sandbox([DIGEST_A] * 8), [0, 1, 0, 0])
    loop = _make_loop(ex)
    await _drive(loop, _action("shell", "c1", command="bash flaky.sh"))
    await _drive(loop, _action("shell", "c2", command="bash flaky.sh"))   # exits 1 on the same source
    await _drive(loop, _action("shell", "c3", command="bash flaky.sh"))
    events = await _drive(loop, _action("shell", "c4", command="bash flaky.sh"))
    assert len(ex.calls) == 4
    statuses = check_ledger.check_statuses(events)
    assert [s.state for s in statuses] == ["flaky"]
    assert "exited differently on unchanged inputs" in statuses[0].render()


async def test_changed_source_or_kill_switch_executes(monkeypatch) -> None:
    monkeypatch.delenv("DISCO_CHECK_MEMO", raising=False)
    ex = _SeqExecutor(_Sandbox([DIGEST_A, DIGEST_A, DIGEST_A, DIGEST_A, DIGEST_B, DIGEST_B]), [0, 0, 0])
    loop = _make_loop(ex)
    await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))
    await _drive(loop, _action("shell", "c2", command="bash smoke.sh"))
    await _drive(loop, _action("shell", "c3", command="bash smoke.sh"))   # digest B now → executes
    assert len(ex.calls) == 3

    monkeypatch.setenv("DISCO_CHECK_MEMO", "off")
    ex2 = _SeqExecutor(_Sandbox([DIGEST_A] * 6), [0, 0, 0])
    loop2 = _make_loop(ex2)
    for cid in ("k1", "k2", "k3"):
        await _drive(loop2, _action("shell", cid, command="bash smoke.sh"))
    assert len(ex2.calls) == 3


# --- phase 4: the finish gate re-runs stale checks ---------------------------------


class _GateExecutor(_ProfileExecutor):
    """file_edit mutates; shell exits with the next code from `codes` (default 0)."""

    def __init__(self, sandbox: _Sandbox | None, codes: list[int] | None = None) -> None:
        super().__init__(sandbox)
        self._codes = list(codes or [])

    async def execute(self, call):
        if call.tool_name == "shell":
            self.calls.append(call)
            code = self._codes.pop(0) if self._codes else 0
            return ToolResult(
                call_id=call.call_id, tool_name="shell", success=code == 0,
                content="24/24 PASS" if code == 0 else "FAIL: search medications",
                error=None if code == 0 else f"command exited {code}",
                structured={"exit_code": code},
            )
        return await super().execute(call)


async def _seed_stale_check(loop) -> list[Event]:
    await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))
    return await _drive(loop, _action("file_edit", "e1", path="src/a.js", old="a", new="b"))


def _shell_calls(ex) -> list:
    return [c for c in ex.calls if c.tool_name == "shell"]


def _gate_messages(events: list[Event]) -> list[str]:
    return [
        e.message.content
        for e in events
        if getattr(e, "meta", {}).get("diagnostic") == "stale_checks_gate"
    ]


async def test_gate_reruns_the_stale_check_as_a_host_probe_and_passes(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CHECK_MEMO", "off")
    ex = _GateExecutor(_Sandbox([DIGEST_A, DIGEST_A] + [DIGEST_B] * 8), codes=[0, 0])  # the edit changes the source
    loop = _make_loop(ex)
    events = await _seed_stale_check(loop)
    assert [s.state for s in check_ledger.check_statuses(events)] == ["stale"]

    assert await loop._finish.stale_checks_gate_passed(events) is True
    events = await loop.store.get_events(CID)
    probe = [e for e in events if isinstance(e, ActionEvent) and e.meta.get("verify_probe")]
    assert len(probe) == 1 and probe[0].tool_call.arguments == {"command": "bash smoke.sh", "force": True}
    assert len(_shell_calls(ex)) == 2  # the original run and the gate's rerun
    assert [s.state for s in check_ledger.check_statuses(events)] == ["current"]
    assert _gate_messages(events) == []


async def test_gate_refuses_when_the_rerun_fails_twice_and_names_the_check(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CHECK_MEMO", "off")
    ex = _GateExecutor(_Sandbox([DIGEST_A, DIGEST_A] + [DIGEST_B] * 10), codes=[0, 1, 1])
    loop = _make_loop(ex)
    events = await _seed_stale_check(loop)

    assert await loop._finish.stale_checks_gate_passed(events) is False
    events = await loop.store.get_events(CID)
    assert len(_shell_calls(ex)) == 3  # original + run + one flake retry
    [message] = _gate_messages(events)
    assert "1 recorded check that passed earlier fail on the current source" in message
    assert "`bash smoke.sh` passed at step 2; now exit 1." in message
    assert "FAIL: search medications" in message
    assert "Finish refused (1 of 3)" in message


async def test_gate_lets_finish_through_on_the_third_refusal(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CHECK_MEMO", "off")
    ex = _GateExecutor(_Sandbox([DIGEST_A, DIGEST_A] + [DIGEST_B] * 28), codes=[0] + [1] * 20)
    loop = _make_loop(ex)
    events = await _seed_stale_check(loop)
    assert await loop._finish.stale_checks_gate_passed(events) is False
    # same source, same recorded probe failure: refused again without re-running
    assert await loop._finish.stale_checks_gate_passed(await loop.store.get_events(CID)) is False
    assert len(_shell_calls(ex)) == 3
    assert await loop._finish.stale_checks_gate_passed(await loop.store.get_events(CID)) is True
    messages = _gate_messages(await loop.store.get_events(CID))
    assert len(messages) == 3 and "refusal 3 of 3" in messages[-1]


async def test_gate_is_a_no_op_without_stale_checks(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CHECK_MEMO", "off")
    ex = _GateExecutor(_Sandbox([DIGEST_A] * 6))
    loop = _make_loop(ex)
    events = await _drive(loop, _action("shell", "c1", command="bash smoke.sh"))   # current, not stale
    assert await loop._finish.stale_checks_gate_passed(events) is True
    assert len(_shell_calls(ex)) == 1
    assert await loop._finish.stale_checks_gate_passed([]) is True


# --- browser step records, the browser line, the finish fact -----------------------------------


class _BrowserExecutor(_ProfileExecutor):
    """browser returns a page observation; shell as before."""

    async def execute(self, call):
        if call.tool_name == "browser":
            self.calls.append(call)
            url = (call.arguments or {}).get("url") or "http://localhost:3000/"
            return ToolResult(
                call_id=call.call_id, tool_name="browser", success=True,
                content=f"[UNTRUSTED WEB CONTENT]\nURL: {url}\nTITLE: Shop\nELEMENTS:\n  1[:] <button>Sign in</button>\n[END]\nscreenshot: .pmx/screenshots/0001.png",
                action_profile=ActionProfile(capabilities=frozenset({EffectCapability.WEB_OBSERVE})),
            )
        return await super().execute(call)


async def test_browser_observation_carries_a_step_record(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CHECK_MEMO", "off")
    loop = _make_loop(_BrowserExecutor(_Sandbox([DIGEST_A] * 4)))
    await _drive(loop, _action("file_edit", "e1", path="src/a.js", old="a", new="b"))
    events = await _drive(loop, _action("browser", "b1", action="navigate", url="http://localhost:3000/"))
    obs = _last_obs(events)
    step = obs.meta["step"]
    assert step["url"] == "http://localhost:3000/" and len(step["dom_hash"]) == 64
    assert step["fingerprint"].startswith("browser:")
    assert step["mutation_seq"] == 2  # the edit's observation


async def test_browser_line_counts_steps_and_identical_repeats_since_the_last_edit(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CHECK_MEMO", "off")
    loop = _make_loop(_BrowserExecutor(_Sandbox([DIGEST_A] * 6)))
    await _drive(loop, _action("file_edit", "e1", path="src/a.js", old="a", new="b"))
    await _drive(loop, _action("browser", "b1", action="navigate", url="http://localhost:3000/"))
    await _drive(loop, _action("browser", "b2", action="click", click_text="Sign in"))
    events = await _drive(loop, _action("browser", "b3", action="navigate", url="http://localhost:3000/"))
    summary = check_ledger.browser_summary(events)
    assert summary is not None and (summary.since_seq, summary.steps, summary.repeated) == (2, 3, 1)
    assert "Browser: 3 steps since the last source change (step 2); 1 repeated an identical earlier step." == summary.render()
    block = check_ledger.render_checks_block(events)
    assert block is not None and block.splitlines()[0] == check_ledger.CHECKS_HEADER and "Browser: 3 steps" in block


def _quiet_events(n_verification: int, *, failed: bool = False) -> list[Event]:
    evs: list[Event] = _edit("e0", "src/a.js") + _pure("c0", "bash smoke.sh", exit_code=1 if failed else 0)
    for i in range(n_verification):
        evs += _pure(f"v{i}", f"curl localhost:3000/api/{i}")
    return with_seqs(evs)


def test_finish_fact_needs_all_current_checks_and_a_quiet_stretch() -> None:
    quiet = _quiet_events(check_ledger.QUIET_ACTIONS)
    fact = check_ledger.finish_fact(quiet, check_ledger.check_statuses(quiet, limit=None))
    assert fact is not None
    assert fact.startswith("No source change since step 2 (13 verification actions since); all 13 recorded checks are current.")
    assert "Nothing is stale for the finish gate to re-run." in fact
    assert fact in (check_ledger.render_checks_block(quiet) or "")

    short = _quiet_events(check_ledger.QUIET_ACTIONS - 2)
    assert check_ledger.finish_fact(short, check_ledger.check_statuses(short, limit=None)) is None

    failing = _quiet_events(check_ledger.QUIET_ACTIONS, failed=True)
    assert check_ledger.finish_fact(failing, check_ledger.check_statuses(failing, limit=None)) is None


def test_finish_fact_resets_after_an_edit() -> None:
    evs = _quiet_events(check_ledger.QUIET_ACTIONS) + with_seqs(_edit("e9", "src/b.js"), start=200)
    statuses = check_ledger.check_statuses(evs, limit=None)
    assert all(s.state == "stale" for s in statuses)
    assert check_ledger.finish_fact(evs, statuses) is None
    since, count = check_ledger.quiet_verification_count(evs)
    assert since == 201 and count == 0


def test_no_checks_and_no_browser_steps_renders_nothing() -> None:
    assert check_ledger.render_checks_block([]) is None
    assert check_ledger.browser_summary([]) is None
