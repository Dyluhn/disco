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

from ..auth import local_preview_gateway_ports
from ..env import disco_env

# Canonical preview ports the user-visible deliverable may serve on (ordered by
# preference). 8000 is Disco's canonical user-visible port INSIDE an isolated
# sandbox; `finish.py` re-exports this as `_PREVIEW_PORTS` (the tool imports that
# name) so the gate's preview detection and the tool's auto-detect stay identical.
PREVIEW_PORTS: tuple[int, ...] = (8000, 5173, 3000, 8080, 5000, 4321)

# Process sandboxes share the host TCP namespace. Their managed runtimes cannot
# be limited to the few framework-default ports above: concurrent conversations
# need independent listeners, while container backends must retain their small
# create-time published allowlist. This bounded non-ephemeral range is therefore
# ONLY for platform-owned PreviewManager runtimes on a shared host. It is never
# added to USER_PORTS and never becomes an agent-selectable generic port surface.
DEFAULT_MANAGED_PREVIEW_PORT_START = 10_000
DEFAULT_MANAGED_PREVIEW_PORT_COUNT = 8_000


def _managed_host_preview_port_config() -> tuple[int, int, frozenset[int]]:
    raw_start = disco_env(
        "MANAGED_PREVIEW_PORT_START",
        str(DEFAULT_MANAGED_PREVIEW_PORT_START),
    )
    raw_count = disco_env(
        "MANAGED_PREVIEW_PORT_COUNT",
        str(DEFAULT_MANAGED_PREVIEW_PORT_COUNT),
    )
    try:
        start = int(str(raw_start).strip())
        count = int(str(raw_count).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("managed Preview port range must be numeric") from exc
    if start < 1_024 or count < 1 or start + count - 1 > 65_535:
        raise ValueError("managed Preview port range is outside the user-port range")
    excluded = reserved_control_ports() | frozenset(local_preview_gateway_ports())
    return start, count, excluded


def managed_host_preview_ports() -> tuple[int, ...]:
    """Configured host-only port candidates for platform-managed runtimes.

    The configured interval is finite because TCP ports are finite, but unlike
    the former four-entry framework-default pool it scales to ordinary concurrent
    builds. Control servers and DNS-free Preview gateway listeners are excluded
    from the returned authority even when an operator configures an overlapping
    interval.
    """

    start, count, excluded = _managed_host_preview_port_config()
    return tuple(port for port in range(start, start + count) if port not in excluded)


def is_managed_host_preview_port(port: object) -> bool:
    """Whether ``port`` may carry one exact host-managed Preview generation."""

    if type(port) is not int:
        return False
    start, count, excluded = _managed_host_preview_port_config()
    return start <= port < start + count and port not in excluded


def active_managed_preview_ports(sandbox: object) -> tuple[int, ...]:
    """Read the exact canonical host port currently selected by this manager.

    Consumers use this single-value list for ownership probes. They must never
    scan the whole host range, or let an older still-running manager session
    supersede the product-selected Preview generation.
    """

    manager = getattr(sandbox, "_preview_manager", None)
    if manager is None:
        return ()
    try:
        port = manager.canonical_port()
    except Exception:  # noqa: BLE001 - malformed optional collaborator is no authority
        return ()
    return (port,) if is_managed_host_preview_port(port) else ()


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
_DEFAULT_AGENT_PORT = 8000
_DEFAULT_APP_PORT = 8800
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
    # Keep the conventional ports protected even when a disposable stack runs on
    # alternates. A shared-host process sandbox can otherwise collide with the
    # operator's active default stack while the campaign itself listens on 181xx.
    # Also reserve the effective UI port; the old fixed-5173-only rule could target
    # another suite's Vite server when LIVE_PORT/UI_PORT selected an alternate.
    return frozenset(
        {
            _DEFAULT_AGENT_PORT,
            _DEFAULT_APP_PORT,
            _FRONTEND_DEV_PORT,
            _int_env("AGENT_PORT", _DEFAULT_AGENT_PORT),
            _int_env("APP_PORT", _DEFAULT_APP_PORT),
            _int_env("UI_PORT", _FRONTEND_DEV_PORT),
        }
    )


def backend_shares_host_network(sandbox: object) -> bool:
    """True iff the sandbox shares the host network namespace (the ``process``
    backend) — where ``127.0.0.1:<reserved>`` IS the agent-server and control ports
    MUST stay reserved. Isolated container backends (gVisor/Podman/local) return
    False: their loopback is the box's own, so ``:8000`` is the build's verifiable
    preview, not a control port.

    Prefers the explicit ``shares_host_network`` flag the instances declare; falls
    back to the legacy ``workspace_path``-presence heuristic ONLY when the flag is
    absent (e.g. a test fake), preserving prior behavior there. The bug this fixes:
    isolated containers ALSO have a ``workspace_path``, so the old heuristic
    misclassified them as host-shared and excluded their real ``:8000`` preview."""
    flag = getattr(sandbox, "shares_host_network", None)
    if isinstance(flag, bool):
        return flag
    return getattr(sandbox, "workspace_path", None) is not None


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


def target_url_port(url: str) -> int | None:
    """The explicit port of a preview URL (scheme optional). None when the url is
    empty / unparseable / carries NO explicit port — a port-less url cannot be
    confirmed as a served preview, so the shared-host validator rejects it."""
    from urllib.parse import urlsplit

    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "http://" + u
    try:
        return urlsplit(u).port
    except ValueError:
        return None


def explicit_target_allowed(
    *,
    port: int | None,
    host_shared: bool,
    owned: Mapping[int, PortOwnership],
    conversation_id: str,
    reserved: frozenset[int] | None = None,
) -> bool:
    """Whether an AGENT-SUPPLIED explicit verify target is a valid preview to verify
    — the SAME rule as auto-detect, applied to a hand-picked url so it can't bypass
    the resolver. ISOLATED backend (`host_shared=False`): always honored (8000 is the
    box's app; no shared-host foreign-port risk). SHARED host: honored ONLY for a
    CONVERSATION-OWNED, NON-reserved port — never a control/UI port (8000/8800/5173),
    never a sibling conversation's or an unattributable port (those would be a FALSE
    PASS against the wrong app)."""
    if not host_shared:
        return True
    if port is None:
        return False
    reserved = reserved_control_ports() if reserved is None else reserved
    if port in reserved:
        return False
    o = owned.get(port)
    if o is None or o.pid is None:
        return False
    prefixes = _conversation_session_prefixes(conversation_id)
    return any((o.session or "").startswith(pre) for pre in prefixes)


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
            parts = line.strip().split(maxsplit=1)
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

def parent_pid(pid):
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('PPid:'):
                    return int(line.split(':', 1)[1].strip())
    except Exception:
        pass
    return None

def find_tmux_session(pid, panes):
    # npm/framework launchers can put several shells and Node processes between
    # the listening process and tmux's pane shell. Walk the complete bounded
    # ancestry instead of assuming a shallow process tree.
    current_pid = pid
    visited = set()
    while True:
        if current_pid in panes:
            return panes[current_pid]
        if current_pid <= 1 or current_pid in visited:
            break
        visited.add(current_pid)
        current_pid = parent_pid(current_pid)
        if current_pid is None:
            break
    return None

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

        session = find_tmux_session(found_pid, panes)

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
            out[int(data["port"])] = PortOwnership(pid=data.get("pid"), session=data.get("session"))
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
    # Bug 16 — the refusal must be ACTIONABLE: name the rejected port, the whole
    # reserved set, AND a concrete safe replacement, so a model that hits it can recover
    # to a non-reserved port instead of retrying the same reserved one and STUCKing.
    reserved_csv = ", ".join(str(r) for r in sorted(reserved))
    safe = process_safe_preview_port(reserved=reserved)
    for p in sorted(reserved):
        ps = re.escape(str(p))
        bind_patterns = (
            rf"http\.server\s+{ps}\b",  # python3 -m http.server 8000
            rf"--port[=\s]+{ps}\b",  # --port 8000 / --port=8000
            rf"\bport[=\s]+{ps}\b",  # PORT=8000 / port 8000
            rf"-p[=\s]+{ps}\b",  # -p 8000 (flask/uvicorn/serve)
            rf"\blisten\s+{ps}\b",  # nginx/`listen 8000`
            # :8000 / host:8000 as a bind argument (serve -l 0.0.0.0:8000, ` :8000`)
            rf"(?:^|[\s=])(?:0\.0\.0\.0|127\.0\.0\.1|localhost|\[::1\]|::1)?:{ps}\b",
        )
        kill_patterns = (
            rf"\bfuser\b[^\n]*\b{ps}\b",  # fuser -k 8000/tcp
            rf"\blsof\b[^\n]*:{ps}\b",  # lsof -ti:8000 | xargs kill
        )
        for pat in bind_patterns:
            if re.search(pat, low):
                return (
                    f"refused: port {p} is reserved for the platform "
                    f"(reserved control/UI ports: {reserved_csv}) — serve your app on a "
                    f"non-reserved port such as {safe} instead"
                )
        for pat in kill_patterns:
            if re.search(pat, low):
                return (
                    f"refused: port {p} is reserved for the platform "
                    f"(reserved control/UI ports: {reserved_csv}); do not kill it — serve "
                    f"your own app on a non-reserved port such as {safe} instead"
                )
    return None


# A GENUINE `python -m http.server <port>` SERVE invocation, matched ONLY at the START of
# a CLEAN command string — the model's raw `shell_exec` command, BEFORE Disco wraps it into
# `tmux send-keys -l '<command>'`. Anchoring at the start is what makes this SAFE without a
# shell parser: the leading `python` cannot be inside a quote / heredoc body / `echo`
# argument (nothing precedes it), so — unlike a scan over an already-wrapped or arbitrary
# shell string — it can NEVER rewrite serve-shaped TEXT the model meant to run verbatim
# (`echo '; python3 -m http.server 8000'`, `python3 -c "print('http.server 8000')"`, a
# heredoc line, …). We also require the real `python[3] -m http.server <port>` shape (so a
# bare `http.server 8000` substring with no `-m http.server` never matches). A serve buried
# after a separator (`cd x && python -m http.server 8000`) or any quoting is deliberately
# NOT matched — it falls through to the actionable refuse-and-guide path. `cmd` is re-emitted
# verbatim — only the reserved `port` is rewritten.
_HTTP_SERVER_SERVE_RE = re.compile(
    r"^(?P<cmd>\s*(?:[\w./-]*/)?python[\d.]*\s+-m\s+http\.server\s+)(?P<port>\d+)"
)


def remap_reserved_preview_serve(
    command: str,
    reserved: frozenset[int] | None = None,
    safe_port: int | None = None,
) -> str | None:
    """Bug 16 — the RECOVERABLE counterpart to `reserved_port_command_violation`.

    `command` MUST be the model's CLEAN, single `shell_exec` command (the preview-serve
    handling point — `ShellSessionManager.exec`, BEFORE it is wrapped into `tmux
    send-keys -l '<command>'`). If it BEGINS with a genuine `python -m http.server
    <port>` SERVE on a RESERVED control/UI port, return it with that port rewritten to a
    process-safe preview port; otherwise return None (nothing to remap).

    On the process/local shared host a model that serves its deliverable on 8000/5173
    would be refused by the containment scan — and, with no preview established, the
    build STUCKs (`verify_no_progress`, the §17 no-fluke intermittency). Transparently
    remapping the SERVE to a safe port keeps the Bug-7 crash vector CLOSED (we never bind
    a reserved control port) while letting the build finish: the remapped server runs in
    the conversation's `disco-{cid8}-<session>` tmux session, so it is conversation-owned
    and `resolve_preview_port(host_shared=True)` targets it.

    Scope (Bug-16 review #2): the rewrite is ANCHORED at the start of the clean command,
    so it is SAFE without a shell parser — it can NEVER touch serve-shaped TEXT inside
    quotes / a heredoc / an `echo`/`printf`/`python -c` argument (those don't BEGIN with
    `python -m http.server`). A serve after a `&&`/`;`/`|` separator, or any non-leading
    position, is intentionally left for `reserved_port_command_violation` to
    refuse-with-guidance — safer than risking a rewrite of quoted text. Kills + other
    reserved binds are likewise not a leading `http.server` serve and are refused."""
    reserved = reserved_control_ports() if reserved is None else reserved
    safe_port = process_safe_preview_port(reserved=reserved) if safe_port is None else safe_port

    def _sub(m: re.Match[str]) -> str:
        port = int(m.group("port"))
        new_port = safe_port if port in reserved else port
        return f"{m.group('cmd')}{new_port}"

    new = _HTTP_SERVER_SERVE_RE.sub(_sub, command)
    return new if new != command else None


__all__ = [
    "DEFAULT_MANAGED_PREVIEW_PORT_COUNT",
    "DEFAULT_MANAGED_PREVIEW_PORT_START",
    "PREVIEW_PORTS",
    "PortOwnership",
    "active_managed_preview_ports",
    "backend_shares_host_network",
    "explicit_target_allowed",
    "is_managed_host_preview_port",
    "managed_host_preview_ports",
    "parse_port_ownership",
    "port_ownership_probe_command",
    "process_safe_preview_port",
    "remap_reserved_preview_serve",
    "reserved_control_ports",
    "reserved_port_command_violation",
    "resolve_preview_port",
    "target_url_port",
]
