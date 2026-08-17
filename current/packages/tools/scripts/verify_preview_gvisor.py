"""BP-02 live verification: preview-as-session against the gVisor backend (VM 201).

Run from a tailnet node over Docker-over-SSH:
    PMX_DOCKER_HOST=ssh://sandbox@100.81.82.115 uv run python \
        current/packages/tools/scripts/verify_preview_gvisor.py

Proves the BP-02 contract live, in-container:
  (a) the container's main process is a plain `sleep infinity` — the supervised
      keepalive http.server (the dual-owner :8000 race) is GONE;
  (b) ensure_preview() starts the static server as tmux session 'preview' and is
      idempotent (second call returns False: port owned, nothing to do);
  (c) port_owner attributes :8000 to pid + cmdline + the 'preview' session;
  (d) the workspace is actually served (in-container fetch of a written file);
  (e) self-heal: kill the preview session -> port frees -> ensure_preview brings
      it back (the UI 'Restart preview' path).
"""

from __future__ import annotations

import asyncio
import os

from disco.tools.sandbox import (
    GvisorSandboxService,
    SandboxSpec,
    default_sandbox_config,
)
from disco.tools.sandbox.port_owner import port_owner
from disco.tools.sandbox.session import SandboxSession


def ok(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not passed:
        raise SystemExit(f"FAILED: {label}")


async def main() -> None:
    cfg = default_sandbox_config()
    socket = os.environ.get("PMX_DOCKER_HOST", cfg.docker_socket)
    cfg = cfg.model_copy(update={"docker_socket": socket})
    print(f"config: socket={cfg.docker_socket} runtime={cfg.runtime} image={cfg.image}")
    svc = GvisorSandboxService(cfg)

    session = SandboxSession(svc, SandboxSpec(memory_mb=512), conversation_id="bp02-verify")
    try:
        inst = await session._ensure()

        # (a) plain keepalive: no supervised http.server baked into the main process
        raw = inst._container  # the docker-py Container — inspect its command
        raw.reload()
        cmd = raw.attrs["Config"]["Cmd"]
        ok("container cmd is plain sleep infinity", cmd == ["sleep", "infinity"], repr(cmd))
        ps = await inst.exec_shell("ps aux", timeout_s=20)
        ok(
            "no pre-existing http.server process",
            "http.server" not in ps.stdout,
            "ps clean",
        )

        # (b) ensure_preview starts session 'preview'; second call is a no-op False
        started = await session.ensure_preview()
        ok("ensure_preview starts the server", started is True)
        await asyncio.sleep(1.5)
        again = await session.ensure_preview()
        ok("idempotent (port owned -> False)", again is False)

        # (c) port_owner attributes :8000 to the preview session
        owner = await port_owner(inst, 8000)
        ok("port 8000 has an owner", owner is not None and owner.pid is not None)
        assert owner is not None
        ok(
            "owner cmdline is http.server",
            "http.server" in (owner.cmdline or ""),
            owner.cmdline or "",
        )
        ok("owner session is 'preview'", owner.session == "pmx-preview", repr(owner.session))

        # (d) the workspace is genuinely served
        await inst.write_file("index.html", b"<h1>bp02-live</h1>")
        fetch = await inst.exec_shell(
            "python3 -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000').read().decode())\"",
            timeout_s=30,
        )
        ok("workspace served on :8000", "bp02-live" in fetch.stdout, fetch.stdout.strip()[:60])

        # (e) self-heal: kill the session, port frees, ensure_preview revives it
        await session.sessions.kill_foreground("preview")
        await asyncio.sleep(1.0)
        freed = await port_owner(inst, 8000)
        ok("kill frees the port", freed is None or freed.pid is None)
        revived = await session.ensure_preview()
        ok("ensure_preview revives", revived is True)
        await asyncio.sleep(1.5)
        owner2 = await port_owner(inst, 8000)
        ok(
            "revived owner is 'preview' again",
            owner2 is not None and owner2.session == "pmx-preview",
        )

        print("ALL PASS (gvisor preview-as-session)")
    finally:
        await session.destroy()


if __name__ == "__main__":
    asyncio.run(main())
