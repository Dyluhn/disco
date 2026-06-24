"""Backend-aware preview-target resolution + process-backend control-port
containment — the SINGLE source of truth shared by the finish gate (core) and the
`verify_web_app` tool (tools, which imports this).

Why this exists (Build Soak Bug 6 + Bug 7): on the `process` backend the sandbox
shares the host network with the agent-server, so `127.0.0.1:8000` is the
agent-server's OWN control port — NOT the build's app. Auto-detecting "the first
reachable preview port" there made `verify_web_app` probe (and the agent try to
serve on) the control port: the verify never matched the deliverable → STUCK, and
an agent binding 8000 collided with + crashed the agent-server. Inside an isolated
container (gVisor/Podman) `127.0.0.1:8000` IS the app, so that backend is
unchanged. The selection RULE lives here once; each caller gathers port-ownership
its own way (the tool via `port_owners`, the gate via the in-sandbox probe below)
and feeds it to the same `resolve_preview_port`.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from ..env import disco_env

# Canonical preview ports the user-visible deliverable may serve on (ordered by
# preference). 8000 is Disco's canonical user-visible port INSIDE an isolated
# sandbox; `finish.py` re-exports this as `_PREVIEW_PORTS` (the tool imports that
# name) so the gate's preview detection and the tool's auto-detect stay identical.
PREVIEW_PORTS: tuple[int, ...] = (8000, 5173, 3000, 8080, 5000, 4321)


def _int_env(suffix: str, default: int) -> int:
    raw = disco_env(suffix, str(default))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# The Vite dev server port. On a SHARED-host dev box this is Disco's OWN frontend
# (the UI's `vite`), NOT a build's app — so on the process/local backend it is a
# reserved control/infra port like 8000/8800: a build's verify must never be pointed
# at it (that would verify the UI / an unrelated app — a FALSE PASS) and the build
# should not squat it.
_FRONTEND_DEV_PORT = 5173


def reserved_control_ports() -> frozenset[int]:
    """The host control/infra ports a SHARED-host (process/local) build must NEVER be
    verified against or bind: the agent-server port (``DISCO_AGENT_PORT``/
    ``PMX_AGENT_PORT``, default 8000), the app-server port (``DISCO_APP_PORT``/
    ``PMX_APP_PORT``, default 8800), and the frontend Vite dev port (5173 — Disco's
    own UI on a dev host, or some other app). Server ports come from the SAME env the
    servers read so a non-default deployment stays consistent. (Inside an isolated
    container these are the box's own loopback and are NOT reserved — see
    `host_shared`.)"""
    return frozenset(
        {_int_env("AGENT_PORT", 8000), _int_env("APP_PORT", 8800), _FRONTEND_DEV_PORT}
    )


@dataclass(frozen=True)
class PortOwnership:
    """The minimal ownership facts the resolver needs about a listening port: the
    listening pid (None ⇒ nobody listening) and the tmux session that owns it (the
    process backend namespaces sessions by conversation — see
    `_conversation_session_prefixes`)."""

    pid: int | None
    session: str | None = None


def _conversation_session_prefixes(conversation_id: str) -> tuple[str, ...]:
    """tmux session-name prefixes that mark a port as owned by THIS conversation on
    the process backend (`disco-{cid8}-{name}`; legacy `pmx-{cid8}-{name}`)."""
    ns = (conversation_id or "")[:8]
    return (f"disco-{ns}-", f"pmx-{ns}-")


def resolve_preview_port(
    *,
    host_shared: bool,
    owned: Mapping[int, PortOwnership],
    conversation_id: str,
    preview_ports: tuple[int, ...] = PREVIEW_PORTS,
    reserved: frozenset[int] | None = None,
) -> int | None:
    """Pick the port the live deliverable serves on — the ONE resolver shared by the
    finish gate and `verify_web_app`.

    ``host_shared=True`` (process/local — the sandbox shares the host network with
    the agent-server): return ONLY a CONVERSATION-OWNED served port (its tmux session
    matches this conversation's namespace) that is NOT a reserved control/infra port.
    If no such port exists → ``None`` (UNDETECTABLE). It deliberately does NOT fall
    back to "the first reachable / any owned port": on a shared host that could be the
    agent-server (8000), the Disco UI (5173), or ANOTHER conversation's server —
    verifying it would be a FALSE PASS against the wrong app. ``None`` routes the
    caller to the honest-unverifiable path, never a guessed target. ``host_shared=
    False`` (gVisor/Podman — isolated namespace): 8000 inside the box IS the app, so
    keep the legacy "first owned port, else the canonical port" behavior."""
    reserved = reserved_control_ports() if reserved is None else reserved

    def _owned(p: int) -> bool:
        o = owned.get(p)
        return o is not None and o.pid is not None

    if not host_shared:
        return next((p for p in preview_ports if _owned(p)), preview_ports[0])

    prefixes = _conversation_session_prefixes(conversation_id)

    def _conv_owned(p: int) -> bool:
        o = owned.get(p)
        if o is None or o.pid is None:
            return False
        sess = o.session or ""
        return any(sess.startswith(pre) for pre in prefixes)

    # ONLY a port THIS conversation serves on, never a reserved control/infra port.
    # No port the build cannot be tied to → UNDETECTABLE (never a blind guess that
    # could verify the agent-server, the UI, or a sibling conversation's app).
    for p in preview_ports:
        if p not in reserved and _conv_owned(p):
            return p
    return None


def process_safe_preview_port(
    preview_ports: tuple[int, ...] = PREVIEW_PORTS,
    reserved: frozenset[int] | None = None,
) -> int:
    """The first preview port that is NOT a reserved control/infra port — the port
    the process/local backend serves its OWN static preview on instead of 8000 (that
    preview is conversation-owned, so the resolver then finds it). This is NOT a
    verify-target guess: the resolver returns None rather than guess a port to verify
    (see `resolve_preview_port`)."""
    reserved = reserved_control_ports() if reserved is None else reserved
    for p in preview_ports:
        if p not in reserved:
            return p
    return preview_ports[-1]


# ---- in-sandbox port-ownership probe (port -> listening pid + tmux session) ---
# SINGLE source of truth for the /proc + tmux walk: tools' `port_owner.py` imports
# this exact string (its richer `PortOwner` adds cmdline; the gate only needs
# pid+session via `parse_port_ownership`). Keeping ONE probe means the gate and the
# tool can never disagree about who owns a port.
PORT_OWNER_PROBE_SRC = """\
import json
import os
import subprocess
import sys

def get_tmux_panes():
    try:
        out = subprocess.check_output(
            ['tmux', 'list-panes', '-a', '-F', '#{pane_pid} #{session_name}']
        ).decode('utf-8')
        res = {}
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                res[int(parts[0])] = parts[1]
        return res
    except Exception:
        return {}

def read_tcp_listen(port_hex):
    inodes = set()
    for net_file in ['/proc/net/tcp', '/proc/net/tcp6']:
        try:
            with open(net_file) as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 10:
                        local_addr = parts[1]
                        state = parts[3]
                        inode = parts[9]
                        if state == '0A' and local_addr.endswith(':' + port_hex):
                            inodes.add(inode)
        except Exception:
            pass
    return inodes

def main():
    ports = [int(a) for a in sys.argv[1:]]
    inode_to_port = {}
    for target_port in ports:
        for ino in read_tcp_listen(f"{target_port:04X}"):
            inode_to_port.setdefault(ino, target_port)

    port_to_pid = {}
    if inode_to_port:
        for pid_str in os.listdir('/proc'):
            if not pid_str.isdigit():
                continue
            fd_dir = f'/proc/{pid_str}/fd'
            if not os.path.isdir(fd_dir):
                continue
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(f'{fd_dir}/{fd}')
                        if link.startswith('socket:['):
                            ino = link[8:-1]
                            if ino in inode_to_port:
                                port_to_pid.setdefault(inode_to_port[ino], int(pid_str))
                    except Exception:
                        pass
            except Exception:
                pass

    panes = get_tmux_panes()
    results = []
    for target_port in ports:
        found_pid = port_to_pid.get(target_port)
        if not found_pid:
            results.append({"port": target_port, "pid": None})
            continue

        cmdline = ""
        try:
            with open(f'/proc/{found_pid}/cmdline', 'rb') as f:
                cmdline = f.read().replace(b'\\x00', b' ').decode('utf-8').strip()
        except Exception:
            pass

        current_pid = found_pid
        session = None
        for _ in range(6):
            if current_pid in panes:
                session = panes[current_pid]
                break
            try:
                with open(f'/proc/{current_pid}/stat') as f:
                    stat_data = f.read().split()
                    if len(stat_data) >= 4:
                        current_pid = int(stat_data[3])
                    else:
                        break
            except Exception:
                break

        results.append(
            {"port": target_port, "pid": found_pid, "cmdline": cmdline, "session": session}
        )

    print(json.dumps(results))

if __name__ == "__main__":
    main()
"""


def port_ownership_probe_command(ports: tuple[int, ...] | list[int]) -> str:
    """The shell command the finish gate runs in-sandbox to learn who owns each
    preview port (port → pid + tmux session)."""
    arg = " ".join(str(int(p)) for p in ports)
    return f"python3 -c {shlex.quote(PORT_OWNER_PROBE_SRC)} {arg}"


def parse_port_ownership(stdout: str) -> dict[int, PortOwnership]:
    """Parse the probe's JSON into the resolver's `PortOwnership` map. Malformed /
    empty output → empty map (a detection failure must never wedge the gate)."""
    out: dict[int, PortOwnership] = {}
    try:
        for data in json.loads(stdout or "[]"):
            out[int(data["port"])] = PortOwnership(
                pid=data.get("pid"), session=data.get("session")
            )
    except Exception:  # noqa: BLE001 — malformed probe output → no owners
        return {}
    return out


# ---- process-backend control-port containment --------------------------------
def reserved_port_command_violation(
    command: str, reserved: frozenset[int] | None = None
) -> str | None:
    """Process-backend containment, BEST-EFFORT / DEFENSE-IN-DEPTH (NOT a guarantee):
    a reason string if `command` would BIND or KILL a reserved control port (the
    agent-server / app-server), else None.

    On the process backend there is no network namespace, so an agent that binds 8000
    collides with + crashes the agent-server (observed in the live soak). This shell-
    string scan catches the COMMON shapes (`python3 -m http.server 8000`, `--port
    8000`, `fuser -k 8000`) but is TRIVIALLY BYPASSABLE — an agent can bind a reserved
    port via a method the scan doesn't match (a raw Python `socket.bind`, a renamed
    binary, an env-indirected port). Per the RCA, shell scanning CANNOT guarantee
    "never bind/collide"; the robust containment is a network namespace (or simply not
    using the process backend for hosted/multi-tenant soak) — tracked as a follow-up.
    The PRIMARY Bug-7 protection is elsewhere and stands on its own: verify no longer
    TARGETS or suggests 8000, and `ensure_preview` remaps it. This scan + `expose_port`
    refusal are an additional layer, not the load-bearing fix. Runs ONLY on the process
    backend, fails CLOSED on the clear server-bind / port-kill shapes; URL fetches
    (`http://127.0.0.1:8000/...`) are intentionally NOT matched (reading is not the
    crash vector and the gate's own probes never bind)."""
    reserved = reserved_control_ports() if reserved is None else reserved
    low = command.lower()
    for p in sorted(reserved):
        ps = re.escape(str(p))
        bind_patterns = (
            rf"http\.server\s+{ps}\b",                 # python3 -m http.server 8000
            rf"--port[=\s]+{ps}\b",                    # --port 8000 / --port=8000
            rf"\bport[=\s]+{ps}\b",                    # PORT=8000 / port 8000
            rf"-p[=\s]+{ps}\b",                        # -p 8000 (flask/uvicorn/serve)
            rf"\blisten\s+{ps}\b",                     # nginx/`listen 8000`
            # :8000 / host:8000 as a bind argument (serve -l 0.0.0.0:8000, ` :8000`)
            rf"(?:^|[\s=])(?:0\.0\.0\.0|127\.0\.0\.1|localhost|\[::1\]|::1)?:{ps}\b",
        )
        kill_patterns = (
            rf"\bfuser\b[^\n]*\b{ps}\b",               # fuser -k 8000/tcp
            rf"\blsof\b[^\n]*:{ps}\b",                 # lsof -ti:8000 | xargs kill
        )
        for pat in bind_patterns:
            if re.search(pat, low):
                return (
                    f"refused: binds reserved control port {p} (the agent-server/"
                    "app-server control port — serve your app on a different port)"
                )
        for pat in kill_patterns:
            if re.search(pat, low):
                return (
                    f"refused: targets reserved control port {p} (the agent-server/"
                    "app-server control port)"
                )
    return None


__all__ = [
    "PREVIEW_PORTS",
    "PortOwnership",
    "parse_port_ownership",
    "port_ownership_probe_command",
    "process_safe_preview_port",
    "reserved_control_ports",
    "reserved_port_command_violation",
    "resolve_preview_port",
]
