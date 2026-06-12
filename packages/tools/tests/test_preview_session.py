"""BP-02 acceptance 2 — preview-as-session on the process backend with REAL tmux.

The race's tombstone lives here: after the agent kills the 'preview' session the port
stays FREE (no hidden supervisor resurrects it — that resurrection-vs-pkill fight is
the dual-owner race BP-02 removed), and a dev server that takes the port over is
attributed to ITS session by the /proc probe.

Runs on a free high port: on dev hosts :8000 is typically the agent-server itself,
which is exactly why ensure_preview() grew the port parameter.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.tools.sandbox.port_owner import port_owner
from disco.tools.sandbox.process import ProcessSandboxService
from disco.tools.sandbox.session import SandboxSession

_PORT = 8188  # free on dev hosts; NOT 8000 (the agent-server owns that here)


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
