"""Tests for disco.agent_server.workspace_process_fence.

Coverage: child-process fence contention with SIGKILL recovery, asyncio task
reentrancy, and fail-closed behavior when fcntl is unavailable.
"""

from __future__ import annotations

import select
import signal
import subprocess
import sys

import pytest
from disco.agent_server.workspace_process_fence import (
    WorkspaceProcessBusy,
    WorkspaceProcessFenceUnavailable,
    try_workspace_process_fence,
    workspace_process_fence,
    workspace_process_lock_path,
)


def test_child_holds_flock_then_killed(tmp_path, monkeypatch):
    """A real child holds the lock file -> wait=False raises Busy, then
    SIGKILL + child death releases the flock -> acquisition succeeds.
    """
    lock_dir = tmp_path / "locks"
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(lock_dir))
    workspace = tmp_path / "ws"
    lock_path = workspace_process_lock_path(workspace)

    child_code = (
        "import os, fcntl, sys, time\n"
        f"fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        readable, _, _ = select.select([proc.stdout], [], [], 5)
        assert readable, "child did not acquire the fence within five seconds"
        assert proc.stdout.readline() == "ready\n"
        with pytest.raises(WorkspaceProcessBusy):
            with try_workspace_process_fence(workspace):
                pass
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)

    with try_workspace_process_fence(workspace):
        pass


def test_group_or_world_writable_lock_directory_fails_closed(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(mode=0o700)
    lock_dir.chmod(0o777)
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(lock_dir))

    with pytest.raises(WorkspaceProcessFenceUnavailable, match="group/world writable"):
        workspace_process_lock_path(tmp_path / "ws")


def test_symlinked_lock_directory_ancestor_fails_closed(tmp_path, monkeypatch):
    target_a = tmp_path / "target-a"
    target_b = tmp_path / "target-b"
    (target_a / "locks").mkdir(parents=True, mode=0o700)
    (target_b / "locks").mkdir(parents=True, mode=0o700)
    redirect = tmp_path / "redirect"
    redirect.symlink_to(target_a, target_is_directory=True)
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(redirect / "locks"))

    with pytest.raises(WorkspaceProcessFenceUnavailable, match="symlinked ancestor"):
        workspace_process_lock_path(tmp_path / "ws")

    redirect.unlink()
    redirect.symlink_to(target_b, target_is_directory=True)
    with pytest.raises(WorkspaceProcessFenceUnavailable, match="symlinked ancestor"):
        workspace_process_lock_path(tmp_path / "ws")


def test_relative_lock_directory_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", "relative-locks")

    with pytest.raises(WorkspaceProcessFenceUnavailable, match="absolute path"):
        workspace_process_lock_path(tmp_path / "ws")


def test_lock_directory_owned_by_another_user_fails_closed(tmp_path, monkeypatch):
    import disco.agent_server.workspace_process_fence as wpf

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(mode=0o700)
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(lock_dir))
    actual_euid = wpf.os.geteuid()
    monkeypatch.setattr(wpf.os, "geteuid", lambda: actual_euid + 1)

    with pytest.raises(WorkspaceProcessFenceUnavailable, match="not owned"):
        workspace_process_lock_path(tmp_path / "ws")


async def test_reentrant_task_inherits_fence(tmp_path, monkeypatch):
    """A child asyncio task inherits an active reentrant scope and can enter
    the same fence without self-deadlock; after outer exit a fresh
    subprocess acquisition succeeds.
    """
    import asyncio

    lock_dir = tmp_path / "locks"
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(lock_dir))
    workspace = tmp_path / "ws"

    child_entered = asyncio.Event()
    child_done = asyncio.Event()

    async def child():
        async with workspace_process_fence(workspace, wait=False):
            child_entered.set()
            await child_done.wait()

    async with workspace_process_fence(workspace, wait=False):
        task = asyncio.create_task(child())
        await asyncio.wait_for(child_entered.wait(), timeout=5)

    # The child inherited a retained descriptor, so the kernel lock remains
    # owned even though the lexical parent scope has already exited.
    with pytest.raises(WorkspaceProcessBusy):
        with try_workspace_process_fence(workspace):
            pass
    child_done.set()
    await asyncio.wait_for(task, timeout=5)

    lock_path = workspace_process_lock_path(workspace)
    subp = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os,fcntl;"
        f"fd=os.open({str(lock_path)!r},os.O_CREAT|os.O_RDWR,0o600);"
        "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);"
        "print('ok')",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(subp.communicate(), timeout=5)
    assert subp.returncode == 0
    assert b"ok" in stdout


async def test_fcntl_none_async_fence_fails_closed(tmp_path, monkeypatch):
    """Monkeypatching _fcntl to None makes the strict async fence fail
    closed with WorkspaceProcessFenceUnavailable.
    """
    import disco.agent_server.workspace_process_fence as wpf

    lock_dir = tmp_path / "locks"
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(lock_dir))
    monkeypatch.setattr(wpf, "_fcntl", None)

    workspace = tmp_path / "ws"
    with pytest.raises(WorkspaceProcessFenceUnavailable):
        async with workspace_process_fence(workspace, wait=False):
            pass
