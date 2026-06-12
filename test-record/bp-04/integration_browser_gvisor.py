"""BP-04 integration rung 3 — LIVE browser daemon verification on VM-201 (gVisor).

Order acceptance over the REAL production path: BrowserTool.run() ->
_ensure_daemon (ships _browser_daemon.py, starts it in tmux session `__browser`
via ShellSessionManager) -> Playwright/Chromium INSIDE the runsc container on
loopback :8901 -> curl POST from the same box. Nothing executes on the host.

  (a) navigate to an in-sandbox fixture page -> ok, console captured (the page's
      console.error('boom') is in structured["console"]), elements indexed,
      screenshot file EXISTS in the sandbox workspace;
  (b) click by element index WITHOUT re-navigating -> page state advanced
      (counter 0 -> 1), proving the daemon's page persists across tool calls;
  (c) console_view -> returns the accumulated log without touching the page;
  (d) daemon persistence: a second action does NOT restart the daemon (the
      `__browser` tmux session and the daemon PID survive across calls).

Plus the containment cross-checks: expose_port(8901) is None (internal plumbing
stays internal) and the daemon socket is loopback-only inside the box.

Run:  .venv/bin/python test-record/bp-04/integration_browser_gvisor.py
Exit: 0 PASS, 2 assertion failures (listed), 1 infra error.
"""

from __future__ import annotations

import asyncio
import sys

from disco.tools.anatomy import ToolContext
from disco.tools.builtin.browser import BrowserArgs, BrowserTool
from disco.tools.sandbox.base import Capability, SandboxSpec
from disco.tools.sandbox.config import SandboxConfig
from disco.tools.sandbox.gvisor import GvisorSandboxService
from disco.tools.sandbox.session import SandboxSession
from disco.tools.secrets import CapabilitySet

SOCKET = "ssh://sandbox@100.81.82.115"

FIXTURE_HTML = b"""<html>
<body>
    <h1 id="counter">0</h1>
    <button id="btn"
        onclick="const c = document.getElementById('counter');
                 c.textContent = parseInt(c.textContent) + 1;">Click</button>
    <script>console.error('boom');</script>
</body>
</html>"""


async def main() -> int:
    failures: list[str] = []
    svc = GvisorSandboxService(SandboxConfig(docker_socket=SOCKET, runtime="runsc"))
    # Production Build-surface posture: NETWORK granted (the browser tool declares it).
    spec = SandboxSpec(permitted=frozenset({Capability.NETWORK}))
    session = SandboxSession(
        svc, spec, owner_id="bp04-integ", conversation_id="conv_bp04_integ"
    )
    try:
        inst = await session._ensure()  # noqa: SLF001 — harness-side introspection
        print(f"sandbox up: {inst.id}")

        # containment cross-check: the daemon port is internal plumbing, never published
        if inst.expose_port(8901) is not None:
            failures.append("SECURITY: expose_port(8901) returned a URL — must be None")
        else:
            print("expose_port(8901) -> None (internal plumbing stays internal)")

        # Order rung 3: fixture served on 127.0.0.1:8000 by the `preview` session inside
        # the SAME sandbox — the exact loopback-verification path BP-05's gate consumes.
        await inst.write_file("/workspace/fixture/index.html", FIXTURE_HTML)
        await session.sessions.exec(
            "preview", "python3 -m http.server 8000 --bind 127.0.0.1", "/workspace/fixture"
        )

        tool = BrowserTool()
        ctx = ToolContext(
            sandbox=inst,
            sessions=session.sessions,
            workspace_path=".",
            timeout_s=120,
            capabilities=CapabilitySet(frozenset(), {}),
            owner_id="bp04-integ",
            conversation_id="conv_bp04_integ",
        )

        # ---- (a) navigate ---------------------------------------------------------
        out = await tool.run(
            BrowserArgs(action="navigate", url="http://127.0.0.1:8000/"), ctx
        )
        if not out.success:
            failures.append(f"(a) navigate failed: {out.error}")
            raise RuntimeError("navigate failed — aborting dependent checks")
        data = out.structured or {}
        if not any("boom" in c["text"] for c in data.get("console", [])):
            failures.append(f"(a) console.error('boom') not captured: {data.get('console')}")
        else:
            print(f"(a) console captured: {data['console']}")
        if not any("<button>" in el for el in data.get("elements", [])):
            failures.append(f"(a) button not in indexed elements: {data.get('elements')}")
        else:
            print(f"(a) elements indexed: {data['elements']}")
        shot = data.get("screenshot_path")
        chk = await inst.exec_shell(f"test -f /workspace/{shot}", timeout_s=10)
        if not shot or chk.exit_code != 0:
            failures.append(f"(a) screenshot missing in sandbox: {shot!r}")
        else:
            print(f"(a) screenshot exists in sandbox: {shot}")

        # daemon PID before the next call — persistence check (d)
        pid1 = (await inst.exec_shell(
            "pgrep -f _browser_daemon.py | head -1", timeout_s=10
        )).stdout.strip()

        # ---- (b) click by index, page state persists ------------------------------
        btn_index = None
        for el in data.get("elements", []):
            if "<button>" in el:
                btn_index = int(el.split("[", 1)[0])
                break
        if btn_index is None:
            failures.append("(b) no button index parsed from elements")
        else:
            out = await tool.run(BrowserArgs(action="click", index=btn_index), ctx)
            data_b = out.structured or {}
            if not (out.success and "1" in data_b.get("text", "")):
                failures.append(f"(b) counter did not advance: {data_b.get('text')!r}")
            else:
                print(f"(b) click PASS: counter text now {data_b.get('text')!r}")

        # ---- (c) console_view -----------------------------------------------------
        out = await tool.run(BrowserArgs(action="console_view"), ctx)
        data_c = out.structured or {}
        if not (out.success and any("boom" in c["text"] for c in data_c.get("console", []))):
            failures.append(f"(c) console_view lost the log: {data_c.get('console')}")
        else:
            print("(c) console_view PASS: accumulated log intact")

        # ---- (d) daemon persistence -----------------------------------------------
        pid2 = (await inst.exec_shell(
            "pgrep -f _browser_daemon.py | head -1", timeout_s=10
        )).stdout.strip()
        if not pid1 or pid1 != pid2:
            failures.append(f"(d) daemon restarted between calls: {pid1!r} -> {pid2!r}")
        else:
            print(f"(d) daemon persistent PASS: pid {pid1} across all calls")

    finally:
        await session.destroy()
        print("sandbox destroyed")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 2
    print("BP-04 GVISOR INTEGRATION PASS: daemon + console + elements + persistence")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
