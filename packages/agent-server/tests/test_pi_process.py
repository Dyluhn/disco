"""Tests for `PiProcess` — the Python manager for the Node Pi-kernel sidecar
(Disco Pi Build Kernel Campaign, PR B2).

The lifecycle tests drive the REAL built sidecar (`packages/pi-kernel/dist/index.js`)
over its frozen stdio protocol — proving the handshake, the no-gateway error
path, and that `cancel` keeps the process alive while `aclose` reaps it. A
separate test spawns a STUBBORN fake sidecar (ignores stdin EOF + SIGTERM and
forks a long-lived grandchild) to prove `aclose` escalates to a `SIGKILL` of the
whole process *tree* — no child survives.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from disco.agent_server.build_kernel.pi_process import (
    PiProcess,
    default_pi_kernel_entry,
    scrub_provider_credential_env,
)

Frame = dict[str, Any]

_ENTRY = str(default_pi_kernel_entry())
# .../pi-kernel/{dist,src}/index.{js,ts} → the pi-kernel package dir.
_PI_KERNEL_CWD = str(Path(_ENTRY).resolve().parents[1])

pytestmark = pytest.mark.skipif(
    not Path(_ENTRY).exists(),
    reason="pi-kernel sidecar entry not found (build packages/pi-kernel)",
)


# ---- helpers -----------------------------------------------------------------


def _make_proc() -> PiProcess:
    return PiProcess(
        node_bin="node",
        entry=_ENTRY,
        env=dict(os.environ),
        cwd=_PI_KERNEL_CWD,
    )


async def _next(it: AsyncIterator[Frame], timeout: float = 10.0) -> Frame:
    return await asyncio.wait_for(it.__anext__(), timeout)


async def _await_frame(
    it: AsyncIterator[Frame],
    pred: Callable[[Frame], bool],
    timeout: float = 10.0,
) -> Frame:
    async def run() -> Frame:
        async for frame in it:
            if pred(frame):
                return frame
        raise AssertionError("stream ended before a matching frame")

    return await asyncio.wait_for(run(), timeout)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- env scrub ---------------------------------------------------------------


def test_scrub_removes_provider_credentials() -> None:
    env = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-secret",
        "ANTHROPIC_API_KEY": "sk-ant",
        "FOO_API_TOKEN": "tok",
        "MY_OAUTH_TOKEN": "oauth",
        "SOMETHING_KEY": "k",
        "DISCO_HOME": "/home/disco",
    }
    cleaned = scrub_provider_credential_env(env)
    assert "OPENAI_API_KEY" not in cleaned
    assert "ANTHROPIC_API_KEY" not in cleaned
    assert "FOO_API_TOKEN" not in cleaned
    assert "MY_OAUTH_TOKEN" not in cleaned
    assert "SOMETHING_KEY" not in cleaned  # matches the `_KEY$` pattern
    # Non-credential vars survive untouched.
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["DISCO_HOME"] == "/home/disco"


def test_child_env_is_scrubbed() -> None:
    proc = PiProcess(
        node_bin="node",
        entry=_ENTRY,
        env={"PATH": os.environ.get("PATH", ""), "OPENAI_API_KEY": "sk-leak"},
        cwd=_PI_KERNEL_CWD,
    )
    assert "OPENAI_API_KEY" not in proc._child_env  # type: ignore[attr-defined]
    assert "PATH" in proc._child_env  # type: ignore[attr-defined]


# ---- real sidecar lifecycle --------------------------------------------------


@pytest.mark.asyncio
async def test_ready_then_heartbeat() -> None:
    proc = _make_proc()
    ready = await proc.start({"heartbeatMs": 50})
    try:
        assert ready["type"] == "ready"
        assert ready["protocolVersion"] == 1
        # No gateway wired → no model selected, no tools.
        assert ready["model"] is None
        assert ready["tools"]["noTools"] == "all"
        assert ready["tools"]["activeToolNames"] == []
        assert ready["tools"]["customToolCount"] == 0

        events = proc.events()
        # `ready` is replayed on the stream, then heartbeats follow.
        first = await _next(events)
        assert first["type"] == "ready"
        heartbeat = await _await_frame(events, lambda f: f["type"] == "heartbeat")
        assert isinstance(heartbeat["ts"], int)
        assert proc.returncode is None
    finally:
        await proc.aclose()


@pytest.mark.asyncio
async def test_prompt_without_gateway_is_nonfatal_and_stays_alive() -> None:
    proc = _make_proc()
    await proc.start({"heartbeatMs": 50})
    try:
        events = proc.events()
        await proc.prompt("build me an app")

        error = await _await_frame(events, lambda f: f["type"] == "error")
        assert "prompt failed" in error["message"]
        assert error.get("fatal", False) is False

        # The sidecar survives a failed prompt: it keeps heartbeating.
        await asyncio.sleep(0.15)
        assert proc.returncode is None
        heartbeat = await _await_frame(events, lambda f: f["type"] == "heartbeat")
        assert heartbeat["type"] == "heartbeat"
    finally:
        await proc.aclose()


@pytest.mark.asyncio
async def test_cancel_keeps_alive_then_aclose_reaps_tree() -> None:
    proc = _make_proc()
    await proc.start({"heartbeatMs": 50})
    pgid = proc.pgid
    assert pgid is not None

    # cancel writes {"type":"cancel"} and must NOT kill the process.
    await proc.cancel()
    await asyncio.sleep(0.3)
    assert proc.returncode is None

    # aclose closes stdin → the sidecar exits cleanly (code 0) and the group is gone.
    code = await proc.aclose()
    assert code == 0
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


# ---- stubborn fake: prove tree-kill escalation -------------------------------


_STUBBORN_FAKE = r"""
import json, os, signal, subprocess, sys, time

# Ignore the graceful stop signal so aclose must escalate to SIGKILL.
signal.signal(signal.SIGTERM, signal.SIG_IGN)

# Fork a long-lived GRANDCHILD in the same process group; it must die with the tree.
child = subprocess.Popen(["sleep", "300"])
with open(os.environ["FAKE_CHILD_PID_FILE"], "w") as fh:
    fh.write(str(child.pid))

sys.stdout.write(json.dumps({
    "type": "ready", "protocolVersion": 1, "piVersion": "fake",
    "model": None,
    "tools": {"activeToolNames": [], "customToolCount": 0, "noTools": "all"},
}) + "\n")
sys.stdout.flush()

# Never read stdin (EOF is ignored) and never exit on its own.
while True:
    sys.stdout.write(json.dumps({"type": "heartbeat", "ts": int(time.time() * 1000)}) + "\n")
    sys.stdout.flush()
    time.sleep(0.1)
"""


@pytest.mark.asyncio
async def test_aclose_kills_process_tree(tmp_path: Path) -> None:
    fake = tmp_path / "stubborn_sidecar.py"
    fake.write_text(_STUBBORN_FAKE)
    pidfile = tmp_path / "child.pid"

    proc = PiProcess(
        node_bin=sys.executable,  # run the fake with Python, not node
        entry=str(fake),
        env={**os.environ, "FAKE_CHILD_PID_FILE": str(pidfile)},
        cwd=str(tmp_path),
    )
    ready = await proc.start({}, timeout=10.0)
    assert ready["type"] == "ready"

    pgid = proc.pgid
    assert pgid is not None
    grandchild_pid = int(pidfile.read_text().strip())
    assert _pid_alive(grandchild_pid)

    # EOF is ignored and SIGTERM is ignored by the leader, so aclose must escalate
    # to a group SIGKILL. Short graces keep the test fast.
    code = await proc.aclose(graceful_timeout=0.8, kill_timeout=1.5)
    # Killed by signal → negative return code (SIGKILL=-9, or SIGTERM=-15 if the
    # leader had honored it). Either proves the process did not exit on its own.
    assert code is not None and code < 0

    # The whole tree is gone: the group is unsignalable and the grandchild is dead.
    for _ in range(40):
        if not _pid_alive(grandchild_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(grandchild_pid), "grandchild (preview-server analogue) survived aclose"
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


# ---- clean leader exit but a grandchild lingers ------------------------------

# The leader exits CLEANLY (code 0) on stdin EOF, but it forked a long-lived
# grandchild into the same process group first. A clean leader exit must NOT let
# the grandchild (a preview-server analogue) outlive the tree: aclose must probe
# the group and tear it down even on the graceful path.
_CLEAN_LEADER_FORKS_GRANDCHILD = r"""
import json, os, subprocess, sys

# Fork a long-lived GRANDCHILD in the SAME process group before the leader leaves.
child = subprocess.Popen(["sleep", "300"])
with open(os.environ["FAKE_CHILD_PID_FILE"], "w") as fh:
    fh.write(str(child.pid))

sys.stdout.write(json.dumps({
    "type": "ready", "protocolVersion": 1, "piVersion": "fake",
    "model": None,
    "tools": {"activeToolNames": [], "customToolCount": 0, "noTools": "all"},
}) + "\n")
sys.stdout.flush()

# Block until stdin EOF (aclose closes stdin), then exit CLEANLY — leaving the
# grandchild alive in the same group.
sys.stdin.read()
sys.exit(0)
"""


@pytest.mark.asyncio
async def test_aclose_reaps_grandchild_after_clean_leader_exit(tmp_path: Path) -> None:
    fake = tmp_path / "clean_leader_sidecar.py"
    fake.write_text(_CLEAN_LEADER_FORKS_GRANDCHILD)
    pidfile = tmp_path / "child.pid"

    proc = PiProcess(
        node_bin=sys.executable,  # run the fake with Python, not node
        entry=str(fake),
        env={**os.environ, "FAKE_CHILD_PID_FILE": str(pidfile)},
        cwd=str(tmp_path),
    )
    ready = await proc.start({}, timeout=10.0)
    assert ready["type"] == "ready"

    pgid = proc.pgid
    assert pgid is not None
    grandchild_pid = int(pidfile.read_text().strip())
    assert _pid_alive(grandchild_pid)

    # The leader takes the GRACEFUL path (clean exit 0 on stdin EOF), but the
    # grandchild lingers in the group — aclose must still tear the group down.
    code = await proc.aclose(graceful_timeout=2.0, kill_timeout=1.5)
    assert code == 0, "leader exited cleanly on stdin EOF"

    for _ in range(40):
        if not _pid_alive(grandchild_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(grandchild_pid), "grandchild survived a CLEAN leader exit"
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
