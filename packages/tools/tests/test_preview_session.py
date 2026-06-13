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

import pytest
from disco.tools.sandbox.port_owner import port_owner
from disco.tools.sandbox.process import ProcessSandboxService
from disco.tools.sandbox.session import SandboxSession

_PORT = 8188  # free on dev hosts; NOT 8000 (the agent-server owns that here)


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preview_session_lifecycle_process_backend():
    session = SandboxSession(ProcessSandboxService(), conversation_id="conv-bp02")
    ns = "pmx-conv-bp0-"  # conversation_id[:8] + '-' under the manager's 'pmx-' prefix
    try:
        inst = await session._ensure()

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
