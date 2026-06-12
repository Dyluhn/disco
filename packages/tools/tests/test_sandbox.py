"""Sandbox spec/instance/backend (§11.3) + file-tool jailing."""

from __future__ import annotations

import pytest
from conftest import FakeSandboxInstance, call
from disco.tools import (
    DefaultToolExecutor,
    ProcessSandboxService,
    SandboxError,
    SandboxSession,
    SandboxSpec,
    ToolScope,
    build_default_registry,
)


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
