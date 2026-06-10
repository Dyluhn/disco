"""BP-08 integration rung 3 — LIVE kernel-gateway verification on VM-201 (gVisor).

Acceptance 2(a) and 2(c) over the GATEWAY transport, through the REAL production
path: SandboxSession(gvisor).kernel -> GatewayKernel -> lazy `jupyter kernelgateway`
in tmux session `__kernel` inside the runsc container -> host mapping of INTERNAL
port 8899 (BP-10's publish-at-create; never user-exposable).

  (a) state:     cell1 `x = 41`; cell2 `x + 1` -> 42 — RAM state, no pickle;
  (c) interrupt: `while True: pass`, timeout_s=3 -> timed_out=True, restarted=False,
                 and a follow-up cell still sees `x` (state survived the interrupt).

Plus the BP-10 containment cross-check while the box is up: expose_port(8899) is None.

Run:  .venv/bin/python test-record/bp-08/integration_kernel_gvisor.py
Exit: 0 PASS, 2 assertion failures (listed), 1 infra error.
"""

from __future__ import annotations

import asyncio
import sys

from perpleximanus.tools.sandbox.base import Capability, SandboxSpec
from perpleximanus.tools.sandbox.config import SandboxConfig
from perpleximanus.tools.sandbox.gvisor import GvisorSandboxService
from perpleximanus.tools.sandbox.session import SandboxSession

SOCKET = "ssh://sandbox@100.81.82.115"


async def main() -> int:
    failures: list[str] = []
    svc = GvisorSandboxService(SandboxConfig(docker_socket=SOCKET, runtime="runsc"))
    # Production Build-surface posture: NETWORK granted -> published ports exist,
    # so the 8899 internal mapping the gateway transport rides is live.
    spec = SandboxSpec(permitted=frozenset({Capability.NETWORK}))
    session = SandboxSession(
        svc, spec, owner_id="bp08-integ", conversation_id="conv_bp08_integ"
    )
    try:
        inst = await session._ensure()  # noqa: SLF001 — harness-side introspection
        print(f"sandbox up: {inst.id}")

        # containment cross-check (BP-10 contract BP-08 depends on)
        if inst.expose_port(8899) is not None:
            failures.append("SECURITY: expose_port(8899) returned a URL — must be None")
        else:
            print("expose_port(8899) -> None (internal plumbing stays internal)")

        k = await session.kernel
        print(f"kernel transport: {type(k).__name__}")
        if type(k).__name__ != "GatewayKernel":
            failures.append(f"expected GatewayKernel on gvisor, got {type(k).__name__}")

        # ---- 2(a) state ----------------------------------------------------------
        r1 = await k.execute("x = 41", timeout_s=60)
        if not r1.ok:
            failures.append(f"2(a) cell1 failed: {r1}")
        r2 = await k.execute("x + 1", timeout_s=30)
        if not (r2.ok and r2.result_repr and "42" in r2.result_repr):
            failures.append(f"2(a) cell2: expected result 42, got {r2}")
        else:
            print(f"2(a) state PASS: x=41 then x+1 -> {r2.result_repr}")

        # ---- 2(c) interrupt ------------------------------------------------------
        r3 = await k.execute("while True: pass", timeout_s=3)
        if not r3.timed_out:
            failures.append(f"2(c): expected timed_out=True, got {r3}")
        if r3.restarted:
            failures.append(f"2(c): interrupt escalated to RESTART (state lost): {r3}")
        r4 = await k.execute("x + 1", timeout_s=30)
        if not (r4.ok and r4.result_repr and "42" in r4.result_repr):
            failures.append(f"2(c) post-interrupt state lost: {r4}")
        else:
            print(
                f"2(c) interrupt PASS: timed_out={r3.timed_out} restarted={r3.restarted}, "
                f"then x+1 -> {r4.result_repr} (state preserved)"
            )

        await k.shutdown()
    finally:
        await session.destroy()
        print("sandbox destroyed")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 2
    print("BP-08 GVISOR INTEGRATION PASS: state + interrupt over the kernel gateway")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
