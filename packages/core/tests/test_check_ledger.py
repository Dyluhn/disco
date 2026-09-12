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
