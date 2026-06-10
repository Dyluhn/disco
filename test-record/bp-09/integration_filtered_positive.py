"""BP-09 filtered-profile POSITIVE proof — live on VM-201 (gVisor + proxy sidecar).

The worker's filtered run proved the negative (example.com blocked, exit 56) but
its positive was hollow: `pip install requests` hit "Requirement already
satisfied" — requests is pre-baked into pmx-sandbox:base, so ZERO bytes came
from pypi. This script installs packages that are NOT in the image, so success
requires real registry egress THROUGH the allowlisting proxy:

  (a) pip install cowsay   (pypi.org + files.pythonhosted.org)
  (b) npm install left-pad (registry.npmjs.org)
  (c) curl https://example.com still FAILS (the allowlist is enforced)

Spec posture is exactly the runtime's filtered branch: egress_allow =
REGISTRY_EGRESS_ALLOW, Capability.NETWORK NOT granted.

Run:  .venv/bin/python test-record/bp-09/integration_filtered_positive.py
Exit: 0 PASS, 2 assertion failures (listed), 1 infra error.
"""

from __future__ import annotations

import asyncio
import sys

from perpleximanus.tools.sandbox.base import REGISTRY_EGRESS_ALLOW, SandboxSpec
from perpleximanus.tools.sandbox.config import SandboxConfig
from perpleximanus.tools.sandbox.gvisor import GvisorSandboxService
from perpleximanus.tools.sandbox.session import SandboxSession

SOCKET = "ssh://sandbox@100.81.82.115"


async def main() -> int:
    failures: list[str] = []
    svc = GvisorSandboxService(SandboxConfig(docker_socket=SOCKET, runtime="runsc"))
    # The runtime's filtered branch: allowlist set, NETWORK capability withheld.
    spec = SandboxSpec(egress_allow=REGISTRY_EGRESS_ALLOW)
    session = SandboxSession(
        svc, spec, owner_id="bp09-filt", conversation_id="conv_bp09_filtered_pos"
    )
    try:
        inst = await session._ensure()  # noqa: SLF001 — harness-side introspection
        print(f"sandbox up: {inst.id}", flush=True)

        async def run(label: str, cmd: str, timeout: int = 180):
            r = await session.exec_shell(cmd, timeout_s=timeout)
            print(f"--- {label}: exit {r.exit_code}", flush=True)
            print((r.stdout or "")[-600:], flush=True)
            if r.stderr:
                print("STDERR:", r.stderr[-400:], flush=True)
            return r

        # (a) pip from pypi — cowsay is NOT in the image.
        r = await run(
            "pip install cowsay (NOT preinstalled)",
            "pip3 install --break-system-packages cowsay"
            ' && python3 -c "import cowsay; print(cowsay.__name__, \'ok\')"',
        )
        if r.exit_code != 0 or "Downloading" not in r.stdout:
            dl = "present" if "Downloading" in r.stdout else "ABSENT"
            failures.append(
                "pip install cowsay did not DOWNLOAD from pypi through the filter "
                f"(exit {r.exit_code}; 'Downloading' {dl})"
            )

        # (b) npm from registry.npmjs.org — left-pad is NOT in the image.
        r = await run(
            "npm install left-pad (NOT preinstalled)",
            "mkdir -p /tmp/bp09 && cd /tmp/bp09 && npm init -y >/dev/null"
            " && npm install left-pad && node -e \"console.log(require('left-pad')('x', 3))\"",
        )
        if r.exit_code != 0:
            failures.append(f"npm install left-pad failed through the filter (exit {r.exit_code})")

        # (c) the allowlist still blocks arbitrary egress.
        r = await run(
            "curl example.com (must FAIL)", "curl -sS --max-time 20 https://example.com", 40
        )
        if r.exit_code == 0:
            failures.append("curl https://example.com SUCCEEDED — filter not enforced")
    finally:
        await session.destroy()

    if failures:
        for f in failures:
            print(f"FAIL: {f}", flush=True)
        return 2
    print("FILTERED POSITIVE PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
