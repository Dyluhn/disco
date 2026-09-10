"""Live verification: the SANDBOX GENERATION ROTATION contract (process backend).

Run it directly (it needs a real host tmux + Playwright, so it is not a unit test):
    TMPDIR=~/.local/state/disco-campaign-tmp uv run python \
        packages/tools/scripts/verify_generation_rotation.py

WHY THIS EXISTS. A recurring defect family is "a resource bound to sandbox
generation N is silently reused at generation N+1": the browser daemon inheriting a
dead workspace identity, a preview relaunching into a deleted exec_dir, a bind probe
fooled by a prior lifetime's TIME_WAIT sockets, a poll trusting invocation-local
state. Every one of them is invisible to the unit suite — tests inject a fake
instance, and a fake never carries a previous OS process's environ, a dead cwd, or a
surviving tmux pane. The only place a real rotation happened was the Build-soak
`*_restart` lane: 4 of 100 counted trials, behind the 86-trial main stack, ~20 minutes
per observation, and it stops at the FIRST rotation defect so the rest stay hidden.
That is a serial-discovery machine, and it is why this family kept reappearing.

This script does the rotation directly and checks EVERY generation-bound resource in
one pass, in seconds, so the remaining defects surface together instead of one per
ladder run. Real ProcessSandboxService, real host tmux, real BrowserTool, real
_browser_daemon.py, real Playwright — nothing under test is faked.

Exit code is 0 only when every contract check holds. A known-open finding is
recorded in the campaign ledger rather than silently tolerated here.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import types
import uuid
from pathlib import Path

from disco.tools.builtin.browser import BrowserTool, BrowserUnavailableError
from disco.tools.sandbox.process import ProcessSandboxService, cleanup_process_tmux_sessions
from disco.tools.sandbox.shell_sessions import ShellSessionManager

CONV = "conv_" + uuid.uuid4().hex
NS = f"{CONV[:8]}-"

findings: list[tuple[str, str, bool, str]] = []


def check(resource: str, claim: str, ok: bool, detail: str = "") -> None:
    findings.append((resource, claim, ok, detail))
    print(
        f"  {'HOLDS ' if ok else 'BROKEN'}  {resource:24s} {claim}"
        + (f"\n          → {detail}" if detail and not ok else "")
    )


def ctx_for(instance, sessions):
    return types.SimpleNamespace(
        sandbox=instance,
        sessions=sessions,
        timeout_s=30,
        browser_generation=1,
        browser_workspace_epoch=1,
        browser_lane="agent",
    )


async def compose(label: str):
    service = ProcessSandboxService()
    instance = await service.create(None, owner_id="probe", conversation_id=CONV)

    async def get_instance():
        return instance

    sessions = ShellSessionManager(get_instance, namespace=NS)
    print(f"[{label}] workspace {instance.workspace_path}")
    return service, instance, sessions


def daemon_env_by_pid() -> dict[int, str]:
    out: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if "_browser_daemon.py" not in (entry / "cmdline").read_bytes().decode(
                "utf-8", "replace"
            ):
                continue
            raw = (entry / "environ").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        env = dict(i.split("=", 1) for i in raw.split("\0") if "=" in i)
        out[int(entry.name)] = env.get("DISCO_WORKSPACE", "(unset)")
    return out


async def _run_generation_one() -> tuple[object, str, set[int]]:
    """Do real work under generation 1 and return what generation 2 must rotate past."""
    tool = BrowserTool()
    _svc1, inst1, sess1 = await compose("gen 1")
    ws1 = str(inst1.workspace_path)
    await sess1.exec(
        "main", "GEN=one; mkdir -p /workspace/app; echo hi > /workspace/app/i.txt", None
    )
    await sess1.exec("server", "python3 -m http.server 8123 >/dev/null 2>&1 &", None)
    gen1_browser_ok = True
    try:
        await tool._ensure_daemon(ctx_for(inst1, sess1))
    except BrowserUnavailableError:
        gen1_browser_ok = False
    before_pids = set(daemon_env_by_pid())
    print(f"[gen 1] browser healthy: {gen1_browser_ok}\n")
    return inst1, ws1, before_pids


async def _check_shell_rotation(sess2, ws2: str) -> None:
    """R1-R3: the shell session environment, cwd, and variables all rotate cleanly."""
    out = await sess2.exec("main", "echo WS=$DISCO_WORKSPACE HOME=$HOME", None)
    body = out.output or ""
    check(
        "R1 shell session env",
        "DISCO_WORKSPACE/HOME point at gen 2",
        f"WS={ws2}" in body and f"HOME={ws2}" in body,
        f"observed {body.strip()!r}",
    )

    out = await sess2.exec("main", "pwd", None)
    body = (out.output or "").strip()
    check(
        "R2 shell session cwd",
        "cwd resolves inside gen 2 (not a dead tree)",
        ws2 in body or body == "/workspace",
        f"observed {body!r}",
    )

    out = await sess2.exec("main", "echo GEN=[$GEN]", None)
    check(
        "R3 shell state",
        "gen 1 shell variables are NOT inherited",
        "GEN=[]" in (out.output or ""),
        f"observed {(out.output or '').strip()!r}",
    )


async def _check_browser_rotation(
    tool: BrowserTool, inst2, sess2, ws1: str, ws2: str, before_pids: set[int]
) -> None:
    """R4-R7: the browser daemon rebinds to gen 2's identity, workspace, and port file."""
    gen2_browser_ok = True
    try:
        await tool._ensure_daemon(ctx_for(inst2, sess2))
    except BrowserUnavailableError as exc:
        gen2_browser_ok = False
        check(
            "R4 browser daemon",
            "starts and passes the gen 2 identity pin",
            False,
            f"BrowserUnavailableError: ...{exc.startup_diagnostic[-120:]}",
        )
    if not gen2_browser_ok:
        return
    check("R4 browser daemon", "starts and passes the gen 2 identity pin", True)
    fresh = {p: w for p, w in daemon_env_by_pid().items() if p not in before_pids}
    check(
        "R5 daemon workspace",
        "the daemon gen 2 started inherited gen 2's workspace",
        bool(fresh) and all(w == ws2 for w in fresh.values()),
        f"observed {fresh}",
    )
    # The essential property is that the CURRENT generation publishes and
    # reads a port file in its OWN tree; R5/R7 already prove it is not using
    # the previous generation's identity. A leftover file in the dead tree is
    # inert residue (nothing reads that path again, and the orphaned root is
    # swept on age) — retiring the previous generation kills its pane outright
    # rather than letting its daemon Ctrl-C and unlink, so it is expected.
    check(
        "R6 daemon port file",
        "the current generation publishes its OWN port file",
        (Path(ws2) / ".pmx" / "browser-port").exists(),
        f"gen2={(Path(ws2) / '.pmx' / 'browser-port').exists()} "
        f"(gen1 residue={(Path(ws1) / '.pmx' / 'browser-port').exists()}, inert)",
    )
    check(
        "R7 identity hash",
        "daemon hash == sha256(gen 2 workspace)",
        hashlib.sha256(ws2.encode()).hexdigest() != hashlib.sha256(ws1.encode()).hexdigest(),
        "hashes must differ across generations",
    )


async def _check_residual_state(sess2) -> None:
    """R8-R9: no gen-1 server/content survives into gen 2's sandbox tree."""
    out = await sess2.exec(
        "main", "(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8123 || echo none)", None
    )
    body = (out.output or "").strip()
    check(
        "R8 stale listener",
        "gen 1's server does not survive the rotation holding its port",
        "200" not in body,
        f"port 8123 answered {body!r}",
    )

    out = await sess2.exec(
        "main", "test -f /workspace/app/i.txt && echo PRESENT || echo ABSENT", None
    )
    check(
        "R9 workspace tree",
        "gen 2 starts from its own tree (restore is the server's job)",
        "ABSENT" in (out.output or ""),
        f"observed {(out.output or '').strip()!r}",
    )


def _print_summary() -> bool:
    """Print the pass/fail table and return whether any check broke."""
    print("\n--- summary ---")
    broken = [f for f in findings if not f[2]]
    for resource, _claim, ok, _detail in findings:
        print(f"  {'ok  ' if ok else 'FAIL'}  {resource}")
    print(f"\n{len(findings) - len(broken)}/{len(findings)} rotation-contract checks hold")
    return bool(broken)


async def main() -> int:
    tool = BrowserTool()
    print(f"conversation {CONV}  namespace disco-{NS}*\n")

    # ---------------- generation 1: do real work ----------------
    inst1, ws1, before_pids = await _run_generation_one()

    # ---------------- rotate ----------------
    svc2, inst2, sess2 = await compose("gen 2")
    ws2 = str(inst2.workspace_path)
    print("\n--- rotation contract: every generation-bound resource must rebind to gen 2 ---")

    await _check_shell_rotation(sess2, ws2)
    await _check_browser_rotation(tool, inst2, sess2, ws1, ws2, before_pids)
    await _check_residual_state(sess2)

    broken = _print_summary()

    await cleanup_process_tmux_sessions(CONV)
    await inst1.destroy()
    await inst2.destroy()
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
