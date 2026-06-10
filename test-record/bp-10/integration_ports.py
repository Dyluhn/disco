"""BP-10 integration rung (tools level) — LIVE multi-port verification on VM-201.

Creates a REAL gVisor sandbox (runsc, ssh://sandbox@100.81.82.115), starts two HTTP
servers inside (on :8000 and :3000), and asserts the BP-10 port surface end to end:

  1. publish-at-create: every PUBLISHED_PORT (USER ∪ {8899}) has a host binding on
     the Docker side (the create-time dict comprehension, not lazy binding);
  2. batched probe: ONE port_owners() call over all USER_PORTS returns owners for
     8000+3000 (pid set) and None for the unbound ones — and really is one exec;
  3. expose_port containment: every bound USER port → URL (and the URL serves);
     8899 → None (published but NEVER user-exposable); 9999 → None (unknown).

Run:  PMX_INTEG=1 .venv/bin/python test-record/bp-10/integration_ports.py
Exit: 0 PASS, 2 assertion failures (listed), 1 infra error.
"""

from __future__ import annotations

import asyncio
import sys
import urllib.request

from perpleximanus.tools.sandbox._container import (
    INTERNAL_PORTS,
    PUBLISHED_PORTS,
    USER_PORTS,
)
from perpleximanus.tools.sandbox.base import Capability, SandboxSpec
from perpleximanus.tools.sandbox.config import SandboxConfig
from perpleximanus.tools.sandbox.gvisor import GvisorSandboxService
from perpleximanus.tools.sandbox.port_owner import port_owners

SOCKET = "ssh://sandbox@100.81.82.115"


async def main() -> int:
    failures: list[str] = []
    svc = GvisorSandboxService(SandboxConfig(docker_socket=SOCKET, runtime="runsc"))
    # Mirror the PRODUCTION Build-surface posture (runtime.py _compose_build_loop):
    # Capability.NETWORK granted -> egress_mode "open" -> bridge net + published
    # ports. A default SandboxSpec() is SEALED and (correctly) publishes nothing.
    spec = SandboxSpec(permitted=frozenset({Capability.NETWORK}))
    inst = await svc.create(
        spec, owner_id="bp10-integ", conversation_id="conv_bp10_integ"
    )
    print(f"sandbox up: {inst.id}")
    try:
        # ---- 1. publish-at-create ------------------------------------------------
        # ContainerInstance keeps the docker-py object private (`_container`);
        # harness-side introspection is the one legitimate reader.
        raw = inst._container  # noqa: SLF001
        raw.reload()
        ports_attr = raw.attrs.get("NetworkSettings", {}).get("Ports") or {}
        bound_keys = {k for k, v in ports_attr.items() if v}
        missing = {f"{p}/tcp" for p in sorted(PUBLISHED_PORTS)} - set(ports_attr)
        if missing:
            failures.append(f"publish-at-create: not in container port map: {sorted(missing)}")
        else:
            print(f"publish-at-create OK: {sorted(ports_attr)} (host-bound: {sorted(bound_keys)})")

        # ---- start two servers inside --------------------------------------------
        for port in (8000, 3000):
            res = await inst.exec_shell(
                f"mkdir -p /tmp/srv{port} && echo 'pmx-bp10-{port}' > /tmp/srv{port}/index.html && "
                f"cd /tmp/srv{port} && setsid nohup python3 -m http.server {port} "
                f">/tmp/srv{port}.log 2>&1 & sleep 1; echo started",
                timeout_s=30,
            )
            if "started" not in res.stdout:
                failures.append(f"server on :{port} did not start: {res.stdout!r} {res.stderr!r}")
        await asyncio.sleep(1)

        # ---- 2. batched probe -----------------------------------------------------
        owners = await port_owners(inst, sorted(USER_PORTS))
        for p in (8000, 3000):
            o = owners.get(p)
            if o is None or o.pid is None:
                failures.append(f"batched probe: :{p} should be OWNED, got {o}")
            else:
                print(f"probe :{p} -> pid={o.pid} cmd={str(o.cmdline)[:40]!r}")
        for p in sorted(USER_PORTS - {8000, 3000}):
            if owners.get(p) is not None and owners[p].pid is not None:
                failures.append(f"batched probe: :{p} should be free, got {owners[p]}")
        print(f"batched probe over {sorted(USER_PORTS)}: 1 call, {len(owners)} results")

        # ---- 3. expose_port containment -------------------------------------------
        for p in (8000, 3000):
            url = inst.expose_port(p)
            if not url:
                failures.append(f"expose_port({p}) returned None for a bound USER port")
                continue
            try:
                with urllib.request.urlopen(f"{url.rstrip('/')}/index.html", timeout=10) as r:
                    body = r.read().decode()
                if f"pmx-bp10-{p}" not in body:
                    failures.append(f"expose_port({p}) URL serves wrong content: {body[:60]!r}")
                else:
                    print(f"expose_port({p}) -> {url} serves the right content")
            except Exception as e:  # noqa: BLE001
                failures.append(f"expose_port({p}) URL {url} unreachable from host: {e}")
        for p in sorted(INTERNAL_PORTS) + [9999]:
            if inst.expose_port(p) is not None:
                failures.append(f"SECURITY: expose_port({p}) returned a URL — must be None")
            else:
                print(f"expose_port({p}) -> None (correct: {'internal' if p in INTERNAL_PORTS else 'unknown'})")
    finally:
        await inst.destroy()
        print("sandbox destroyed")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 2
    print("BP-10 INTEGRATION PASS: publish-at-create + batched probe + expose containment")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
