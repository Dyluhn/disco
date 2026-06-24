"""Sandbox spec/instance/backend (§11.3) + file-tool jailing."""

from __future__ import annotations

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
    import tempfile
    from pathlib import Path

    from disco.tools.sandbox.process import ProcessSandboxInstance

    with tempfile.TemporaryDirectory() as tmp:
        inst = ProcessSandboxInstance("i", "o", "c", SandboxSpec(), Path(tmp))

        # 1. Not in USER_PORTS -> None
        assert inst.expose_port(9999) is None

        # 2. In USER_PORTS but not bound -> None
        assert inst.expose_port(3000) is None

        # 3. INTERNAL_PORTS (8899) -> None
        assert inst.expose_port(8899) is None

        await inst.destroy()


async def test_process_exec_shell_remaps_reserved_preview_serve():
    # Bug 16 (§17 no-fluke): a reserved-port preview SERVE is REMAPPED to a safe port
    # BEFORE execution, instead of refused — so a model serving on 8000 recovers rather
    # than STUCKing. Proven hermetically via an `echo` proxy (NO real bind: we never
    # touch port 8000/3000) that the reserved port has already been rewritten by the
    # time the command runs.
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
        # `echo ... http.server 8000` would normally trip the containment refusal; the
        # remap rewrites the reserved port first, so the command runs and echoes `safe`.
        res = await inst.exec_shell("echo python3 -m http.server 8000", timeout_s=10)
        assert res.exit_code == 0, res.stderr
        assert safe in res.stdout
        assert "8000" not in res.stdout  # the reserved port was rewritten before exec
        await inst.destroy()


async def test_process_exec_shell_still_refuses_reserved_kill_and_arbitrary_bind():
    # Must-not-regress: the remap covers ONLY the http.server serve shape. A reserved-
    # port KILL and an arbitrary reserved BIND are STILL refused (exit 126) with the
    # actionable message — and the refusal returns BEFORE the real subprocess launcher.
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
