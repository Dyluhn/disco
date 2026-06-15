"""BP-01 live verification: ShellSessionManager against the gVisor backend (VM 201).

Run from a tailnet node over Docker-over-SSH:
    PMX_DOCKER_HOST=ssh://sandbox@100.81.82.115 uv run python \
        packages/tools/scripts/verify_shell_sessions_gvisor.py

Mirrors the process-backend integration scenarios in
packages/tools/tests/test_shell_sessions.py::test_integration_scenarios, adapted to
a container: (a) state persists across exec calls; (b) background server + in-container
curl + kill; (c) interactive read fed via write; (d) SessionBusy on a busy session.
"""

from __future__ import annotations

import asyncio
import os

from disco.tools.sandbox import (
    GvisorSandboxService,
    SandboxSpec,
    default_sandbox_config,
)
from disco.tools.sandbox.shell_sessions import SessionBusy, ShellSessionManager


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

    inst = await svc.create(
        SandboxSpec(memory_mb=512), owner_id="local", conversation_id="bp01-verify"
    )
    try:
        async def get_inst():
            return inst

        # Container tmux server is per-container — no namespace needed (BP-01 design).
        mgr = ShellSessionManager(get_inst)

        # tmux present in the image (rebuilt with tmux this campaign)?
        res = await inst.exec_shell("tmux -V", timeout_s=20)
        ok("tmux in image", res.exit_code == 0, res.stdout.strip())

        # (a) state persists across exec calls in one session
        out1 = await mgr.exec("main", "x=42; echo started", None)
        ok("exec returns finished", out1.running is False, f"exit={out1.exit_code}")
        out2 = await mgr.exec("main", "echo $x", None)
        ok("state persists ($x)", out2.output.strip() == "42", repr(out2.output))

        # (b) background server; view shows it; in-container curl; kill it
        srv = await mgr.exec("srv", "python3 -m http.server 8123", None)
        ok("server reports running", srv.running is True)
        await asyncio.sleep(1.5)
        view = await mgr.view("srv")
        ok("view shows server banner", "Serving HTTP" in view.output, view.output[-80:])
        curl = await inst.exec_shell(
            "python3 -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8123').status)\"",
            timeout_s=30,
        )
        ok(
            "in-container fetch 200",
            curl.stdout.strip() == "200",
            curl.stdout.strip() or curl.stderr.strip(),
        )
        kill = await mgr.kill_foreground("srv")
        ok("kill foreground", ("idle" in kill) or ("killed" in kill), kill[:80])

        # (c) interactive: read prompt fed via write
        rd = await mgr.exec("main", "read -p 'name? ' n && echo hi-$n", None)
        ok("read prompt waits", rd.running is True)
        await mgr.write("main", "dylan", press_enter=True)
        done = await mgr.wait("main", 10)
        ok("interactive answer lands", "hi-dylan" in done.output, done.output[-60:])

        # (d) SessionBusy verbatim contract
        busy = await mgr.exec("main2", "sleep 20", None)
        ok("long cmd still running", busy.running is True)
        try:
            await mgr.exec("main2", "echo nope", None)
            ok("SessionBusy raised", False)
        except SessionBusy as e:
            ok("SessionBusy raised", "Previous command not finished in session 'main2'" in str(e))

        print("ALL PASS (gvisor shell sessions)")
    finally:
        await inst.destroy()


if __name__ == "__main__":
    asyncio.run(main())
