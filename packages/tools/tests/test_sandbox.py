"""Sandbox spec/instance/backend (§11.3) + file-tool jailing."""

from __future__ import annotations

import pytest
from conftest import FakeSandboxInstance, call
from perpleximanus.tools import (
    DefaultToolExecutor,
    ProcessSandboxService,
    SandboxError,
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
    assert await round_trip(proc) == await round_trip(FakeSandboxInstance()) == "same-bytes"
    await proc.destroy()


async def test_shell_and_code_exec_run_in_process_sandbox():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    reg = build_default_registry()
    ex = DefaultToolExecutor(
        reg, ToolScope(allowed_tools=frozenset({"shell", "code_exec"})), sandbox=inst
    )
    sh = await ex.execute(call("shell", command="echo hi"))
    assert sh.success and "hi" in sh.content
    code = await ex.execute(call("code_exec", language="python", code="print(6*7)"))
    assert code.success and "42" in code.content
    await inst.destroy()
