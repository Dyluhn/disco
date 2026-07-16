import asyncio
import urllib.request
import uuid

import pytest
from disco.tools.sandbox import shell_sessions as shell_sessions_module
from disco.tools.sandbox.base import ExecResult
from disco.tools.sandbox.process import ProcessSandboxService
from disco.tools.sandbox.shell_sessions import SessionBusy, ShellSessionManager

_DONE_0 = "__DISCO_DONE_testtoken__0__"
_DONE_7 = "__DISCO_DONE_testtoken__7__"


@pytest.fixture(autouse=True)
def _stable_completion_token(monkeypatch):
    monkeypatch.setattr(shell_sessions_module.secrets, "token_hex", lambda _n: "testtoken")


class FakeInstance:
    def __init__(self):
        self.cmd_log = []
        self.canned_outputs = {}
        self.default_output = ("", "")
        self.default_exit_code = 0
        self.capture_pane_calls = 0

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.cmd_log.append(cmd)

        if "capture-pane" in cmd:
            self.capture_pane_calls += 1
            if "capture-pane" in self.canned_outputs:
                if isinstance(self.canned_outputs["capture-pane"], list):
                    cp = self.canned_outputs["capture-pane"]
                    val = cp.pop(0) if cp else self.canned_outputs["capture-pane_default"]
                    return ExecResult(exit_code=val[0], stdout=val[1], stderr="", timed_out=False)

        for k, v in self.canned_outputs.items():
            if k in cmd and k != "capture-pane":
                if isinstance(v, list):
                    val = v.pop(0) if v else self.canned_outputs.get(f"{k}_default", (0, ""))
                    return ExecResult(exit_code=val[0], stdout=val[1], stderr="", timed_out=False)
                else:
                    exit_code, stdout = v
                    return ExecResult(
                        exit_code=exit_code, stdout=stdout, stderr="", timed_out=False
                    )

        if "capture-pane" in cmd and "capture-pane" in self.canned_outputs:
            val = self.canned_outputs["capture-pane"]
            return ExecResult(exit_code=val[0], stdout=val[1], stderr="", timed_out=False)

        return ExecResult(
            exit_code=self.default_exit_code,
            stdout=self.default_output[1],
            stderr="",
            timed_out=False,
        )


@pytest.mark.asyncio
async def test_marker_parse_exit_0():
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)

    # Fake ensure
    inst.canned_outputs["has-session"] = (0, "")

    # Fake view (busy -> not busy)
    inst.canned_outputs["capture-pane_default"] = (0, "__DISCO_PS1__0__$ ")
    inst.canned_outputs["capture-pane"] = [
        (0, "__DISCO_PS1__0__$ "),  # view
        (0, "__DISCO_PS1__0__$ "),  # view in is_busy
        (0, "__DISCO_PS1__0__$ "),  # pre_cap
        (0, f"__DISCO_PS1__0__$ \necho hi\nhi\n{_DONE_0}\n__DISCO_PS1__0__$ "),
    ]

    view = await manager.view("main")
    assert not view.running

    out = await manager.exec("main", "echo hi", None)
    assert not out.running
    assert out.exit_code == 0
    assert out.output == "hi"


@pytest.mark.asyncio
async def test_marker_parse_exit_7():
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane_default"] = (0, "__DISCO_PS1__0__$ ")
    inst.canned_outputs["capture-pane"] = [
        (0, "__DISCO_PS1__0__$ "),  # view in is_busy
        (0, "__DISCO_PS1__0__$ "),  # pre_cap
        (0, f"__DISCO_PS1__0__$ \nexit 7\n{_DONE_7}\n__DISCO_PS1__7__$ "),
    ]

    out = await manager.exec("main", "exit 7", None)
    assert not out.running
    assert out.exit_code == 7
    assert out.output == ""


@pytest.mark.asyncio
async def test_fresh_prompt_before_background_stderr_proves_shell_idle() -> None:
    """H322: late child stderr cannot hide the fresh post-dispatch prompt."""

    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    inst.canned_outputs["has-session"] = (0, "")
    prompt = "__DISCO_PS1__0__$ "
    late = (
        prompt
        + "\npython3 /workspace/server.py &\n[1] 444\n"
        + _DONE_0
        + "\n"
        + prompt
        + "Traceback (most recent call last):\nOSError: [Errno 98] Address already in use"
    )
    inst.canned_outputs["capture-pane_default"] = (0, late)
    inst.canned_outputs["capture-pane"] = [
        (0, prompt),  # is_busy
        (0, prompt),  # pre-dispatch capture
        (0, late),  # fresh prompt followed inline by background stderr
    ]

    out = await manager.exec("server", "python3 /workspace/server.py &", None)

    assert out.running is False
    assert out.exit_code == 0
    assert "Address already in use" in out.output
    assert "__DISCO_PS1__" not in out.output
    assert out.note is not None and "background process status is unverified" in out.note
    assert not await manager.is_busy("server")

    before = list(inst.cmd_log)
    killed = await manager.kill_foreground("server")
    assert killed == "Session 'server' is already idle; no signal sent."
    assert not any(" C-c" in command for command in inst.cmd_log[len(before) :])

    inst.canned_outputs["capture-pane"] = [
        (0, late),  # cached idle is accepted for is_busy
        (0, late),  # next command's pre-dispatch capture
        (0, late + "\necho ok\nok\n" + _DONE_0 + "\n" + prompt),
    ]
    next_out = await manager.exec("server", "echo ok", None)
    assert next_out.running is False
    assert next_out.output == "ok"


@pytest.mark.asyncio
async def test_old_prompt_before_new_foreground_command_does_not_false_idle(monkeypatch) -> None:
    """Only a marker in the current exec delta is proof; scrollback is not."""

    from disco.tools.sandbox import shell_sessions

    monkeypatch.setattr(shell_sessions, "_EXEC_WAIT_S", 0.05)
    monkeypatch.setattr(shell_sessions, "_POLL_S", 0.01)
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    prompt = "__DISCO_PS1__0__$ "
    running = prompt + "\nsleep 10\n" + prompt + "\nworking"
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane_default"] = (0, running)
    inst.canned_outputs["capture-pane"] = [
        (0, prompt),  # is_busy
        (0, prompt),  # pre-dispatch capture
        (0, running),  # old prompt is entirely in pre; delta has no fresh prompt
    ]

    out = await manager.exec("main", "sleep 10", None)

    assert out.running is True
    assert await manager.is_busy("main")


@pytest.mark.asyncio
async def test_prefix_mismatch_does_not_promote_old_prompt_to_fresh(monkeypatch) -> None:
    """Pane truncation/resize loses continuity; only a terminal marker is safe then."""

    from disco.tools.sandbox import shell_sessions

    monkeypatch.setattr(shell_sessions, "_EXEC_WAIT_S", 0.05)
    monkeypatch.setattr(shell_sessions, "_POLL_S", 0.01)
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    prompt = "__DISCO_PS1__0__$ "
    pre = prompt + "\nold pane history"
    mismatched = "truncated history\n" + prompt + "sleep 10\nworking"
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane_default"] = (0, mismatched)
    inst.canned_outputs["capture-pane"] = [
        (0, prompt),
        (0, pre),
        (0, mismatched),
    ]

    out = await manager.exec("main", "sleep 10", None)

    assert out.running is True
    assert manager._foreground_state["main"] == "busy"


@pytest.mark.asyncio
async def test_dispatch_failure_clears_prior_foreground_proof(monkeypatch) -> None:
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    manager._foreground_state["main"] = "idle"
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane"] = [
        (0, "background stderr"),  # cached idle is_busy
        (0, "background stderr"),  # pre-dispatch capture
    ]
    original_run_tmux = manager._run_tmux

    async def _fail_send(command: str) -> str:
        if "send-keys" in command:
            raise RuntimeError("send failed")
        return await original_run_tmux(command)

    monkeypatch.setattr(manager, "_run_tmux", _fail_send)

    with pytest.raises(RuntimeError, match="send failed"):
        await manager.exec("main", "echo no", None)

    assert "main" not in manager._foreground_state


@pytest.mark.asyncio
async def test_exec_enter_failure_clears_proof_and_blocks_command_concatenation(
    monkeypatch,
) -> None:
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    manager._foreground_state["main"] = "idle"
    prompt_with_text = "__DISCO_PS1__0__$ echo first"
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane_default"] = (0, prompt_with_text)
    inst.canned_outputs["capture-pane"] = [
        (0, "__DISCO_PS1__0__$ "),
        (0, "__DISCO_PS1__0__$ "),
    ]
    original_run_tmux = manager._run_tmux

    async def _fail_enter(command: str) -> str:
        if command.endswith(" Enter"):
            raise RuntimeError("enter failed")
        return await original_run_tmux(command)

    monkeypatch.setattr(manager, "_run_tmux", _fail_enter)

    with pytest.raises(RuntimeError, match="enter failed"):
        await manager.exec("main", "echo first", None)

    assert "main" not in manager._foreground_state
    inst.canned_outputs["capture-pane"] = [(0, prompt_with_text)]
    with pytest.raises(SessionBusy):
        await manager.exec("main", "echo second", None)


@pytest.mark.asyncio
async def test_write_enter_failure_does_not_claim_foreground_busy(monkeypatch) -> None:
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    manager._foreground_state["main"] = "idle"
    inst.canned_outputs["has-session"] = (0, "")
    original_run_tmux = manager._run_tmux

    async def _fail_enter(command: str) -> str:
        if command.endswith(" Enter"):
            raise RuntimeError("enter failed")
        return await original_run_tmux(command)

    monkeypatch.setattr(manager, "_run_tmux", _fail_enter)

    with pytest.raises(RuntimeError, match="enter failed"):
        await manager.write("main", "echo no", press_enter=True)

    assert "main" not in manager._foreground_state
    inst.canned_outputs["capture-pane"] = [(0, "__DISCO_PS1__0__$ echo no")]
    with pytest.raises(SessionBusy):
        await manager.exec("main", "echo second", None)


@pytest.mark.asyncio
async def test_write_without_enter_clears_idle_proof_and_blocks_exec() -> None:
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    manager._foreground_state["main"] = "idle"
    inst.canned_outputs["has-session"] = (0, "")

    await manager.write("main", "echo pending", press_enter=False)

    assert "main" not in manager._foreground_state
    inst.canned_outputs["capture-pane"] = [(0, "__DISCO_PS1__0__$ echo pending")]
    with pytest.raises(SessionBusy):
        await manager.exec("main", "echo second", None)


@pytest.mark.asyncio
async def test_write_first_send_failure_clears_idle_proof(monkeypatch) -> None:
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    manager._foreground_state["main"] = "idle"
    inst.canned_outputs["has-session"] = (0, "")

    async def _fail_send(_command: str) -> str:
        raise RuntimeError("transport uncertain")

    monkeypatch.setattr(manager, "_run_tmux", _fail_send)

    with pytest.raises(RuntimeError, match="transport uncertain"):
        await manager.write("main", "echo maybe", press_enter=False)

    assert "main" not in manager._foreground_state


@pytest.mark.asyncio
async def test_unknown_idle_kill_preflight_sends_no_signal() -> None:
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    prompt = "__DISCO_PS1__0__$ "
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane"] = [(0, prompt)]

    result = await manager.kill_foreground("main")

    assert result == "Session 'main' is already idle; no signal sent."
    assert not any(" C-c" in command for command in inst.cmd_log)


def test_reset_known_sessions_clears_proven_foreground_state() -> None:
    async def _unused_instance():
        raise AssertionError("instance access is not expected")

    manager = ShellSessionManager(_unused_instance)
    manager._foreground_state["main"] = "idle"

    manager.reset_known_sessions()

    assert manager._foreground_state == {}


def test_background_parser_distinguishes_control_from_redirection() -> None:
    assert ShellSessionManager._backgrounded("vite --port 5173 &") is True
    assert ShellSessionManager._backgrounded("vite --port 5173&") is True
    assert ShellSessionManager._backgrounded("python check.py 2>&1") is False
    assert ShellSessionManager._backgrounded("python check.py &>output.log") is False
    assert ShellSessionManager._backgrounded("cd /workspace && python check.py") is False
    assert ShellSessionManager._backgrounded("python check.py '&'") is False
    assert ShellSessionManager._backgrounded('python check.py "&"') is False
    assert ShellSessionManager._backgrounded(r"python check.py \&") is False
    assert ShellSessionManager._backgrounded("python check.py |& tee output.log") is False
    assert ShellSessionManager._backgrounded("echo ok # R&D") is False
    assert ShellSessionManager._backgrounded("echo R&D") is True


def test_private_completion_marker_is_not_literal_in_dispatched_command() -> None:
    dispatched, completion_re = ShellSessionManager._completion_dispatch("echo hi", "testtoken")

    assert "__DISCO_DONE_testtoken__" not in dispatched
    assert completion_re.search("\n__DISCO_DONE_testtoken__7__\n") is not None
    assert completion_re.search("echo __DISCO_DONE_testtoken__7__") is None


@pytest.mark.asyncio
async def test_busy_detection():
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane"] = (0, "sleep 10\n")

    view = await manager.view("main")
    assert view.running

    # test SessionBusy verbatim message
    with pytest.raises(
        SessionBusy,
        match=(
            "Previous command not finished in session 'main'. Wait for it "
            "\\(shell_wait\\), interact with it \\(shell_write_to_process\\), "
            "kill it \\(shell_kill_process\\), or use a different session name."
        ),
    ):
        await manager.exec("main", "ls", None)


@pytest.mark.asyncio
async def test_kill_then_recreate():
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    inst.canned_outputs["has-session"] = [(0, ""), (1, "")]
    inst.canned_outputs["has-session_default"] = (0, "")
    inst.canned_outputs["capture-pane"] = (0, "sleep 10\n")

    res = await manager.kill_foreground("main")
    assert "Process ignored Ctrl-C; session 'main' was killed and recreated." in res
    assert "tmux kill-session" in " ".join(inst.cmd_log)
    assert "tmux new-session" in " ".join(inst.cmd_log)


@pytest.mark.asyncio
async def test_session_lost_after_recreate():
    """After a sandbox recreate, viewing a previously-known session must say WHY
    it is gone — not the generic 'not found'."""
    inst = FakeInstance()

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)

    # Session 'main' becomes known (idle prompt visible).
    inst.canned_outputs["has-session"] = (1, "")
    inst.canned_outputs["capture-pane"] = (0, "__DISCO_PS1__0__$ ")
    await manager.ensure("main")
    assert "main" in manager._known_sessions

    # Sandbox dies and is recreated -> manager is told; tmux on the new box
    # has no such session (capture-pane now fails).
    manager.reset_known_sessions()
    inst.canned_outputs["capture-pane"] = (1, "can't find session")

    view = await manager.view("main")
    assert view.running is False
    assert view.output == "session lost: sandbox was recreated"

    # A session that was NEVER known still gets the generic message.
    view2 = await manager.view("other")
    assert "Session not found or error" in view2.output


@pytest.mark.asyncio
async def test_process_session_creation_binds_capability_environment():
    inst = FakeInstance()
    inst.workspace_path = "/tmp/disco-conversation-a"
    inst.canned_outputs["has-session"] = (1, "")
    inst.canned_outputs["capture-pane"] = (0, "__DISCO_PS1__0__$ ")

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst, namespace="conv-a-")
    await manager.ensure("main")

    create = next(cmd for cmd in inst.cmd_log if "tmux new-session" in cmd)
    assert "-e PATH=/usr/local/bin:/usr/bin:/bin" in create
    for key in ("HOME", "TMPDIR", "DISCO_WORKSPACE"):
        assert f"-e {key}=/tmp/disco-conversation-a" in create


@pytest.mark.asyncio
async def test_existing_process_session_refreshes_tmux_environment():
    inst = FakeInstance()
    inst.workspace_path = "/tmp/disco-conversation-b"
    inst.canned_outputs["has-session"] = (0, "")

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst, namespace="conv-b-")
    await manager.ensure("main")

    refresh = "\n".join(cmd for cmd in inst.cmd_log if "set-environment" in cmd)
    assert "DISCO_WORKSPACE /tmp/disco-conversation-b" in refresh
    assert "HOME /tmp/disco-conversation-b" in refresh
    assert "TMPDIR /tmp/disco-conversation-b" in refresh


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_scenarios():
    service = ProcessSandboxService()
    inst = await service.create(spec=None, owner_id="test", conversation_id="conv-int")

    try:

        async def get_inst():
            return inst

        manager = ShellSessionManager(get_inst, namespace="conv-int-")

        # a) state persists
        out1 = await manager.exec("main", "x=42; echo started", None)
        assert not out1.running

        out2 = await manager.exec("main", "echo $x", None)
        assert not out2.running
        assert out2.output.strip() == "42"

        # b) start server, curl, kill
        out_srv = await manager.exec("srv", "python3 -m http.server 8123", None)
        assert out_srv.running

        # give it a second to start
        await asyncio.sleep(1)

        view_srv = await manager.view("srv")
        assert "Serving HTTP" in view_srv.output

        # verify from test
        req = urllib.request.Request("http://127.0.0.1:8123")
        with urllib.request.urlopen(req) as response:
            assert response.status == 200

        # actually curl 127.0.0.1:8123
        kill_res = await manager.kill_foreground("srv")
        assert "idle" in kill_res or "killed" in kill_res

        # c) busy session
        out_read = await manager.exec("main", "read -p 'name? ' n && echo hi-$n", None)
        assert out_read.running

        await manager.write("main", "dylan", press_enter=True)

        # Wait for it to finish
        view_read = await manager.wait("main", 5)
        assert not view_read.running
        assert "hi-dylan" in view_read.output

        # d) SessionBusy
        out_busy = await manager.exec("main2", "sleep 20", None)
        assert out_busy.running

        with pytest.raises(SessionBusy):
            await manager.exec("main2", "echo nope", None)

    finally:
        await inst.destroy()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shared_tmux_server_cannot_cross_bind_process_workspaces():
    """A stale host-global tmux env never overrides per-conversation capability."""
    service = ProcessSandboxService()
    conv_a = "enva" + uuid.uuid4().hex
    conv_b = "envb" + uuid.uuid4().hex
    inst_a = await service.create(spec=None, owner_id="test", conversation_id=conv_a)
    inst_b = await service.create(spec=None, owner_id="test", conversation_id=conv_b)
    socket_name = "disco-env-" + uuid.uuid4().hex

    class IsolatedTmuxManager(ShellSessionManager):
        async def _run_tmux(self, cmd: str) -> str:
            inst = await self._get_instance()
            res = await inst.exec_shell(f"tmux -L {socket_name} {cmd}", timeout_s=10)
            if res.exit_code != 0:
                raise RuntimeError(res.stderr)
            return res.stdout

        async def _run_tmux_safe(self, cmd: str) -> tuple[int, str]:
            inst = await self._get_instance()
            res = await inst.exec_shell(f"tmux -L {socket_name} {cmd}", timeout_s=10)
            return res.exit_code, res.stdout

    async def get_a():
        return inst_a

    async def get_b():
        return inst_b

    try:
        stale = await inst_a.exec_shell(
            "env HOME=/tmp/stale-home TMPDIR=/tmp/stale-tmp "
            "DISCO_WORKSPACE=/tmp/stale-workspace "
            f"tmux -L {socket_name} new-session -d -s stale-keeper",
            timeout_s=10,
        )
        assert stale.exit_code == 0, stale.stderr

        manager_a = IsolatedTmuxManager(get_a, namespace=f"{conv_a[:8]}-")
        manager_b = IsolatedTmuxManager(get_b, namespace=f"{conv_b[:8]}-")
        command = 'printf \'%s|%s|%s\\n\' "$HOME" "$TMPDIR" "$DISCO_WORKSPACE"'
        out_a = await manager_a.exec("main", command, None)
        out_b = await manager_b.exec("main", command, None)

        expected_a = "|".join([inst_a.workspace_path] * 3)
        expected_b = "|".join([inst_b.workspace_path] * 3)
        assert expected_a in out_a.output
        assert expected_b in out_b.output
        assert expected_b not in out_a.output
        assert expected_a not in out_b.output
        assert "/tmp/stale-" not in out_a.output + out_b.output
    finally:
        await inst_a.exec_shell(f"tmux -L {socket_name} kill-server", timeout_s=10)
        await inst_a.destroy()
        await inst_b.destroy()


# ---- Bug 16: reserved-port preview-serve remap at the CLEAN-command point ----


def _serve_canned(inst):
    """Drive exec() through: ensure (has-session ok) → is_busy view → pre_cap →
    post_cap-with-marker, so a single exec() completes deterministically."""
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane_default"] = (0, "__DISCO_PS1__0__$ ")
    inst.canned_outputs["capture-pane"] = [
        (0, "__DISCO_PS1__0__$ "),  # is_busy view
        (0, "__DISCO_PS1__0__$ "),  # pre_cap
        (0, f"__DISCO_PS1__0__$ \nserving\n{_DONE_0}\n__DISCO_PS1__0__$ "),
    ]


def _sent_literals(inst):
    """The `tmux send-keys ... -l '<command>'` literals exec() issued."""
    return " ".join(c for c in inst.cmd_log if "send-keys" in c and " -l " in c)


@pytest.mark.asyncio
async def test_exec_remaps_reserved_preview_serve_on_shared_host():
    # Bug 16 review #2: the remap fires on the model's CLEAN shell_exec command (here,
    # before exec() wraps it into `tmux send-keys -l '...'`). On a SHARED-host backend
    # (workspace_path set ⇒ process/local, where 8000 is the agent-server's control port)
    # a leading `python -m http.server 8000` is remapped to the process-safe port — so the
    # served port is conversation-owned + verifiable, and 8000/5173 is never wrapped/run.
    from disco.core.loop.preview_target import (
        process_safe_preview_port,
        reserved_control_ports,
    )

    safe = str(process_safe_preview_port(reserved=reserved_control_ports()))
    inst = FakeInstance()
    inst.workspace_path = "/tmp/ws"  # shared-host (process) signal

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst, namespace="conv_abc-")
    _serve_canned(inst)

    await manager.exec("preview", "python3 -m http.server 8000", None)

    sent = _sent_literals(inst)
    assert f"http.server {safe}" in sent, inst.cmd_log
    assert "http.server 8000" not in sent


@pytest.mark.asyncio
async def test_exec_keeps_8000_canonical_on_isolated_backend():
    # Must-not-regress: an ISOLATED container (no workspace_path) keeps 8000 as its
    # canonical app port — the remap must NOT fire there.
    inst = FakeInstance()  # no workspace_path ⇒ isolated

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst)
    _serve_canned(inst)

    await manager.exec("preview", "python3 -m http.server 8000", None)

    sent = _sent_literals(inst)
    assert "http.server 8000" in sent  # canonical inside the box — unchanged


@pytest.mark.asyncio
async def test_exec_never_rewrites_serve_shaped_text_on_shared_host():
    # Bug-16 review #2 bypasses: even on a shared host, serve-shaped TEXT that does not
    # BEGIN with a real `python -m http.server` invocation (a separator inside quotes, an
    # echo argument) is run VERBATIM — never port-rewritten.
    inst = FakeInstance()
    inst.workspace_path = "/tmp/ws"

    async def get_inst():
        return inst

    manager = ShellSessionManager(get_inst, namespace="conv_abc-")

    for command in (
        "echo '; python3 -m http.server 8000'",
        "echo python3 -m http.server 8000",
        "cd build && python3 -m http.server 8000",
    ):
        inst.cmd_log.clear()
        _serve_canned(inst)
        await manager.exec("main", command, None)
        sent = _sent_literals(inst)
        assert "http.server 8000" in sent, command  # verbatim
        assert "http.server 3000" not in sent, command
