"""Sandbox spec/instance/backend (§11.3) + file-tool jailing."""

from __future__ import annotations

import asyncio
import shutil
import uuid

import pytest
from disco.tools import (
    DefaultToolExecutor,
    ProcessSandboxService,
    SandboxError,
    SandboxSession,
    SandboxSpec,
    ToolScope,
    build_default_registry,
)
from tool_fakes import FakeSandboxInstance, call


async def test_spec_to_instance_carries_ownership():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="alice", conversation_id="c1")
    assert inst.owner_id == "alice" and inst.conversation_id == "c1"
    assert await svc.get(inst.id) is inst
    await inst.destroy()


async def test_lifecycle_destroy_rejects_further_calls():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    await inst.write_file("a.txt", b"x")
    await inst.destroy()
    with pytest.raises(SandboxError):
        await inst.read_file("a.txt")


async def _tmux(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8")


@pytest.mark.integration
async def test_process_delete_cleans_only_exact_conversation_tmux_sessions():
    """A delete that arrives after runtime restart has only a conversation id.  Prove
    the process service can recover both current and legacy session namespaces from that
    id without killing another conversation's host-global tmux sessions."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real process-backend cleanup boundary")

    cid = uuid.uuid4().hex
    namespace = cid[:8]
    current = f"disco-{namespace}-preview"
    legacy = f"pmx-{namespace}-legacy"
    unrelated = f"disco-{uuid.uuid4().hex[:8]}-keep"
    names = (current, legacy, unrelated)
    try:
        for name in names:
            code, _ = await _tmux("new-session", "-d", "-s", name)
            assert code == 0

        await ProcessSandboxService().destroy_by_conversation(cid)

        code, output = await _tmux("list-sessions", "-F", "#{session_name}")
        assert code == 0
        remaining = set(output.splitlines())
        assert current not in remaining
        assert legacy not in remaining
        assert unrelated in remaining
    finally:
        for name in names:
            await _tmux("kill-session", "-t", name)


async def test_file_round_trip_and_escape_rejection():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    await inst.write_file("sub/data.txt", b"payload")
    assert await inst.read_file("sub/data.txt") == b"payload"
    # Path-escape attempts are rejected (workspace jail).
    with pytest.raises(SandboxError):
        await inst.read_file("../../etc/passwd")
    with pytest.raises(SandboxError):
        await inst.write_file("/etc/evil", b"x")
    await inst.destroy()


async def test_process_shell_resolves_workspace_prefix():
    """ROOT-1 (slides spiral): on the process backend the shell `cwd` is the real
    per-instance temp dir, which has no literal `/workspace`. exec_shell must rewrite
    a genuine `/workspace` path token to the real dir so `ls`/`cat /workspace/X` work
    exactly like the container backends — while a NEW relative path still resolves via
    cwd and a longer name like `/workspaces` is NOT touched."""
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    await inst.write_file("deck.pptx", b"DECKBYTES")

    # Absolute /workspace path resolves to the real workspace file.
    res = await inst.exec_shell("cat /workspace/deck.pptx", timeout_s=10)
    assert res.exit_code == 0
    assert res.stdout == "DECKBYTES"

    # `ls /workspace` lists the workspace contents.
    res = await inst.exec_shell("ls /workspace", timeout_s=10)
    assert res.exit_code == 0
    assert "deck.pptx" in res.stdout

    # A NEW-file RELATIVE path still works via cwd (no rewrite needed).
    res = await inst.exec_shell("echo hi > new.txt && cat new.txt", timeout_s=10)
    assert res.exit_code == 0
    assert res.stdout.strip() == "hi"
    assert await inst.read_file("new.txt") == b"hi\n"

    # A longer name (`/workspaces`) is NOT a `/workspace` token → left alone (so it
    # does not resolve to the real dir and the `cat` genuinely fails to find it).
    res = await inst.exec_shell("cat /workspaces/deck.pptx", timeout_s=10)
    assert res.exit_code != 0

    await inst.destroy()


async def test_process_shell_resolves_workspace_path_with_escaped_space():
    """A shell-escaped space remains part of one jailed /workspace token."""
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c-space")
    await inst.write_file("media/hero image.svg", b"SVG")

    res = await inst.exec_shell(r"cat /workspace/media/hero\ image.svg", timeout_s=10)
    assert res.exit_code == 0
    assert res.stdout == "SVG"
    await inst.destroy()


async def test_process_child_receives_real_workspace_capability_path():
    """Browser helpers write artifacts inside the jail, not literal /workspace."""
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c-browser-env")
    command = (
        "python3 -c 'import os,pathlib; "
        "pathlib.Path(os.environ[\"DISCO_WORKSPACE\"], \".pmx\", \"browser-probe\")"
        ".parent.mkdir(parents=True, exist_ok=True); "
        "pathlib.Path(os.environ[\"DISCO_WORKSPACE\"], \".pmx\", \"browser-probe\")"
        ".write_text(\"ok\")'"
    )
    res = await inst.exec_shell(command, timeout_s=10)
    assert res.exit_code == 0, res.stderr
    assert await inst.read_file(".pmx/browser-probe") == b"ok"
    await inst.destroy()


async def test_process_exec_shell_cancellation_reaps_command_group():
    """Cancelling a sandbox command leaves no helper/shell descendants."""
    import asyncio
    import os

    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c-cancel")
    task = asyncio.create_task(
        inst.exec_shell("echo $$ > worker.pid; sleep 60", timeout_s=60)
    )
    for _ in range(100):
        if await inst.file_exists("worker.pid"):
            break
        await asyncio.sleep(0.01)
    assert await inst.file_exists("worker.pid")
    worker_pid = int((await inst.read_file("worker.pid")).decode().strip())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail(f"cancelled sandbox worker {worker_pid} is still alive")
    await inst.destroy()


async def test_process_shell_workspace_rewrite_rejects_traversal_escape():
    """ROOT-1 P1 (security): the /workspace shell rewrite must NOT be the thing that
    grants a path-traversal escape. A `/workspace/..`-rooted token that resolves
    OUTSIDE the workspace fails closed (SandboxPermissionError) — exactly like the
    file tools' jailed /workspace — instead of being rewritten to an out-of-jail
    absolute path."""
    from disco.tools.sandbox.base import SandboxPermissionError

    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")

    # Classic traversal: /workspace/../../../etc/passwd must be rejected, NOT rewritten
    # to <realdir>/../../../etc/passwd (which would resolve to /etc/passwd).
    with pytest.raises(SandboxPermissionError):
        await inst.exec_shell("cat /workspace/../../../etc/passwd", timeout_s=10)
    # A single-level escape out of the workspace is likewise rejected.
    with pytest.raises(SandboxPermissionError):
        await inst.exec_shell("cat /workspace/../outside", timeout_s=10)

    # Sanity: a NON-escaping /workspace path with a subdir still rewrites + works.
    await inst.write_file("sub/keep.txt", b"OK")
    res = await inst.exec_shell("cat /workspace/sub/keep.txt", timeout_s=10)
    assert res.exit_code == 0 and res.stdout == "OK"

    await inst.destroy()


async def test_process_file_exists_present_absent_and_escape():
    """B4 — the process backend's `file_exists` is a workspace-jailed existence
    check: True for a real file, False for a missing one, and False (never
    raised) for a path that escapes the workspace jail."""
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    await inst.write_file("dir/present.txt", b"x")
    assert await inst.file_exists("dir/present.txt") is True
    assert await inst.file_exists("dir/absent.txt") is False
    # A jail escape is False, not a raise (the predicate named an out-of-scope
    # path, which simply does not exist *in* the workspace).
    assert await inst.file_exists("../../etc/passwd") is False
    await inst.destroy()
    # After destroy the instance rejects further calls (lifecycle contract).
    with pytest.raises(SandboxError):
        await inst.file_exists("dir/present.txt")


async def test_session_file_exists_delegates_to_instance():
    """B4 — SandboxSession.file_exists delegates to the live instance (drop-in
    SandboxInstance), resolving in the instance's own namespace."""
    svc = ProcessSandboxService()
    session = SandboxSession(svc, owner_id="local", conversation_id="c-fe")
    await session.write_file("made.txt", b"y")
    assert await session.file_exists("made.txt") is True
    assert await session.file_exists("nope.txt") is False
    await session.destroy()


async def test_session_destroy_shuts_down_cached_kernel():
    """The session owns and closes its persistent CodeAct kernel."""

    class _Kernel:
        shutdown_called = False

        async def shutdown(self) -> None:
            self.shutdown_called = True

    session = SandboxSession(
        ProcessSandboxService(), owner_id="local", conversation_id="c-kernel-close"
    )
    kernel = _Kernel()
    session._kernel = kernel

    await session.destroy()

    assert kernel.shutdown_called is True
    assert session._kernel is None


async def test_backend_swap_identical_results_process_vs_fake():
    """The same file tool yields identical observable results on the process
    backend and an in-memory fake — proving no backend leakage (§11.3)."""

    async def round_trip(sandbox) -> str:
        reg = build_default_registry()
        ex = DefaultToolExecutor(
            reg, ToolScope(allowed_tools=frozenset({"file_write", "file_read"})), sandbox=sandbox
        )
        await ex.execute(call("file_write", path="f.txt", content="same-bytes"))
        res = await ex.execute(call("file_read", path="f.txt"))
        return res.content

    svc = ProcessSandboxService()
    proc = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    a, b = await round_trip(proc), await round_trip(FakeSandboxInstance())
    assert a == b and "same-bytes" in a  # identical across backends (numbered read)
    await proc.destroy()


async def test_shell_and_code_exec_run_in_process_sandbox():
    svc = ProcessSandboxService()
    session = SandboxSession(svc, owner_id="local", conversation_id="c")
    reg = build_default_registry()
    ex = DefaultToolExecutor(
        reg, ToolScope(allowed_tools=frozenset({"shell", "code_exec"})), sandbox=session
    )
    sh = await ex.execute(call("shell", command="echo hi"))
    assert sh.success and "hi" in sh.content
    code = await ex.execute(call("code_exec", language="python", code="print(6*7)"))
    assert code.success and "42" in code.content
    await session.destroy()


async def test_process_backend_expose_port_defense():
    # BP-10: process backend expose_port stays within USER_PORTS and checks binding
    import socket
    import tempfile
    from pathlib import Path

    import pytest
    from disco.core.loop.preview_target import reserved_control_ports
    from disco.tools.sandbox._container import USER_PORTS
    from disco.tools.sandbox.process import ProcessSandboxInstance

    def _bound(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            return False

    unbound = next(
        (port for port in sorted(USER_PORTS - reserved_control_ports()) if not _bound(port)),
        None,
    )
    if unbound is None:
        pytest.skip("all non-reserved user preview ports are already bound on this host")

    with tempfile.TemporaryDirectory() as tmp:
        inst = ProcessSandboxInstance("i", "o", "c", SandboxSpec(), Path(tmp))

        # 1. Not in USER_PORTS -> None
        assert inst.expose_port(9999) is None

        # 2. In USER_PORTS but not bound -> None
        assert inst.expose_port(unbound) is None

        # 3. INTERNAL_PORTS (8899) -> None
        assert inst.expose_port(8899) is None

        await inst.destroy()


async def test_process_exec_shell_does_not_rewrite_arbitrary_wrapped_strings(monkeypatch):
    # Bug-16 review #2: process.exec_shell receives ALREADY-WRAPPED / arbitrary shell
    # (e.g. the `tmux send-keys -l '...'` wrapper) and must NEVER rewrite it — the remap
    # now lives upstream on the CLEAN command (ShellSessionManager.exec). Here we prove
    # exec_shell launches the wrapped serve string VERBATIM (no port rewrite), spying on
    # the subprocess launcher so nothing is actually bound.
    import asyncio
    import tempfile
    from pathlib import Path

    from disco.tools.sandbox.process import ProcessSandboxInstance

    captured: list[tuple[str, ...]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_create(*argv, **kwargs):
        captured.append(argv)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)

    with tempfile.TemporaryDirectory() as tmp:
        inst = ProcessSandboxInstance("i", "o", "c", SandboxSpec(), Path(tmp))
        # A serve form buried INSIDE a tmux send-keys literal is NOT a reserved-port BIND
        # the containment scan flags (the bind regex keys on `http.server <reserved>`,
        # which IS present here) — so this particular wrapper would actually be refused by
        # containment. Use a wrapper around an ALREADY-SAFE port to prove the no-rewrite:
        # it passes containment and is launched byte-for-byte, never port-rewritten.
        captured.clear()
        wrapped = "tmux send-keys -t disco-c-preview -l 'python3 -m http.server 3000'"
        await inst.exec_shell(wrapped, timeout_s=10)
        assert len(captured) == 1
        assert captured[0][4] == wrapped  # helper receives it verbatim; no port rewrite


async def test_process_exec_shell_still_refuses_reserved_kill_and_arbitrary_bind():
    # Must-not-regress: a reserved-port KILL and an arbitrary reserved BIND are STILL
    # refused (exit 126) with the actionable message — the refusal returns BEFORE the
    # real subprocess launcher. (The recovery REMAP lives upstream on the clean command;
    # this containment net only ever REJECTS, never rewrites.)
    import tempfile
    from pathlib import Path

    from disco.core.loop.preview_target import (
        process_safe_preview_port,
        reserved_control_ports,
    )
    from disco.tools.sandbox.process import ProcessSandboxInstance

    safe = str(process_safe_preview_port(reserved=reserved_control_ports()))
    with tempfile.TemporaryDirectory() as tmp:
        inst = ProcessSandboxInstance("i", "o", "c", SandboxSpec(), Path(tmp))
        for cmd in ("fuser -k 8000/tcp", "uvicorn app:app --port 8000"):
            res = await inst.exec_shell(cmd, timeout_s=10)
            assert res.exit_code == 126, cmd
            assert res.stderr.startswith("refused:"), cmd
            assert safe in res.stderr  # actionable: names a safe replacement
        await inst.destroy()


async def test_process_exec_shell_refuses_host_signal_commands(monkeypatch):
    # Bug 19 (P0): a process-backend build must NOT be able to signal/kill a HOST process
    # (it shares the host PID namespace — a `kill <pid>` of a discovered PID took down the
    # agent-server LIVE). Every host-signal shape is refused (exit 126, actionable message)
    # and the real subprocess launcher is NEVER invoked for a refused command.
    import asyncio
    import tempfile
    from pathlib import Path

    from disco.tools.sandbox.process import ProcessSandboxInstance

    launched: list[str] = []

    async def _fake_create(cmd, **kwargs):  # pragma: no cover — must NOT be reached
        launched.append(cmd)
        raise AssertionError(f"launcher invoked for a refused command: {cmd!r}")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create)

    # Host-signal shapes the NEW Bug-19 net catches → the host-signal refusal message.
    host_signal = (
        "kill 12345",
        "kill -9 12345",
        "kill -TERM 12345",
        "kill -s TERM 12345",
        "pkill -f uvicorn",
        "killall python",
        "fuser -k 9001/tcp",  # non-reserved port — still a host-process kill
        "lsof -ti:9001 | xargs -r kill",  # non-reserved + xargs -r form
        "ss -lntp ; kill 931479",  # the exact live takedown shape (kill after a separator)
    )
    # Reserved-port kill shapes — refused EARLIER by the Bug-7/16 port check (its own
    # port-specific message). Still exit 126, still never launched. Kept here to prove
    # the additional Bug-19 net does not weaken the existing reserved-port refusal.
    reserved_port_kill = (
        "fuser -k 8000/tcp",
        "lsof -ti:8000 | xargs kill",
    )
    with tempfile.TemporaryDirectory() as tmp:
        inst = ProcessSandboxInstance("i", "o", "c", SandboxSpec(), Path(tmp))
        for cmd in host_signal:
            res = await inst.exec_shell(cmd, timeout_s=10)
            assert res.exit_code == 126, cmd
            # starts with "refused:" so system.py surfaces it as the model-visible error
            assert res.stderr.startswith("refused:"), cmd
            assert "never" in res.stderr.lower() and "8080" in res.stderr, cmd
        for cmd in reserved_port_kill:
            res = await inst.exec_shell(cmd, timeout_s=10)
            assert res.exit_code == 126, cmd
            assert res.stderr.startswith("refused:"), cmd
        assert launched == []  # the launcher was never reached for any refused command
        # NB: no inst.destroy() here — destroy() itself uses the (patched) launcher for
        # tmux cleanup; the TemporaryDirectory removes the workspace.


async def test_process_exec_shell_allows_normal_build_commands(monkeypatch):
    # Must-not-regress: normal build commands STILL reach the launcher — only host-signal
    # shapes are refused. `kill`/`pkill`/`killall` must NOT match as substrings of harmless
    # commands (`pytest -k kill_switch`, `echo "kill the build"`).
    import asyncio
    import tempfile
    from pathlib import Path

    from disco.tools.sandbox.process import ProcessSandboxInstance

    captured: list[tuple[str, ...]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_create(*argv, **kwargs):
        captured.append(argv)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)

    controls = (
        "pytest --version",
        "npm run build",
        "python3 -m http.server 8080",  # SAFE-port serve still runs
        "echo hi > new.txt && cat new.txt",  # file op
        "pip install requests",
        "pytest -k kill_switch",  # `kill` as a substring of a -k filter — NOT a kill cmd
        'echo "kill the build cache"',  # `kill` inside a quoted echo arg — not the verb
        "find . -name '*.pyc' | xargs rm",  # xargs without kill — not refused
    )
    with tempfile.TemporaryDirectory() as tmp:
        inst = ProcessSandboxInstance("i", "o", "c", SandboxSpec(), Path(tmp))
        for cmd in controls:
            captured.clear()
            res = await inst.exec_shell(cmd, timeout_s=10)
            assert res.exit_code == 0, cmd
            assert len(captured) == 1, cmd  # reached the launcher exactly once
            assert captured[0][4] == cmd
        await inst.destroy()


async def test_container_backend_kill_path_unchanged():
    # Bug 19 scope: the host-signal refusal is PROCESS-backend ONLY. The container/isolated
    # backend has its OWN PID namespace (a kill there only hits sandbox processes), so its
    # exec_shell must NOT refuse a kill — it runs it inside the container as before.
    from disco.tools.sandbox._container import ContainerInstance

    ran: list[list[str]] = []

    class _FakeContainer:
        def exec_run(self, cmd, demux=False, workdir=None):
            ran.append(cmd)
            return (0, (b"", b""))

    inst = ContainerInstance(
        id="i",
        owner_id="o",
        conversation_id="c",
        spec=SandboxSpec(),
        container=_FakeContainer(),
        container_workspace="/workspace",
        stop_timeout_s=5,
    )
    res = await inst.exec_shell("kill 12345", timeout_s=10)
    assert res.exit_code == 0  # NOT refused — ran inside the container's PID namespace
    # the kill reached the container exec (wrapped in `timeout … sh -c <cmd>`), not a 126
    assert any("kill 12345" in part for part in ran[0])


async def test_session_recreate_fires_rehydrate_hook():
    """bp-13 §2 (orchestrator fix): a mid-session death forces _recreate, which
    must invoke the owner's on_recreate hook AFTER the fresh instance is up —
    the runtime hangs workspace rehydration on it (the conv_f3bdc842 'all files
    were lost' incident). The hook writes back THROUGH the session, so this also
    proves the hook can't deadlock against the session's own lock."""
    from disco.tools.sandbox.base import SandboxUnavailableError

    svc = ProcessSandboxService()
    hook_runs: list[int] = []

    async def rehydrate() -> None:
        hook_runs.append(1)
        # Write through the session itself — like rehydrate_workspace does.
        await session.write_file("rehydrated.txt", b"restored-from-snapshot")

    session = SandboxSession(
        svc, owner_id="local", conversation_id="c-recreate", on_recreate=rehydrate
    )
    await session.write_file("pre.txt", b"x")  # generation 1 is live
    gen1 = session.generation

    # Kill the box out from under the session: the next op sees the typed death.
    async def _dead(*_a, **_k):
        raise SandboxUnavailableError("box died mid-session")

    session._instance.read_file = _dead  # type: ignore[method-assign]

    with pytest.raises(SandboxError, match="re-created"):
        await session.read_file("pre.txt")

    assert session.generation == gen1 + 1  # a fresh instance was created
    assert hook_runs == [1]  # the hook fired exactly once
    # ...and its write landed on the NEW instance (rehydration round trip).
    assert await session.read_file("rehydrated.txt") == b"restored-from-snapshot"
    await session.destroy()


async def test_session_no_hook_recreate_still_works():
    """Without a hook (research surface / tests), recreate behaves exactly as
    before — no new failure mode introduced."""
    from disco.tools.sandbox.base import SandboxUnavailableError

    svc = ProcessSandboxService()
    session = SandboxSession(svc, owner_id="local", conversation_id="c-nohook")
    await session.write_file("pre.txt", b"x")

    async def _dead(*_a, **_k):
        raise SandboxUnavailableError("box died mid-session")

    session._instance.read_file = _dead  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="re-created"):
        await session.read_file("pre.txt")
    assert session.generation == 2
    await session.destroy()
