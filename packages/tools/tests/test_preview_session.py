"""BP-02 acceptance 2 — preview-as-session on the process backend with REAL tmux.

The race's tombstone lives here: after the agent kills the 'preview' session the port
stays FREE (no hidden supervisor resurrects it — that resurrection-vs-pkill fight is
the dual-owner race BP-02 removed), and a dev server that takes the port over is
attributed to ITS session by the /proc probe.

Runs on a free high port: on dev hosts :8000 is typically the agent-server itself,
which is exactly why ensure_preview() grew the port parameter.

Also contains a pure-unit test (no tmux) for the A4 TOCTOU fix: destroy() must
cancel an in-flight auto-preview task without leaving "Task was destroyed but it is
pending!" warnings.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex

import pytest
from disco.tools.sandbox.port_owner import port_owner
from disco.tools.sandbox.process import ProcessSandboxService
from disco.tools.sandbox.session import SandboxSession

_PORT = 18188  # NOT 8000 (agent-server) and NOT 8188 — ComfyUI's default on dev hosts


# ---------------------------------------------------------------------------
# A4 unit test — no tmux required; fake service + monkeypatched ensure_preview
# ---------------------------------------------------------------------------


class _FakeSandboxInstance:
    """Minimal in-memory SandboxInstance — no filesystem, no subprocess."""

    id = "fake-sbx-a4"
    owner_id = "local"
    conversation_id = "conv-a4"
    spec = None

    async def exec_shell(self, cmd: str, *, timeout_s: int):
        from disco.tools.sandbox.base import ExecResult

        return ExecResult(exit_code=0, stdout="/tmp/fake-workspace", stderr="")

    async def read_file(self, path: str) -> bytes:
        return b""

    async def write_file(self, path: str, data: bytes) -> None:
        pass

    async def list_dir(self, path: str) -> list[str]:
        return []

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return None

    async def destroy(self) -> None:
        pass


class _FakeSvc:
    """SandboxService that returns _FakeSandboxInstance; 'fake' name avoids tmux namespace."""

    name = "fake"

    async def create(self, spec, *, owner_id, conversation_id):
        inst = _FakeSandboxInstance()
        inst.owner_id = owner_id
        inst.conversation_id = conversation_id
        return inst

    async def get(self, instance_id):
        return None


@pytest.mark.asyncio
async def test_destroy_cancels_inflight_preview_task():
    """A4 TOCTOU fix: destroy() cancels an in-flight auto-preview task cleanly.

    Invariants checked:
    - The task reference is stored on self._preview_task (the fix is wired).
    - After destroy(), the task is done AND cancelled (not just finished normally).
    - No asyncio "Task was destroyed but it is pending!" warning fires — because
      destroy() awaited the task before returning.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    session = SandboxSession(_FakeSvc(), conversation_id="conv-a4-cancel")

    # Replace ensure_preview with a coroutine that blocks until cancelled or released.
    async def _slow_preview(port: int = 8000) -> bool:
        started.set()
        await release.wait()  # blocks; will be cancelled by destroy()
        return True

    session.ensure_preview = _slow_preview  # type: ignore[method-assign]

    # Trigger instance creation — this spawns the auto-preview task internally.
    await session._ensure()

    # Wait for the preview task to actually start executing (entered _slow_preview).
    await asyncio.wait_for(started.wait(), timeout=2.0)

    task = session._preview_task
    assert task is not None, "_preview_task must be set after _ensure()"
    assert not task.done(), "preview task should still be running before destroy()"

    # destroy() must cancel + await the task — no pending-task warning.
    await session.destroy()

    assert task.done(), "task must be done after destroy()"
    assert task.cancelled(), "task must be cancelled (not error-finished) by destroy()"
    # If we got here with no 'Task was destroyed but it is pending!' stderr noise,
    # the TOCTOU is fixed — asyncio only emits that warning when a pending task is
    # garbage-collected WITHOUT being awaited.


@pytest.mark.asyncio
async def test_destroy_closes_cached_preview_manager():
    """P2 #4: a started PreviewManager cached on the session (`_preview_manager`, set by
    the preview_* tools) runs a supervisor task that sleeps forever. `destroy()` must
    `aclose()` it so the task is cancelled/done — it can't leak past the sandbox it
    supervised."""
    from disco.agent_server.preview_manager import PreviewManager

    session = SandboxSession(_FakeSvc(), conversation_id="conv-pmgr-leak")
    mgr = PreviewManager(
        session,
        port_pool=[8188],
        health_attempts=1,
        health_interval_s=0.0,
        supervise_interval_s=0.01,
    )
    mgr._ensure_supervisor()  # spin up the supervisor task (as a real start would)
    sup = mgr._supervisor
    assert sup is not None and not sup.done(), "supervisor task should be running"

    session._preview_manager = mgr  # how the tools cache it on the session

    await session.destroy()

    assert mgr._closed is True
    assert sup.done(), "supervisor task must be cancelled/done after destroy()"


@pytest.mark.asyncio
async def test_static_preview_shell_serializes_model_authored_serve_dir(monkeypatch):
    """A filename-shaped shell payload remains one argv element on restart."""
    session = SandboxSession(_FakeSvc(), conversation_id="conv-sw5-preview")
    instance = _FakeSandboxInstance()
    session._instance = instance
    malicious = "/tmp/work/x; touch /tmp/sw5-pwned #"

    async def _no_owner(_instance, _port):
        return None

    async def _serve_dir(_instance, _workspace):
        return malicious

    captured = []

    async def _exec(name, command, exec_dir):
        captured.append((name, command, exec_dir))
        return None

    monkeypatch.setattr("disco.tools.sandbox.port_owner.port_owner", _no_owner)
    session._detect_serve_dir = _serve_dir  # type: ignore[method-assign]
    session.sessions.exec = _exec  # type: ignore[method-assign]

    assert await session.ensure_preview(8000) is True
    assert len(captured) == 1
    name, command, exec_dir = captured[0]
    assert name == "preview" and exec_dir == malicious
    assert shlex.split(command) == [
        "python3",
        "-m",
        "http.server",
        "8000",
        "-d",
        malicious,
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preview_session_lifecycle_process_backend():
    session = SandboxSession(ProcessSandboxService(), conversation_id="conv-bp02")
    ns = "disco-conv-bp0-"  # conversation_id[:8] + '-' under the manager's 'disco-' prefix
    try:
        inst = await session._ensure()

        # Deterministically retire the fire-and-forget auto-preview before driving the
        # EXPLICIT ensure_preview below. Both use the 'preview' tmux session name, so
        # letting them race is the source of intermittent 'duplicate session' failures
        # (the race pre-dates and is independent of the manager work). Let the auto-
        # preview FINISH (it swallows its own errors), then kill the 'preview' session it
        # created so the explicit path below starts from a clean slate.
        if session._preview_task is not None:
            with contextlib.suppress(Exception):
                await session._preview_task
        with contextlib.suppress(Exception):
            await session.sessions.kill_foreground("preview")
        await asyncio.sleep(0.5)  # let the auto-preview's port free before we re-bind

        # (a) ensure_preview starts the static server as the visible 'preview' session
        assert await session.ensure_preview(_PORT) is True
        await asyncio.sleep(1.2)
        owner = await port_owner(inst, _PORT)
        assert owner is not None and owner.pid is not None
        assert "http.server" in (owner.cmdline or "")
        assert owner.session == ns + "preview"
        # idempotent: port owned -> False, and crucially NO second server is started
        assert await session.ensure_preview(_PORT) is False

        # (b) kill the preview session -> the port FREES and STAYS free — no hidden
        # supervisor resurrects it within 5s (the old race's tombstone).
        await session.sessions.kill_foreground("preview")
        await asyncio.sleep(5.0)
        freed = await port_owner(inst, _PORT)
        assert freed is None or freed.pid is None

        # (c) agent-style takeover: a dev server in its own session owns the port and
        # the probe attributes it to THAT session; ensure_preview won't fight it.
        out = await session.sessions.exec("dev", f"python3 -m http.server {_PORT}", None)
        assert out.running is True
        await asyncio.sleep(1.2)
        dev_owner = await port_owner(inst, _PORT)
        assert dev_owner is not None and dev_owner.pid is not None
        assert dev_owner.session == ns + "dev"
        assert await session.ensure_preview(_PORT) is False
    finally:
        await session.destroy()
