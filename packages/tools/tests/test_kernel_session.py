import asyncio
import os
import shutil
import stat
import statistics
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from disco.tools.sandbox.base import SandboxError
from disco.tools.sandbox.kernel import (
    _KERNEL_STREAM_CAP,
    KernelResult,
    ProcessKernel,
    _BoundedTextCapture,
    _cap_kernel_traceback,
    _rewrite_process_workspace_literals,
)
from disco.tools.sandbox.process import ProcessSandboxService


def test_sw5_kernel_stream_and_traceback_accumulators_are_bounded():
    capture = _BoundedTextCapture()
    for _ in range(200):
        capture.append("x" * 10_000)
    rendered = capture.render()
    assert len(rendered) < _KERNEL_STREAM_CAP + 256
    assert "kernel output truncated" in rendered

    traceback = _cap_kernel_traceback(["E" * 100_000 for _ in range(20)])
    assert len(traceback) < _KERNEL_STREAM_CAP + 256
    assert "kernel output truncated" in traceback


# 1. Unit tests
def test_kernel_result_rendering():
    """Test the __str__ method of KernelResult."""
    res = KernelResult(ok=True, stdout="hello", stderr="world", result_repr="42")
    assert str(res) == "hello\nworld\n→ 42"

    res = KernelResult(ok=False, stdout="", stderr="", error_traceback="Error!")
    assert str(res) == "Error!"

    res = KernelResult(ok=True, stdout="", stderr="", images=[".pmx/plots/0001.png"])
    assert str(res) == "plot saved: .pmx/plots/0001.png"


def test_process_workspace_literal_rewrite_is_exact_and_jailed(tmp_path):
    code = """
from pathlib import Path
root = Path('/workspace')
child = Path('/workspace/release/fonts/proof.woff2')
label = '/workspaces/not-the-guest-root'
embedded = 'prefix /workspace/release'
dynamic = f'/workspace/{root.name}'
"""

    rewritten = _rewrite_process_workspace_literals(code, tmp_path)

    assert repr(str(tmp_path.resolve())) in rewritten
    assert repr(str(tmp_path.resolve() / "release/fonts/proof.woff2")) in rewritten
    assert "/workspaces/not-the-guest-root" in rewritten
    assert "prefix /workspace/release" in rewritten
    assert f"f'{tmp_path.resolve()}/" in rewritten

    with pytest.raises(SandboxError, match="escapes workspace"):
        _rewrite_process_workspace_literals(
            "open('/workspace/../../etc/passwd').read()", tmp_path
        )


def test_process_workspace_literal_rewrite_preserves_ipython_only_cells(tmp_path):
    code = "%run /workspace/scripts/task.py"
    assert _rewrite_process_workspace_literals(code, tmp_path) == code


@pytest.mark.asyncio
async def test_process_kernel_refuses_plaintext_fallback_without_posix_ipc(
    tmp_path, monkeypatch
):
    pk = ProcessKernel(str(tmp_path))
    monkeypatch.setattr("disco.tools.sandbox.kernel.os.name", "nt")
    with pytest.raises(SandboxError, match="refusing plaintext TCP"):
        await pk.start()
    assert pk._km is None
    assert pk._ipc_dir is None


@pytest.mark.asyncio
async def test_process_kernel_shutdown_closes_client_channels():
    """ProcessKernel owns both the manager process and client ZMQ channels."""
    pk = ProcessKernel("/tmp/test_kernel_shutdown")
    manager = MagicMock()
    manager.shutdown_kernel = AsyncMock()
    client = MagicMock()
    pk._km = manager
    pk._kc = client

    await pk.shutdown()

    client.stop_channels.assert_called_once_with()
    manager.shutdown_kernel.assert_awaited_once_with()
    assert pk._kc is None
    assert pk._km is None


@pytest.mark.asyncio
async def test_timeout_protocol_state_machine():
    """Test timeout protocol: interrupt-succeeds -> intact; interrupt-hangs -> restart path."""
    workspace = "/tmp/test_timeout_mock"
    os.makedirs(workspace, exist_ok=True)
    pk = ProcessKernel(workspace)

    # Mock kernel client
    kc = MagicMock()
    kc.get_iopub_msg = AsyncMock()
    kc.get_shell_msg = AsyncMock()
    pk._kc = kc

    # Scenario 1: interrupt-succeeds -> intact
    # First call to get_iopub_msg times out (raises Exception)
    # Then interrupt() is called.
    # Then it polls for idle. We'll make it return idle with matching msg_id.
    msg_id = "test_msg_id"
    kc.get_iopub_msg.side_effect = [
        TimeoutError("timeout"),
        {
            "header": {"msg_type": "status"},
            "content": {"execution_state": "idle"},
            "parent_header": {"msg_id": msg_id},
        },
    ]
    # Need to mock get_shell_msg too for the final success check
    kc.get_shell_msg.return_value = {
        "parent_header": {"msg_id": msg_id},
        "content": {"status": "ok"},
    }
    pk.interrupt = AsyncMock()

    # Mock execute to return our msg_id
    kc.execute.return_value = msg_id

    res = await pk.execute("while True: pass", timeout_s=0.1)
    assert res.timed_out is True
    assert res.restarted is False
    pk.interrupt.assert_called_once()

    # Scenario 2: interrupt-hangs -> restart path
    # First call times out.
    # Subsequent calls in the "wait for idle" loop also time out.
    kc.get_iopub_msg.side_effect = TimeoutError("timeout")
    pk.interrupt = AsyncMock()
    pk.restart = AsyncMock()

    # Mock time.time to simulate 5 seconds passing quickly
    with patch("time.time") as mock_time:
        mock_time.side_effect = [100.0, 100.1, 106.0]  # start, first check, second check (after 5s)
        res = await pk.execute("while True: pass", timeout_s=0.1)

    assert res.timed_out is True
    assert res.restarted is True
    pk.restart.assert_called_once()

    shutil.rmtree(workspace)


# 2. Integration tests
@pytest.mark.integration
@pytest.mark.asyncio
async def test_kernel_integration_state():
    """(a) state: x = 41 in cell 1, x + 1 in cell 2 -> result should be 42."""
    workspace = "/tmp/test_kernel_state"
    os.makedirs(workspace, exist_ok=True)
    pk = ProcessKernel(workspace)
    try:
        await pk.start()
        res1 = await pk.execute("x = 41", timeout_s=10)
        assert res1.ok
        res2 = await pk.execute("x + 1", timeout_s=10)
        assert res2.ok
        assert "42" in res2.result_repr
    finally:
        await pk.shutdown()
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_process_kernel_uses_protected_absolute_ipc_transport(tmp_path):
    """H081: local code/output never traverses plaintext TCP/ZMQ sockets."""
    pk = ProcessKernel(str(tmp_path))
    ipc_dir: Path | None = None
    try:
        await pk.start()
        assert pk._km is not None
        assert pk._km.transport == "ipc"
        assert Path(pk._km.ip).is_absolute()
        ipc_dir = pk._ipc_dir
        assert ipc_dir is not None and ipc_dir.is_dir()
        assert stat.S_IMODE(ipc_dir.stat().st_mode) == 0o700
        assert len(list(ipc_dir.glob("kernel-*"))) == 5

        first = await pk.execute("secret_state = 40; secret_state + 2", timeout_s=10)
        assert first.ok and first.result_repr == "42"
        await pk.restart()
        restarted = await pk.execute("6 * 7", timeout_s=10)
        assert restarted.ok and restarted.result_repr == "42"
    finally:
        await pk.shutdown()
    assert ipc_dir is not None and not ipc_dir.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kernel_integration_workspace_contract(tmp_path):
    """Process code_exec and sibling tools share the documented guest path."""
    pk = ProcessKernel(str(tmp_path))
    try:
        await pk.start()
        write = await pk.execute(
            "from pathlib import Path; "
            "Path('/workspace/release').mkdir(); "
            "Path('/workspace/release/proof.txt').write_text('proof')",
            timeout_s=10,
        )
        assert write.ok, str(write)
        assert (tmp_path / "release/proof.txt").read_text() == "proof"

        (tmp_path / "from-shell.txt").write_text("shared")
        read = await pk.execute(
            "from pathlib import Path; "
            "print(Path('/workspace/from-shell.txt').read_text())",
            timeout_s=10,
        )
        assert read.ok, str(read)
        assert read.stdout.strip() == "shared"

        refused = await pk.execute(
            "open('/workspace/../../etc/passwd').read()", timeout_s=10
        )
        assert not refused.ok
        assert "escapes workspace" in str(refused)
    finally:
        await pk.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_process_sandbox_destroy_terminates_detached_kernel_descendant():
    service = ProcessSandboxService()
    inst = await service.create(
        spec=None,
        owner_id="kernel-cleanup",
        conversation_id="kernel-cleanup-detached",
    )
    pk = ProcessKernel(inst.workspace_path or "")
    child_pid: int | None = None
    try:
        await pk.start()
        spawned = await pk.execute(
            "import subprocess, sys; "
            "child = subprocess.Popen("  # noqa: S603 — deliberate cleanup fixture
            "[sys.executable, '-c', 'import time; time.sleep(300)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL, start_new_session=True); "
            "print(child.pid)",
            timeout_s=10,
        )
        assert spawned.ok, str(spawned)
        child_pid = int(spawned.stdout.strip())
        assert Path(f"/proc/{child_pid}").exists()

        await pk.shutdown()
        assert Path(f"/proc/{child_pid}").exists(), "fixture did not detach"
        await inst.destroy()
        for _ in range(40):
            if not Path(f"/proc/{child_pid}").exists():
                break
            await asyncio.sleep(0.05)
        assert not Path(f"/proc/{child_pid}").exists()
    finally:
        await pk.shutdown()
        if child_pid is not None and Path(f"/proc/{child_pid}").exists():
            os.kill(child_pid, 9)
        if Path(inst.workspace_path or "/nonexistent").exists():
            await inst.destroy()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kernel_integration_fidelity():
    """(b) fidelity: cell 1 binds a real socket.socket and a threading.Thread; cell 2 uses BOTH."""
    workspace = "/tmp/test_kernel_fidelity"
    os.makedirs(workspace, exist_ok=True)
    pk = ProcessKernel(workspace)
    try:
        await pk.start()
        code1 = """
import socket
import threading
import time

shared_list = []
def worker():
    for i in range(5):
        shared_list.append(i)
        time.sleep(0.1)

t = threading.Thread(target=worker)
t.start()

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('127.0.0.1', 0))
port = s.getsockname()[1]
"""
        res1 = await pk.execute(code1, timeout_s=10)
        assert res1.ok

        code2 = """
t.join()
s.close()
(len(shared_list), port > 0)
"""
        res2 = await pk.execute(code2, timeout_s=10)
        assert res2.ok
        assert "(5, True)" in res2.result_repr
    finally:
        await pk.shutdown()
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kernel_integration_interrupt():
    """(c) interrupt: while True: pass with timeout_s=3 -> timed_out=True,
    restarted=False, state preserved."""
    workspace = "/tmp/test_kernel_interrupt"
    os.makedirs(workspace, exist_ok=True)
    pk = ProcessKernel(workspace)
    try:
        await pk.start()
        await pk.execute("y = 100", timeout_s=10)

        res = await pk.execute("while True: pass", timeout_s=3)
        assert res.timed_out is True
        assert res.restarted is False

        res2 = await pk.execute("y", timeout_s=10)
        assert "100" in res2.result_repr
    finally:
        await pk.shutdown()
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kernel_integration_latency():
    """(d) flat latency: Run 60 cells, Assert median(t[50:60]) <= 2 * median(t[2:12])."""
    workspace = "/tmp/test_kernel_latency"
    os.makedirs(workspace, exist_ok=True)
    pk = ProcessKernel(workspace)
    try:
        await pk.start()
        timings = []
        for i in range(60):
            start = time.perf_counter()
            await pk.execute(f"data_{i} = list(range(200_000))", timeout_s=10)
            end = time.perf_counter()
            timings.append(end - start)

        print("\nCell Index | Time Taken (s)")
        print("-----------|---------------")
        for i, t in enumerate(timings):
            print(f"{i:10d} | {t:.6f}")

        m1 = statistics.median(timings[2:12])
        m2 = statistics.median(timings[50:60])
        print(f"\nMedian (2-12): {m1:.6f}")
        print(f"Median (50-60): {m2:.6f}")

        assert m2 <= 2 * m1
    finally:
        await pk.shutdown()
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kernel_integration_plots():
    """(e) plots: A cell using matplotlib to plot something -> verify PNG exists."""
    workspace = "/tmp/test_kernel_plots"
    os.makedirs(workspace, exist_ok=True)
    pk = ProcessKernel(workspace)
    try:
        await pk.start()
        code = """
import matplotlib.pyplot as plt
import numpy as np
x = np.linspace(0, 10, 100)
plt.plot(x, np.sin(x))
plt.show()
"""
        res = await pk.execute(code, timeout_s=20)
        assert res.ok
        assert len(res.images) > 0
        img_path = Path(workspace) / res.images[0]
        assert img_path.exists()
    finally:
        await pk.shutdown()
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.asyncio
async def test_kernel_output_discipline():
    """B2 discipline: big-output -> file + head + marker; small output ->
    unchanged; traceback -> full."""
    workspace = "/tmp/test_kernel_discipline"
    os.makedirs(workspace, exist_ok=True)
    pk = ProcessKernel(workspace)

    # Mock kernel client
    kc = MagicMock()
    kc.execute = MagicMock(return_value="msg_id")
    kc.get_iopub_msg = AsyncMock()
    kc.get_shell_msg = AsyncMock()
    pk._kc = kc

    # Mocking successful execution
    kc.get_shell_msg.return_value = {
        "parent_header": {"msg_id": "msg_id"},
        "content": {"status": "ok"},
    }

    # Case 1: Big stdout
    big_stdout = "A" * 3000
    kc.get_iopub_msg.side_effect = [
        {
            "header": {"msg_type": "stream"},
            "content": {"name": "stdout", "text": big_stdout},
            "parent_header": {"msg_id": "msg_id"},
        },
        {
            "header": {"msg_type": "status"},
            "content": {"execution_state": "idle"},
            "parent_header": {"msg_id": "msg_id"},
        },
    ]
    res = await pk.execute("print('big')", timeout_s=1)
    assert len(res.stdout) < 3000
    assert "[full output: /workspace/.outputs/" in res.stdout
    assert res.stdout.startswith("A" * 500)

    # Verify file exists
    outputs_dir = Path(workspace) / ".outputs"
    files = list(outputs_dir.glob("*-out.txt"))
    assert len(files) == 1
    with open(files[0]) as f:
        assert f.read() == big_stdout

    # Case 2: Small output
    small_stdout = "hello"
    kc.get_iopub_msg.side_effect = [
        {
            "header": {"msg_type": "stream"},
            "content": {"name": "stdout", "text": small_stdout},
            "parent_header": {"msg_id": "msg_id"},
        },
        {
            "header": {"msg_type": "status"},
            "content": {"execution_state": "idle"},
            "parent_header": {"msg_id": "msg_id"},
        },
    ]
    res = await pk.execute("print('small')", timeout_s=1)
    assert res.stdout == small_stdout

    # Case 3: Traceback (never truncated)
    big_traceback = "E" * 3000
    kc.get_iopub_msg.side_effect = [
        {
            "header": {"msg_type": "error"},
            "content": {"traceback": [big_traceback]},
            "parent_header": {"msg_id": "msg_id"},
        },
        {
            "header": {"msg_type": "status"},
            "content": {"execution_state": "idle"},
            "parent_header": {"msg_id": "msg_id"},
        },
    ]
    res = await pk.execute("raise Error()", timeout_s=1)
    assert res.error_traceback == big_traceback

    shutil.rmtree(workspace)
