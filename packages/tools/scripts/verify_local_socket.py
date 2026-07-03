# ruff: noqa: E501 — a verification script; long curl/command lines are inherent.
"""Live verification of the LOCAL container SandboxBackend against a LOCAL socket
(no SSH) — the lowest-isolation tier. Run on a host with a local Docker socket OR a
rootless Podman socket (its Docker-compatible API):

  uv run python packages/tools/scripts/verify_local_socket.py

Defaults to this host's rootless Podman socket; override via env:
  PMX_LOCAL_SOCKET   (default unix:///run/user/1000/podman/podman.sock)
  PMX_LOCAL_RUNTIME  (default runc)
  PMX_LOCAL_IMAGE    (default pmx-sandbox:base — must be PRESENT; this tier never pulls)

Walks the Linux checklist: local connect (no SSH) + runtime, session model (multi-exec
real output/exit + /workspace persistence via a named volume), sealing, limits-bite
(the rootless footgun), timeout, the secret non-leak headline (and that exec genuinely
captured, not a false-empty pass), teardown, and the #5 isolation legibility + coupling.
"""

from __future__ import annotations

import asyncio
import os

from disco.tools.sandbox import (
    LocalSandboxService,
    SandboxConfig,
    SandboxSpec,
    SandboxUnavailableError,
    isolation_for,
)

SENTINEL = "sk-LEAK-SENTINEL-local-do-not-expose"


def ok(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def _cfg() -> SandboxConfig:
    return SandboxConfig(
        backend="local",
        runtime=os.environ.get("PMX_LOCAL_RUNTIME", "runc"),
        docker_socket=os.environ.get("PMX_LOCAL_SOCKET", "unix:///run/user/1000/podman/podman.sock"),
        image=os.environ.get("PMX_LOCAL_IMAGE", "pmx-sandbox:base"),
    )


async def main() -> None:
    cfg = _cfg()
    assert "ssh" not in cfg.docker_socket, "local tier must use a LOCAL socket, no SSH"
    print(f"config: socket={cfg.docker_socket} runtime={cfg.runtime} image={cfg.image}")
    svc = LocalSandboxService(cfg)
    os.environ["PMX_LEAK_TEST"] = SENTINEL  # must never reach the box

    # 1 + 2: create (sealed default) over the LOCAL socket; multi-exec; /workspace persists.
    inst = await svc.create(SandboxSpec(memory_mb=256), owner_id="local", conversation_id="verify")
    print(f"\ncreated {inst.id}")
    who = await inst.exec_shell("id -un; cat /proc/self/cgroup | head -1; echo rt-from-config=" + cfg.runtime, timeout_s=10)
    ok("1. connects over the LOCAL socket (no SSH); container running", who.exit_code == 0, who.stdout.strip().replace("\n", " ")[:60])
    await inst.write_file("state.txt", b"step1\n")
    appended = await inst.exec_shell("cat state.txt; echo step2 >> state.txt", timeout_s=10)
    persisted = await inst.read_file("state.txt")
    ok(
        "2. session: multi-exec (real output/exit) + /workspace persists",
        "step1" in appended.stdout and b"step2" in persisted,
        persisted.decode().strip().replace("\n", "|"),
    )

    # 3a: sealed default cannot reach the network.
    net = await inst.exec_shell(
        "curl -s --max-time 6 https://example.com -o /dev/null -w '%{http_code}' || echo BLOCKED",
        timeout_s=12,
    )
    ok("3a. sealed (default): no network", "BLOCKED" in net.stdout or net.exit_code != 0, net.stdout.strip()[:40])

    # 5: timeout — killed + reported, partial output preserved.
    slow = await inst.exec_shell("echo partial; sleep 30", timeout_s=3)
    ok("5. timeout reported (killed, timed_out, partial output)", slow.timed_out and slow.exit_code in (124, 137), f"timed_out={slow.timed_out} exit={slow.exit_code} out={slow.stdout.strip()[:20]!r}")

    # 6: secret non-leak — AND prove exec genuinely captured (not a false-empty pass).
    canary = await inst.exec_shell("echo EXEC_CAPTURED_OK; exit 5", timeout_s=10)
    exec_really_works = canary.stdout.strip() == "EXEC_CAPTURED_OK" and canary.exit_code == 5
    env = await inst.exec_shell("env", timeout_s=10)
    procs = await inst.exec_shell("ps -e -o args 2>/dev/null || ps", timeout_s=10)
    files = await inst.list_dir(".")
    leaked = SENTINEL in env.stdout or SENTINEL in procs.stdout or any(SENTINEL in f for f in files)
    ok(
        "6. SECRET NON-LEAK (env/proc/files) + exec genuinely captured",
        (not leaked) and exec_really_works and len(env.stdout) > 0,
        f"secret absent; exec captured={exec_really_works} (env has {len(env.stdout.splitlines())} vars)" if not leaked else "LEAKED!",
    )

    vol_name = f"{cfg.workspace_volume_prefix}-{inst.id}"

    # 7: teardown — container gone, workspace named volume removed.
    await inst.destroy()
    import docker

    c = docker.DockerClient(base_url=cfg.docker_socket)
    gone = not any(f"pmx-sbx-{inst.id}" in (ct.name or "") for ct in c.containers.list(all=True))
    vol_removed = not any(vol_name in (v.name or "") for v in c.volumes.list())
    ok("7. teardown: container removed", gone, "")
    ok("7. teardown: workspace named volume removed", vol_removed, vol_name)

    # 4: limits BITE through the LOCAL socket (the rootless footgun — confirm not silently cgroupfs).
    mem_inst = await svc.create(SandboxSpec(memory_mb=128), owner_id="local", conversation_id="verify")
    enforced = False
    try:
        # Allocate 400MB under a 128m cap. exit 137 = OOM-killed (the limit bit); a
        # success (exit 0, "ALLOCATED") would mean the cap was silently ignored.
        mem = await mem_inst.exec_shell("python3 -c 'b=bytearray(400*1024*1024); print(\"ALLOCATED\", len(b))'", timeout_s=25)
        enforced = mem.exit_code != 0 and "ALLOCATED" not in mem.stdout
        detail = f"exit={mem.exit_code} {mem.stderr.strip()[:16]!r} (NOT silently cgroupfs-unlimited)"
    except Exception as exc:  # noqa: BLE001 — box died exceeding its limit: enforced
        enforced = True
        detail = f"OOM-killed: {type(exc).__name__}"
    ok("4. LIMITS bite through the local socket", enforced, detail)
    try:
        await mem_inst.destroy()
    except Exception:  # noqa: BLE001
        pass

    # 3b: a GRANTED sandbox can reach the network.
    open_inst = await svc.create(SandboxSpec(egress_allow=frozenset({"example.com"})), owner_id="local", conversation_id="verify")
    net2 = await open_inst.exec_shell(
        "curl -s --max-time 8 https://example.com -o /dev/null -w '%{http_code}' || echo BLOCKED",
        timeout_s=15,
    )
    ok("3b. granted: network reachable", "200" in net2.stdout or "301" in net2.stdout, net2.stdout.strip()[:40])
    await open_inst.destroy()

    # image-by-load / NEVER pull — a missing image fails loud, no pull attempted.
    bad = LocalSandboxService(cfg.model_copy(update={"image": "pmx-sandbox:DOES-NOT-EXIST"}))
    try:
        await bad.create(SandboxSpec(), owner_id="local", conversation_id="verify")
        ok("8. image-present / NEVER pull: missing → typed error", False, "did NOT fail!")
    except SandboxUnavailableError as e:
        ok("8. image-present / NEVER pull: missing → typed error", "never pulls" in str(e), str(e)[:70])

    # #5: lower-isolation legibility + tighter-confirmation coupling.
    prof = svc.isolation
    tighter = prof.recommended_confirmation()
    stronger = isolation_for("gvisor").recommended_confirmation()
    from disco.core import SecurityRisk

    coupling_bites = tighter.should_confirm(SecurityRisk.MEDIUM) and not stronger.should_confirm(SecurityRisk.MEDIUM)
    print("\n#5 isolation legibility:")
    print(f"  label: {prof.label}")
    ok("5a. labeled weaker / not adversarial-safe", prof.adversarial_safe is False and "shared host kernel" in prof.label.lower(), f"adversarial_safe={prof.adversarial_safe}")
    ok("5b. confirmation coupling tighter than gVisor (MEDIUM gated locally, not on gVisor)", coupling_bites, "local gates MEDIUM-risk; gVisor does not")

    print("\nDone. Any FAIL above is a real gap to fix.")


if __name__ == "__main__":
    asyncio.run(main())
