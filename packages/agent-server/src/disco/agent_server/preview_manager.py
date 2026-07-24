"""PreviewManager — the platform owns ports/serving/health; the model never picks a port.

NORTH STAR (EPIC F): a build's preview is a *platform* concern, not a model one.
The model supplies INTENT ONLY — "serve this build-output dir", or "framework =
vite", or a bare start command — and the platform:

  * ALLOCATES the port (from a curated pool; see `_default_port_pool`), so the model
    can never fixate on / collide with a port. There is no port argument anywhere a
    model can reach (the `preview_start` tool has no port field; `PreviewManager.start`
    has no port parameter). The single place a port is chosen is `_allocate_port`.
  * STARTS + SUPERVISES the preview process inside the sandbox (restart-on-crash).
  * POLLS health (an in-sandbox liveness probe, backend-agnostic).
  * EXPOSES the platform-assigned URL (or degrades with a clear status when a backend
    — e.g. Podman in some setups — can't expose a routable URL, rather than failing).

This kills the port-fixation bug class (the Manus "auto-served preview" model) at the
root: the model declares WHAT to serve; the platform decides WHERE and keeps it alive.

The manager is a per-conversation collaborator. It binds to ONE `SandboxSession` (the
task's sandbox) and uses only its public surface: `sessions` (the shell-session
manager — start/view/kill), `expose_port` (URL), `fetch_inside` (the in-sandbox
liveness probe that works on every backend, sealed-network included), and
`backend_name` (to keep the port pool off a shared host's control ports). Tests fake
exactly that surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import shlex
import stat
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - the process backend is POSIX-only today
    _fcntl = None  # type: ignore[assignment]

_LOG = logging.getLogger(__name__)


class PreviewStatus(str, Enum):
    """Lifecycle state of one preview, as the platform sees it."""

    STARTING = "starting"  # process launched, not yet answering health
    RUNNING = "running"  # answering health on the platform port; URL exposed
    UNAVAILABLE = "unavailable"  # process up but the backend can't expose a URL (graceful degrade)
    RESTARTING = "restarting"  # crash detected; supervisor is re-issuing the command
    CRASHED = "crashed"  # crashed and the restart budget is exhausted
    STOPPED = "stopped"  # stopped on request


class PreviewReloadStrategy(str, Enum):
    """How the canonical client should observe workspace changes for this launch."""

    HMR = "hmr"
    RELOAD = "reload"


# How a bare framework name maps to a start command. `{port}` is filled with the
# PLATFORM-allocated port — never anything the model supplied. Anything not listed
# falls through to the static file server (the safe MVP default).
_FRAMEWORK_COMMANDS: dict[str, str] = {
    "static": "python3 -m http.server {port} -d {dir}",
    "http": "python3 -m http.server {port} -d {dir}",
    "vite": "npm run dev -- --port {port} --host 0.0.0.0 --strictPort",
    "next": "npm run dev -- -p {port}",
    "nextjs": "npm run dev -- -p {port}",
    "react": "PORT={port} npm start",
    "cra": "PORT={port} npm start",
    "astro": "npm run dev -- --port {port} --host 0.0.0.0",
    "svelte": "npm run dev -- --port {port} --host 0.0.0.0",
    "node": "PORT={port} npm start",
    "express": "PORT={port} npm start",
}

# Port arguments required when a caller supplies its own start script *and* declares
# one of the configured runtimes below.  The start script remains authoritative (a
# project may call it "develop", perform setup first, or use another package manager),
# while the runtime adapter remains authoritative for how that server binds the
# platform-owned port.  Relying on PORT alone is not sufficient: Vite/Astro/Svelte do
# not use it as their CLI listen port, so ``command="npm run dev", framework="vite"``
# otherwise starts a foreign server on 5173 while Preview owns (for example) 8000.
#
# These are lifecycle adapters, not generic-loop assumptions. Unknown/future runtimes
# keep the existing custom-command contract (PORT and/or an explicit {port}
# placeholder), and non-web targets never enter PreviewManager.
_FRAMEWORK_COMMAND_PORT_ARGS: dict[str, tuple[str, ...]] = {
    "vite": ("--port", "{port}", "--host", "0.0.0.0", "--strictPort"),
    "next": ("-p", "{port}"),
    "nextjs": ("-p", "{port}"),
    "astro": ("--port", "{port}", "--host", "0.0.0.0"),
    "svelte": ("--port", "{port}", "--host", "0.0.0.0"),
}

# These configured framework intents start development servers whose own runtime
# supplies hot-module updates. Static launches, generic node servers, and arbitrary
# custom commands have no such platform-guaranteed contract, so clients reload them
# when relevant workspace bytes change.
_HMR_FRAMEWORKS = frozenset({"vite", "next", "nextjs", "react", "cra", "astro", "svelte"})
_NODE_RUNTIME_FRAMEWORKS = frozenset(_FRAMEWORK_COMMANDS) - {"static", "http"}
_NODE_RUNTIME_COMMANDS = frozenset({"node", "npm", "npx", "pnpm", "yarn"})


def preview_requires_node_dependencies(*, command: str | None, framework: str | None) -> bool:
    """Whether a sealed raw intent needs its immutable Node dependency graph.

    This consumes only the already-validated PreviewStart intent. It does not
    guess from model names, filenames, or a scenario: configured Node framework
    adapters and explicit Node package executables share the same lifecycle.
    """

    normalized = (framework or "").strip().lower()
    if normalized in _NODE_RUNTIME_FRAMEWORKS:
        return True
    if not command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", "./")):
        key, _value = tokens[0].split("=", 1)
        if not key.replace("_", "a").isalnum():
            break
        tokens.pop(0)
    return bool(tokens and tokens[0].rsplit("/", 1)[-1] in _NODE_RUNTIME_COMMANDS)


# Port flags a model might bake into a raw `command`. These are handled on the SHLEX'D
# ARGV TOKENS (see `_classify_port_flag` / `_scrub_port_flag_tokens`), NOT by a regex on
# the raw string: a regex-on-raw-string scrub loses to quoting (`--port "8000"`,
# `--port='8000'`, `-p8000`, `PORT='8000'` all slip past `--port\s+\d+`), and after shlex
# those forms also dodge the positional-port rejection (the value is the flag's token,
# not a bare integer). Tokenizing first NORMALIZES every quoted/`=`-joined/joined form
# into the same tokens, so one uniform rule scrubs/refills them all — same lesson as the
# operator-ban: stop parsing raw shell, restrict + inspect the tokens.

# P1 #1 — port-bearing forms the flag-scrub above does NOT catch: a POSITIONAL
# `http.server <port>`, or a `host:port` / `:port` BIND argument (gunicorn `-b
# :8000`, `serve -l 0.0.0.0:8000`, …). These bind a MODEL-chosen port while the
# platform believes it owns the allocated one — so we REJECT a raw command that
# carries one (the model must drop the port, letting the platform inject PORT, or
# use the literal `{port}` placeholder the platform fills). Matched only AFTER the
# known flags are scrubbed. The `host:port` branch requires the colon/host to be
# preceded by start/space/quote/paren/`=`, so a URL fetch (`http://127.0.0.1:8000/`)
# — preceded by `/` — is NOT matched (reading a URL is not a bind).
#
# The trailing `\d{4,5}$` branch catches a POSITIONAL port as the final token
# (`uvicorn app:app --host 0.0.0.0 9000`); it requires 4-5 digits (≥1000) so a small
# trailing count like `--workers 16` / `--timeout 30` is NOT misread as a port. A model
# that genuinely needs a trailing 4-5-digit non-port arg can use `{port}` to place the
# real port and still pass the rest; the post-launch ownership check is the backstop.
_RAW_HARDCODED_PORT_RE = re.compile(
    r"""(?xi)
    (?:
        \bhttp\.server\s+\d{2,5}\b                       # python -m http.server 9999 (positional)
      | (?:^|[\s='"(])
        (?:0\.0\.0\.0|127\.0\.0\.1|localhost|\[::1\]|::1)?
        :\d{2,5}\b                                        # host:port / :port BIND
      | (?:^|\s)\d{4,5}\s*$                               # trailing positional port (uvicorn 9000)
    )
    """
)

# Shell control / chaining / background / substitution operators (plus newlines). A raw
# preview command MUST be a SINGLE FOREGROUND process. Anything that can chain (`;`, `&&`,
# `||`), background (`&`), pipe (`|`), or substitute (backtick, `$(...)`, `>(...)`, `<(...)`)
# lets a model SMUGGLE a second listener on a hardcoded port ALONGSIDE the platform's
# `{port}` server — e.g. `... http.server 4321 ... & python3 -m http.server {port} ...`,
# where the backgrounded first process quietly binds 4321 while the second passes the
# ownership probe. Regex-parsing arbitrary shell to spot that is a losing game, so we
# RESTRICT THE GRAMMAR at the choke point and refuse these operators outright. This
# operator-ban (with the post-launch ownership probe) is the real guarantee; the
# positional-port scan below is belt-and-braces over the single surviving command.
_SHELL_OPERATOR_RE = re.compile(r"&&|\|\||\$\(|>\(|<\(|[;&|`\n]")


def _looks_like_port(token: str) -> bool:
    """A bare positional PORT-LIKE token: a 2-5 digit integer in the plausible port range
    (1-65535). Used (after the flag-scrub) to reject a hardcoded port sitting as a plain
    positional arg — `http.server <port>`, `... <port>` — that the platform can't override.
    NOT matched: a `host:port` / `:port` form (has a colon — the regex backstop covers it),
    a single-digit count, or a flag value (the caller excludes any token following a flag,
    so `--workers 4` / `--timeout 30` are never misread as a port)."""
    return token.isdigit() and 2 <= len(token) <= 5 and 1 <= int(token) <= 65535


def _classify_port_flag(tok: str, nxt: str | None) -> tuple[str | None, int, str]:
    """Classify ONE argv token (post-`shlex.split`) as a port-specifying flag.

    Because shlex already stripped quotes and split on `=`, every form a model might use
    to bake in a port — `--port 8000`, `--port "8000"`, `--port='8000'`, `-p 8000`,
    `-p8000`, `-p=8000`, a leading `PORT='8000'` env-assignment — is NORMALIZED into one
    or two plain tokens here, so a single rule handles them uniformly (the quoting/`=`-join
    bypass class that beat the old raw-string regex is gone).

    Returns ``(value, span, kind)``:
      * ``value`` — the port the flag carries: the NEXT token for the space-separated
        forms (`--port X`, `-p X`), or the inline value otherwise; ``None`` when it is not
        a port flag, or a space-separated flag with no following token.
      * ``span`` — tokens the flag occupies (``2`` for `--port X` / `-p X`, else ``1``).
      * ``kind`` — ``""`` not a port flag · ``"flag"`` a CLI `--port`/`-p` flag a server
        reads off argv · ``"env"`` a `PORT=` env-assignment the platform already injects.
    """
    if tok in ("--port", "-p"):
        return (nxt, 2, "flag")  # value is the next token
    if tok.startswith("--port="):
        return (tok[len("--port=") :], 1, "flag")
    if tok.startswith("-p=") and len(tok) > 3:
        return (tok[3:], 1, "flag")
    if tok.startswith("-p") and len(tok) > 2:  # `-p8000` (directly joined)
        return (tok[2:], 1, "flag")
    if tok.startswith("PORT="):
        return (tok[len("PORT=") :], 1, "env")
    return (None, 1, "")


# tmux session-name prefix the ShellSessionManager writes (see shell_sessions._PREFIX).
# Used to confirm a listening port is owned by THIS preview's shell session.
_TMUX_PREFIX = "disco"

_PORT_BIND_TEST_SRC = """\
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# Match the bind semantics of the servers the platform actually launches
# (http.server, node, vite all set SO_REUSEADDR): sockets a torn-down sibling
# preview left in TIME_WAIT must not disqualify the port, while a live listener
# still refuses the bind. Without this, eligibility and server_status disagree
# for ~60s after every teardown ("FREE" vs "no free platform preview port").
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
"""


class PreviewCommandError(ValueError):
    """A raw `command` cannot be made platform-port-safe (it binds a hardcoded port via
    a form the platform can't override). The model must remove the port or use the
    literal `{port}` placeholder. Surfaced to the model as a recoverable tool failure."""


def _adapt_framework_command_port(tokens: list[str], *, framework: str | None) -> list[str]:
    """Apply configured runtime binding to a caller-selected start script."""

    adapter_args = _FRAMEWORK_COMMAND_PORT_ARGS.get((framework or "").strip().lower())
    if adapter_args is None or any("{port}" in token for token in tokens):
        return tokens
    adapted = list(tokens)
    # npm consumes script arguments unless they follow `--`. Other common script
    # runners and direct framework executables forward unknown arguments themselves.
    executable = adapted[0].rsplit("/", 1)[-1] if adapted else ""
    if executable == "npm" and "--" not in adapted:
        adapted.append("--")
    adapted.extend(adapter_args)
    return adapted


def preview_projection_digest(
    *,
    name: str,
    port: int,
    command: str,
    exec_dir: str | None,
    intent: dict[str, Any],
) -> str | None:
    """Canonical identity of the accepted launch, excluding ephemeral health state."""

    try:
        encoded = json.dumps(
            {
                "command": command,
                "exec_dir": exec_dir,
                "intent": intent,
                "name": name,
                "port": port,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class PreviewSession:
    """One supervised preview. `port` is platform-assigned; `intent` records what the
    model asked for (NOT a port). `url` is the platform-exposed URL (None ⇒ the backend
    can't route to it — see UNAVAILABLE)."""

    name: str
    port: int  # PLATFORM-allocated — see PreviewManager._allocate_port
    command: str  # fully-resolved command actually run (port already baked in)
    exec_dir: str | None
    intent: dict[str, Any]
    projection_id: str = field(default_factory=lambda: f"pv_{uuid.uuid4().hex}")
    sandbox_instance_id: str | None = None
    sandbox_generation: int | None = None
    status: PreviewStatus = PreviewStatus.STARTING
    url: str | None = None
    restart_count: int = 0
    detail: str = ""
    # A content/dependency refresh may fail while the prior healthy frame remains
    # useful. Keep that failure distinct from process health so status polling
    # cannot erase it merely because the old process still answers HTTP.
    update_error: str | None = None
    _supervise: bool = field(default=True, repr=False)
    # Automatic restart is armed only after this launch generation has served
    # successfully. An initial startup failure, or a failed explicit repair, must not
    # be multiplied into hidden background launches while the model is still fixing it.
    _auto_restart_armed: bool = field(default=False, repr=False)
    # An explicit recovery starts one new attempt without re-arming background
    # retries. Only proof that the attempt is healthy resets the automatic budget.
    _reset_budget_on_healthy: bool = field(default=False, repr=False)

    @property
    def reload_strategy(self) -> PreviewReloadStrategy:
        framework = self.intent.get("framework")
        if self.intent.get("launch_kind") == "framework" and isinstance(framework, str):
            if framework.strip().lower() in _HMR_FRAMEWORKS:
                return PreviewReloadStrategy.HMR
        return PreviewReloadStrategy.RELOAD

    def to_dict(self) -> dict[str, Any]:
        launch_kind = self.intent.get("launch_kind")
        intent_digest = preview_projection_digest(
            name=self.name,
            port=self.port,
            command=self.command,
            exec_dir=self.exec_dir,
            intent=self.intent,
        )
        return {
            "name": self.name,
            "port": self.port,
            "status": self.status.value,
            "url": self.url,
            "command": self.command,
            "exec_dir": self.exec_dir,
            "intent": self.intent,
            "launch_kind": launch_kind,
            # Public preview generation: stable for one supervised OS process and
            # rotated at every automatic or explicit re-exec boundary.
            "generation": self.projection_id,
            "reload_strategy": self.reload_strategy.value,
            "projection_id": self.projection_id,
            "intent_digest": intent_digest,
            "sandbox_instance_id": self.sandbox_instance_id,
            "sandbox_generation": self.sandbox_generation,
            "restart_count": self.restart_count,
            "detail": self.detail,
            "update_error": self.update_error,
        }


class NoPreviewPortAvailableError(RuntimeError):
    """The platform's preview port pool is exhausted — every curated port is in use."""


@dataclass
class _PreviewPortLease:
    """Kernel-backed reservation for one shared-host preview port."""

    fd: int
    path: str

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)


class PreviewManager:
    """Platform-owned preview lifecycle for ONE conversation's sandbox.

    Idempotent start, restart-on-crash supervision, stop, list, health. The model
    never sees or supplies a port — `start()` takes intent only.
    """

    # Max times the supervisor re-issues a crashed preview's command before it gives
    # up and parks the session in CRASHED (so a hard-broken command can't spin forever).
    MAX_RESTARTS = 3

    def __init__(
        self,
        sandbox: Any,
        *,
        port_pool: list[int] | None = None,
        health_attempts: int = 10,
        health_interval_s: float = 0.3,
        supervise_interval_s: float = 4.0,
    ) -> None:
        self._sandbox = sandbox
        self._pool: list[int] = (
            list(port_pool) if port_pool is not None else _default_port_pool(sandbox)
        )
        self._health_attempts = max(1, health_attempts)
        self._health_interval_s = health_interval_s
        self._supervise_interval_s = supervise_interval_s
        self._sessions: dict[str, PreviewSession] = {}
        # Explicit successful preview_start selections, oldest to newest. The
        # canonical UI route follows the newest still-servable selection; supervisor
        # restarts never reorder it, while an idempotent explicit start does.
        self._selection_order: list[str] = []
        self._lock = asyncio.Lock()
        self._supervisor: asyncio.Task[None] | None = None
        self._closed = False
        self._auto_preview_coordinated = False  # P1 #2: legacy auto-preview stood down once
        # Process sandboxes share one host network across conversations and Agent
        # server processes. Socket probing alone has a check/use gap, so keep a
        # kernel lease for every allocated port through the preview lifecycle.
        self._port_leases: dict[int, _PreviewPortLease] = {}
        # Host-only bindings from an immutable final workspace contract to the
        # freshly revalidated runtime session serving those exact bytes.
        self._sealed_contracts: dict[str, tuple[str, dict[str, Any]]] = {}

    # ---- platform-owned port allocation ------------------------------------

    async def _allocate_port(self, *, reclaim_name: str | None = None) -> int:
        """THE single place a preview port is chosen. The model has no input here — the
        port comes from the curated platform pool, skipping (a) any port already held by
        one of our previews, (b) any port the SANDBOX is already tracking as a service —
        the legacy static auto-preview or an agent-launched dev server (P1 #2: allocating
        onto one would mean the legacy server's response falsely validates ours), and
        (c) any port with a LIVE listener right now (a real owner we don't track), as
        proven from INSIDE the sandbox by socket ownership / bindability rather than
        HTTP health alone. This is the ownership point that kills port-fixation AND
        cross-server contamination."""
        taken = {s.port for s in self._sessions.values() if s.status is not PreviewStatus.STOPPED}
        taken |= self._sandbox_tracked_ports()
        checked_count = 0
        checked_sample: list[int] = []
        for port in self._pool:
            if port in taken:
                continue
            checked_count += 1
            if len(checked_sample) < 20:
                checked_sample.append(port)
            lease: _PreviewPortLease | None = None
            if self._shares_host_network():
                lease = self._try_host_port_lease(port)
                if lease is None:
                    continue
            # A real listener already owns this curated port (not one of ours) -> skip it,
            # or its response would falsely validate a process that EADDRINUSE'd. HTTP
            # health is only the last fallback; the primary checks are in-sandbox socket
            # ownership and bindability.
            try:
                if not await self._port_available_for_allocation(port, reclaim_name=reclaim_name):
                    continue
                if lease is not None:
                    self._port_leases[port] = lease
                    lease = None
                return port
            finally:
                if lease is not None:
                    lease.close()
        raise NoPreviewPortAvailableError(
            "no free platform preview port found after checking "
            f"{checked_count} candidate(s)"
            + (f" (first candidates: {checked_sample})" if checked_sample else "")
        )

    def _shares_host_network(self) -> bool:
        """Whether this manager competes for one host-wide TCP namespace."""

        explicit = getattr(self._sandbox, "shares_host_network", None)
        if isinstance(explicit, bool):
            return explicit
        return (getattr(self._sandbox, "backend_name", "") or "") == "process"

    @staticmethod
    def _try_host_port_lease(port: int) -> _PreviewPortLease | None:
        """Acquire ``port`` without waiting; a busy lease selects another port."""

        if _fcntl is None:
            raise NoPreviewPortAvailableError(
                "shared-host preview allocation requires POSIX file locking"
            )
        from .workspace_process_fence import workspace_process_lock_dir

        path = workspace_process_lock_dir() / f"preview-port-{int(port)}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(path, flags, 0o600)
            opened = os.fstat(fd)
            named = path.lstat()
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                named.st_dev,
                named.st_ino,
            ):
                raise OSError("preview port lease changed identity or is not a regular file")
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            raise NoPreviewPortAvailableError(
                f"shared-host preview port {port} could not be reserved: {exc}"
            ) from exc
        return _PreviewPortLease(fd=fd, path=str(path))

    def _release_port_lease(self, port: int) -> None:
        lease = self._port_leases.pop(port, None)
        if lease is not None:
            lease.close()

    async def _port_available_for_allocation(
        self, port: int, *, reclaim_name: str | None = None
    ) -> bool:
        """Return True only when `port` is genuinely free in the sandbox.

        `_probe_health` can miss non-HTTP listeners or listeners that are wedged before
        serving a response. Allocation therefore checks the socket owner first, optionally
        reclaims a stale preview that belongs to this conversation, then bind-tests the
        port from inside the sandbox when that seam is available.
        """
        owner = await self._port_owner(port)
        if owner is not None and getattr(owner, "pid", None) is not None:
            if await self._reclaim_stale_preview_port(port, owner, reclaim_name=reclaim_name):
                owner = await self._port_owner(port)
                if owner is not None and getattr(owner, "pid", None) is not None:
                    return False
            else:
                return False

        bindable = await self._bind_test_port(port)
        if bindable is not None:
            return bindable

        if owner is not None:
            return getattr(owner, "pid", None) is None

        # Last fallback for old/fake sandboxes without exec-shell attribution. Keep the
        # previous behavior here, but only after stronger in-sandbox occupancy checks fail.
        return not await self._probe_health(port)

    async def _bind_test_port(self, port: int) -> bool | None:
        """Try to bind `port` inside the sandbox. True means bindable/free, False means
        occupied, None means this sandbox cannot run the bind probe."""
        exec_shell = getattr(self._sandbox, "exec_shell", None)
        if exec_shell is None:
            return None
        try:
            res = await exec_shell(
                f"python3 -c {shlex.quote(_PORT_BIND_TEST_SRC)} {int(port)}",
                timeout_s=5,
            )
        except Exception:  # noqa: BLE001 — fall back to owner/health probes
            return None
        return getattr(res, "exit_code", 1) == 0

    async def _reclaim_stale_preview_port(
        self, port: int, owner: Any, *, reclaim_name: str | None = None
    ) -> bool:
        """If `port` is held by a stale preview from this same conversation, stop that
        preview and let allocation reuse the port. Foreign, unattributed, and actively
        tracked previews are never reclaimed here."""
        owner_session = getattr(owner, "session", None)
        stale_name = self._stale_preview_name_for_owner(
            port, str(owner_session) if owner_session else None, reclaim_name=reclaim_name
        )
        if stale_name is None:
            return False

        if stale_name in self._sessions:
            await self._stop_locked(stale_name)
        else:
            try:
                await self._sandbox.sessions.kill_foreground(stale_name)
            except Exception:  # noqa: BLE001 — failed reclaim means "not available"
                return False
        return True

    def _stale_preview_name_for_owner(
        self, port: int, owner_session: str | None, *, reclaim_name: str | None = None
    ) -> str | None:
        if not owner_session:
            return None
        ns = getattr(getattr(self._sandbox, "sessions", None), "namespace", "") or ""
        prefix = f"{_TMUX_PREFIX}-{ns}"
        if not owner_session.startswith(prefix):
            return None
        owner_name = owner_session[len(prefix) :]
        known = self._sessions.get(owner_name)
        if known is not None and known.port == port and known.status is PreviewStatus.STOPPED:
            return owner_name
        if reclaim_name is not None and owner_name == reclaim_name:
            return owner_name
        return None

    def _sandbox_tracked_ports(self) -> set[int]:
        """Curated ports the SANDBOX itself is already tracking (the static auto-preview
        on 8000, any `ensure_service` dev server). Consulted so allocation never lands on
        a port a legacy/coexisting server owns. Best-effort: a fake/old sandbox without
        the accessor contributes nothing (the live-listener probe still guards us)."""
        accessor = getattr(self._sandbox, "tracked_ports", None)
        if accessor is None:
            return set()
        try:
            return set(accessor())
        except Exception:  # noqa: BLE001 — never let a tracking read break allocation
            return set()

    # ---- intent → resolved command -----------------------------------------

    def _scrub_port_flag_tokens(self, tokens: list[str]) -> list[str]:
        """Walk the shlex'd argv and DROP every port-specifying flag that carries a
        CONCRETE model-chosen port (in ANY normalized form — `--port 8000`, `--port "8000"`,
        `--port='8000'`, `-p8000`, `-p 8000`, `PORT='8000'`), so the platform port can never
        be overridden through the command. The platform owns the port and injects it via the
        `PORT=<its port>` prefix, so a dropped flag's server still gets the right port.

        A flag whose value is the literal `{port}` placeholder is the SANCTIONED way to
        position the platform port: keep the `--port`/`-p` flag untouched (it is filled with
        the allocated port later). A redundant `PORT={port}` is dropped (the platform already
        injects PORT=). Everything else passes through unchanged.

        This replaces the old regex-on-raw-string scrub, which lost to quoting/`=`-joining —
        after shlex those forms are normalized tokens we handle uniformly here."""
        out: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            value, span, kind = _classify_port_flag(tok, nxt)
            if kind and value is not None:
                if value.isdigit():
                    # Concrete port → drop the flag (+ its value token for `--port X`/`-p X`).
                    i += span
                    continue
                if value == "{port}":
                    if kind == "env":
                        i += span  # PORT={port}: redundant with the platform PORT= prefix.
                        continue
                    # `--port {port}` / `--port={port}` / `-p{port}`: keep — filled later.
                    out.append(tok)
                    if span == 2 and nxt is not None:
                        out.append(nxt)
                    i += span
                    continue
            out.append(tok)
            i += 1
        return out

    def _reject_positional_tokens(self, tokens: list[str]) -> None:
        """Reject any BARE positional port-like token in the (operator-free, port-flag-scrubbed)
        argv — a hardcoded port the platform can't override sitting as a plain positional arg
        (`http.server <port>`, or a port positioned anywhere). HEURISTIC: a numeric token
        immediately following a flag (a token starting with `-`) is that flag's VALUE, not a
        port, so legit numeric args like `--workers 4` / `--timeout 30` are not false-rejected.
        A `{port}` placeholder token is not a digit, so it is never mistaken for a hardcoded
        port."""
        prev_was_flag = False
        for tok in tokens:
            if _looks_like_port(tok) and not prev_was_flag:
                raise PreviewCommandError(
                    f"preview command carries a hardcoded port ({tok!r}) as a positional "
                    "argument — the platform owns the port, so it can't be supplied here. "
                    "Remove it (the platform injects PORT=<its port>), or put the literal "
                    "placeholder '{port}' where the port goes (e.g. 'python3 -m "
                    "http.server {port} -d dist')."
                )
            prev_was_flag = tok.startswith("-")

    def _resolve_command(
        self,
        port: int,
        *,
        serve_dir: str | None,
        command: str | None,
        framework: str | None,
    ) -> str:
        """Turn model INTENT into the actual command, with the PLATFORM port baked in.

        An explicit `command` chooses the project script; a known `framework` still
        adapts it to the platform port. Otherwise use a known framework template or
        statically serve `serve_dir`.

        A raw command never gets to pick the port: any `--port/-p/PORT=` the model
        embedded (quoted, `=`-joined, or bare) is stripped on the SHLEX'D TOKENS and the
        platform port is injected via `PORT=`. A raw command that ALSO binds a port through
        a form the scrub can't override (a positional `http.server <port>`, a `host:port`
        bind) is REJECTED (`PreviewCommandError`) — unless it uses the literal `{port}`
        placeholder, which the platform fills with ITS port (the explicit, safe way to put
        the platform port at a positional slot).
        """
        serve = serve_dir or self._sandbox_workspace() or "."
        if command:
            # GRAMMAR RESTRICTION (the real guarantee): a raw preview command must be a
            # SINGLE FOREGROUND process. Reject shell control / chaining / backgrounding /
            # piping / substitution operators (and newlines) at the root — they're how a
            # model smuggles a second listener on a hardcoded port past the ownership probe.
            if _SHELL_OPERATOR_RE.search(command):
                raise PreviewCommandError(
                    "preview command must be a SINGLE FOREGROUND process — shell control, "
                    "chaining, backgrounding, piping, or substitution operators "
                    "(; & | && || ` $(...) >(...) <(...) newlines) are not allowed (they can "
                    "smuggle a second server on a hardcoded port the platform can't see). "
                    "Use one command and put the serve port as the literal placeholder "
                    "'{port}' (e.g. 'python3 -m http.server {port} -d dist')."
                )
            # Tokenize ONCE (the operator-ban above guarantees a single command). All
            # port-flag handling happens on these tokens, not on the raw string, so the
            # quoted/`=`-joined forms that beat a raw-string regex are normalized first.
            try:
                tokens = shlex.split(command)
            except ValueError as exc:
                raise PreviewCommandError(
                    f"preview command could not be parsed as a single shell command ({exc}). "
                    "Provide a simple foreground command with the serve port as '{port}'."
                ) from exc
            # Drop every concrete port flag (`--port/-p/PORT=`, any quoting/join); keep a
            # `{port}` placeholder flag for the platform to fill.
            cleaned = self._scrub_port_flag_tokens(tokens)
            # Reject ANY other hardcoded/positional port the flag-scrub can't account for —
            # even on the `{port}`-placeholder path. The placeholder is the ONE sanctioned
            # way to put the platform port at a positional slot, but the REST of the command
            # must still be port-clean: a second, hardcoded port alongside `{port}` (e.g.
            # 'http.server {port} 9999') would bind a model-chosen port the platform doesn't
            # own (P1 #1). The `{port}` token is not a digit, so it never trips this check.
            self._reject_positional_tokens(cleaned)
            # Backstop for `host:port` / `:port` BIND forms (gunicorn `-b :8000`, `serve -l
            # 0.0.0.0:8000`) that carry a colon and so are not a bare integer token above.
            # Run it on a residual rebuilt from the cleaned tokens with `{port}` blanked, so
            # only a CONCRETE bind (`:8000`) trips it — never the sanctioned `:{port}`.
            residual = " ".join(t.replace("{port}", " ") for t in cleaned)
            bound = _RAW_HARDCODED_PORT_RE.search(residual)
            if bound is not None:
                raise PreviewCommandError(
                    "preview command binds a hardcoded port "
                    f"({bound.group(0).strip()!r}) — the platform owns the port, so it "
                    "can't be supplied here. Remove the port (the platform injects PORT="
                    "<its port>), or put the literal placeholder '{port}' where the port "
                    "goes (e.g. 'python3 -m http.server {port} -d dist')."
                )
            cleaned = _adapt_framework_command_port(cleaned, framework=framework)
            # Fill sanctioned placeholders before safely rejoining. PORT covers generic
            # custom runtimes; configured adapters add their required CLI binding.
            filled = [t.replace("{port}", str(port)) for t in cleaned]
            return f"PORT={port} {shlex.join(filled)}"
        if framework:
            template = _FRAMEWORK_COMMANDS.get(framework.strip().lower())
            if template is not None:
                return template.format(port=port, dir=shlex.quote(serve))
        # Default + unknown frameworks: serve the build output statically.
        return f"python3 -m http.server {port} -d {shlex.quote(serve)}"

    def _sandbox_workspace(self) -> str | None:
        try:
            return getattr(self._sandbox, "workspace_path", None)
        except Exception:  # noqa: BLE001 — best-effort default
            return None

    async def _launch_workspace(self, explicit_cwd: str | None) -> str | None:
        """Resolve the command cwd independently from ``serve_dir``.

        Container sandboxes deliberately expose no host ``workspace_path`` even though
        their in-guest workspace is ``/workspace``.  Falling back to a relative
        ``serve_dir`` made that directory both the shell cwd and ``http.server -d``
        argument (``release/release``).  Ask the sandbox for its actual cwd instead;
        leaving it unset is safer than ever reusing the content path as a cwd.
        """

        if explicit_cwd:
            return explicit_cwd
        workspace = self._sandbox_workspace()
        if workspace:
            return workspace
        try:
            result = await self._sandbox.exec_shell("pwd", timeout_s=5)
            if getattr(result, "exit_code", 1) == 0:
                discovered = str(getattr(result, "stdout", "")).strip()
                if discovered:
                    return discovered
        except Exception:  # noqa: BLE001 — shell manager can use its own safe default
            _LOG.debug("preview workspace discovery failed", exc_info=True)
        return None

    @staticmethod
    def _requires_successful_root(session: PreviewSession) -> bool:
        """Static directory intent is ready only when its selected root is successful."""

        return session.intent.get("launch_kind") == "static"

    @staticmethod
    def _launch_kind(*, command: str | None, framework: str | None) -> str:
        """Mirror command resolution so root-health policy matches the actual launcher."""

        normalized = (framework or "").strip().lower()
        if (
            normalized
            and normalized in _FRAMEWORK_COMMANDS
            and normalized
            not in {
                "static",
                "http",
            }
        ):
            return "framework"
        if command:
            return "custom"
        return "static"

    def _default_name(self, serve_dir: str | None, framework: str | None) -> str:
        if framework:
            return f"preview-{framework.strip().lower()}"
        if serve_dir and serve_dir not in (".", ""):
            leaf = serve_dir.rstrip("/").rsplit("/", 1)[-1]
            if leaf:
                return f"preview-{leaf}"
        return "preview"

    # ---- legacy auto-preview coordination (P1 #2) ---------------------------

    async def _coordinate_auto_preview(self) -> None:
        """Stand the sandbox's legacy fire-and-forget static auto-preview DOWN the first
        time the platform manager starts a preview for this sandbox. After this, the
        manager is the SINGLE authority for previews — the auto-preview can no longer
        squat a curated port or answer health on a port the manager allocates (the P1 #2
        false-validate). Best-effort + idempotent: a fake/old sandbox without the hook is
        simply left as-is (allocation's tracked-port + live-listener guards still hold)."""
        if self._auto_preview_coordinated:
            return
        self._auto_preview_coordinated = True
        disable = getattr(self._sandbox, "disable_auto_preview", None)
        if disable is None:
            return
        try:
            await disable()
        except Exception:  # noqa: BLE001 — coordination is best-effort, never fatal
            _LOG.debug("auto-preview coordination failed", exc_info=True)

    # ---- post-launch port-ownership verification (P1 #1 / P1 #2) ------------

    async def _confirm_and_mark_running(self, session: PreviewSession) -> bool:
        """Health answered on the platform port — but confirm THIS preview's process
        actually OWNS that port before declaring RUNNING. Guards two ways something ELSE
        can answer: a raw command bound a different port (P1 #1) and our process never
        bound the allocated one, OR the legacy auto-preview / another server answered
        while our process EADDRINUSE'd (P1 #2). Only POSITIVE evidence of a FOREIGN owner
        blocks RUNNING (→ CRASHED); when ownership can't be determined (backend exposes no
        probe) we trust health, so this never regresses a backend that can't attribute
        ports. Returns True if marked running, False if a foreign owner was detected."""
        owned = await self._verify_owns_port(session)
        if owned is False:
            session.status = PreviewStatus.CRASHED
            session.url = None
            session.detail = (
                f"port {session.port} is answering but is owned by a DIFFERENT process — "
                "this preview did not bind it (its server likely failed / the port was "
                "already in use). Not marked running."
            )
            return False
        self._mark_running(session)
        return True

    async def _verify_owns_port(self, session: PreviewSession) -> bool | None:
        """True ⇒ the allocated port's listener is THIS preview's shell session; False ⇒
        a different process owns it; None ⇒ undeterminable (no probe / no attribution) —
        the caller then trusts health rather than blocking."""
        owner = await self._port_owner(session.port)
        if owner is None:
            return None
        if getattr(owner, "pid", None) is None:
            return None  # nobody attributable listening (health said yes) → inconclusive
        owner_session = getattr(owner, "session", None)
        if not owner_session:
            # Isolated network health is sufficient when a backend cannot map the
            # listener to a shell session. On a shared host, an unattributed PID
            # may be any sibling or owner process and cannot certify this build.
            return False if self._shares_host_network() else None
        return self._owner_is_this_session(str(owner_session), session.name)

    async def _port_owner(self, port: int) -> Any | None:
        """Who owns `port` inside the sandbox (object with `.pid` + `.session`), or None
        if it can't be probed. Prefers a `port_owner` method on the sandbox (tests + any
        future fast path); otherwise uses the tools' in-sandbox /proc+tmux probe. Never
        raises — a probe failure is an inconclusive (None), not a blocked preview."""
        try:
            probe = getattr(self._sandbox, "port_owner", None)
            if probe is not None:
                return await probe(port)
            from disco.tools.sandbox.port_owner import port_owner

            return await port_owner(self._sandbox, port)
        except Exception:  # noqa: BLE001 — ownership probe must never raise into start()
            return None

    def _owner_is_this_session(self, owner_session: str, name: str) -> bool:
        """Does the tmux session that owns the port belong to THIS preview? The shell
        manager names sessions `disco-{namespace}{name}` (see shell_sessions._full_name).
        Require that FULL, EXACT session identity — a loose `-{name}` suffix (or bare
        `name`) match would accept a DIFFERENT conversation's `disco-{othercid}{name}` for
        a common name (`preview`, `app`), false-marking RUNNING against a foreign listener
        in the allocation/launch race window on process/local backends (P1 #2)."""
        ns = getattr(getattr(self._sandbox, "sessions", None), "namespace", "") or ""
        return owner_session == f"{_TMUX_PREFIX}-{ns}{name}"

    # ---- start (idempotent) -------------------------------------------------

    async def start(
        self,
        *,
        serve_dir: str | None = None,
        command: str | None = None,
        framework: str | None = None,
        cwd: str | None = None,
        name: str | None = None,
        supervise: bool = True,
    ) -> PreviewSession:
        """Start (or idempotently return) a preview from model INTENT.

        NOTE the signature: there is deliberately NO `port` parameter. The platform
        allocates it. Re-calling with the same `name` (or default-name-for-intent)
        returns the existing live session after refreshing its health — so a model
        that calls `preview_start` twice doesn't spawn duplicates or burn a 2nd port.
        """
        if self._closed:
            raise RuntimeError("PreviewManager is closed")
        # P1 #2: once the platform manager owns previews for this sandbox, stand the
        # legacy fire-and-forget auto-preview DOWN, so the two can't both claim a curated
        # port (and a stale auto-preview can't answer health on a manager-allocated port).
        await self._coordinate_auto_preview()
        key = name or self._default_name(serve_dir, framework)
        # Resolve (and validate) the command BEFORE taking the lock / allocating, so a
        # rejected raw command (PreviewCommandError) leaks no port or half-built session.
        # `serve` here is dir-only intent; the concrete port is injected after allocation.
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.status is not PreviewStatus.STOPPED:
                # Idempotent: refresh health/URL and return the same session + port.
                await self._refresh(existing)
                if existing.status is PreviewStatus.CRASHED:
                    # A crashed preview is an explicit repair boundary, not ordinary
                    # idempotent lookup. Re-resolve the CURRENT intent so a corrected
                    # command/cwd can replace the failed one without stop/start churn.
                    resolved = self._resolve_command(
                        existing.port,
                        serve_dir=serve_dir,
                        command=command,
                        framework=framework,
                    )
                    exec_dir = await self._launch_workspace(cwd)
                    intent = {
                        "serve_dir": serve_dir,
                        "command": command,
                        "framework": framework,
                        "cwd": cwd,
                        "launch_kind": self._launch_kind(command=command, framework=framework),
                    }
                    await self._recover_explicit(
                        existing,
                        command=resolved,
                        exec_dir=exec_dir,
                        intent=intent,
                    )
                self._record_explicit_selection(existing)
                return existing

            port = await self._allocate_port(reclaim_name=key)
            registered = False
            try:
                resolved = self._resolve_command(
                    port, serve_dir=serve_dir, command=command, framework=framework
                )
                # P1 #3: run the server FROM the workspace (not serve_dir) so the static
                # `-d <serve_dir>` resolves to workspace/<serve_dir> — running from serve_dir
                # with `-d <serve_dir>` served `<serve_dir>/<serve_dir>` (404 / wrong tree).
                exec_dir = await self._launch_workspace(cwd)
                session = PreviewSession(
                    name=key,
                    port=port,
                    command=resolved,
                    exec_dir=exec_dir,
                    intent={
                        "serve_dir": serve_dir,
                        "command": command,
                        "framework": framework,
                        "cwd": cwd,
                        "launch_kind": self._launch_kind(command=command, framework=framework),
                    },
                    _supervise=supervise,
                )
                self._sessions[key] = session
                registered = True
                await self._launch(session)
                self._record_explicit_selection(session)
            except BaseException:
                if not registered:
                    self._release_port_lease(port)
                raise

        if supervise:
            self._ensure_supervisor()
        return session

    @staticmethod
    def _is_servable(session: PreviewSession) -> bool:
        return session.status in {PreviewStatus.RUNNING, PreviewStatus.UNAVAILABLE}

    def _record_explicit_selection(self, session: PreviewSession) -> None:
        """Record an accepted explicit start even if health proof arrives later."""
        if session.status in {PreviewStatus.CRASHED, PreviewStatus.STOPPED}:
            return
        if session.name in self._selection_order:
            self._selection_order.remove(session.name)
        self._selection_order.append(session.name)

    def canonical_port(self) -> int | None:
        """Port selected by the newest successful explicit preview_start.

        A manager with no health-proven preview returns ``None``. This is distinct
        from a sandbox with no manager, where the legacy static preview remains 8000.
        """
        selected = self.canonical_session()
        return selected.port if selected is not None else None

    def canonical_session(self) -> PreviewSession | None:
        """Return the selected session only when that exact selection is servable.

        Stopped selections are retired and may reveal the prior selection. A
        preparing or crashed newest selection remains authoritative lifecycle
        state, however: routing must not silently substitute an older healthy app.
        The browser can retain its already-loaded frame while recovery proceeds.
        """

        for name in reversed(self._selection_order):
            session = self._sessions.get(name)
            if session is None or session.status is PreviewStatus.STOPPED:
                continue
            return session if self._is_servable(session) else None
        return None

    def canonical_lifecycle_session(self) -> PreviewSession | None:
        """Canonical preview state, including an honest not-yet-servable verdict.

        Routing continues to use :meth:`canonical_session`, which refuses to replace a
        failed current selection with superseded bytes. When no selection is servable,
        status clients still receive the exact managed launch that is preparing,
        crashed, or otherwise unavailable instead of a generic "no server" guess.
        """

        servable = self.canonical_session()
        if servable is not None:
            return servable
        for name in reversed(self._selection_order):
            session = self._sessions.get(name)
            if session is not None and session.status is not PreviewStatus.STOPPED:
                return session
        # A launch that failed its initial health check is intentionally not a
        # successful selection, but it remains the authoritative lifecycle verdict.
        for session in reversed(self._sessions.values()):
            if session.status is not PreviewStatus.STOPPED:
                return session
        return None

    async def _launch(self, session: PreviewSession) -> None:
        """Issue the (re)start command in the sandbox + poll health to a verdict."""
        session.status = PreviewStatus.STARTING
        command = session.command
        if session.exec_dir:
            # The manager owns this tmux session, but the SHELL inside it is
            # stateful: a relaunch re-execs into the surviving shell, which keeps
            # the PRIOR generation's cwd — even after the model legitimately
            # deleted that directory (counted seed 440025: node's uv_cwd ENOENT
            # crashed every relaunch from the dead cwd, including corrected
            # commands). The session's exec_dir is authoritative per generation,
            # so bind it at every (re)launch; cd to an absolute path succeeds
            # regardless of the shell's current (possibly deleted) directory.
            command = f"cd {shlex.quote(session.exec_dir)} && {command}"
        try:
            await self._sandbox.sessions.exec(session.name, command, exec_dir=session.exec_dir)
        except Exception as exc:  # noqa: BLE001 — surface as a status, never raise into a tool
            session.status = PreviewStatus.CRASHED
            session.detail = f"failed to launch: {exc}"
            return
        await self._poll_until_healthy(session)

    async def _poll_until_healthy(self, session: PreviewSession) -> None:
        for _ in range(self._health_attempts):
            if await self._probe_health(
                session.port,
                require_success=self._requires_successful_root(session),
            ):
                # Health alone is not enough — confirm OUR process owns the port before
                # RUNNING (a foreign server answering ⇒ CRASHED, not a false success).
                if not await self._confirm_and_mark_running(session):
                    return
                return
            await asyncio.sleep(self._health_interval_s)
        # Never answered in time. Distinguish "process is up but no URL" from a crash:
        # if the shell session is still running, it's just slow/headless → STARTING.
        if await self._session_alive(session.name) and not self._requires_successful_root(session):
            session.status = PreviewStatus.STARTING
            session.detail = "process running; not yet answering health checks"
        else:
            session.status = PreviewStatus.CRASHED
            session.detail = (
                "static preview root did not return a successful HTTP response"
                if self._requires_successful_root(session)
                else "process exited before serving"
            )

    def _mark_running(self, session: PreviewSession) -> None:
        """Healthy on the platform port — expose the URL, or degrade gracefully."""
        sandbox_id = getattr(self._sandbox, "id", None)
        sandbox_generation = getattr(self._sandbox, "generation", None)
        session.sandbox_instance_id = sandbox_id if isinstance(sandbox_id, str) else None
        session.sandbox_generation = sandbox_generation if type(sandbox_generation) is int else None
        session._auto_restart_armed = True
        if session._reset_budget_on_healthy:
            session.restart_count = 0
            session._reset_budget_on_healthy = False
        url = self._expose(session.port)
        if url is not None:
            session.url = url
            session.status = PreviewStatus.RUNNING
            session.detail = ""
        else:
            # SEAM-EVAL graceful degrade: the process is healthy but the backend
            # (e.g. Podman in some setups) can't hand back a routable URL. Say so
            # plainly — never fail the build, never fabricate a URL.
            session.url = None
            session.status = PreviewStatus.UNAVAILABLE
            session.detail = (
                "preview is running inside the sandbox, but this backend can't expose a "
                "routable URL here (it's reachable at deployment)."
            )

    def _expose(self, port: int) -> str | None:
        try:
            return self._sandbox.expose_port(port)
        except Exception:  # noqa: BLE001 — a URL probe must never raise
            return None

    async def _probe_health(self, port: int, *, require_success: bool = False) -> bool:
        """Backend-agnostic liveness: curl the server from INSIDE the sandbox (the one
        place it's reachable on every backend, sealed-network included). Any HTTP
        response proves a port is occupied; static-directory readiness additionally
        requires a successful root so a 404 cannot be advertised as health-verified."""
        try:
            res = await self._sandbox.fetch_inside(port, "/", timeout_s=5)
        except Exception:  # noqa: BLE001 — a probe must never raise into supervision
            return False
        if res is None:
            return False
        if not require_success:
            return True
        try:
            status = int(res[0])
        except (IndexError, TypeError, ValueError):
            return False
        return 200 <= status < 400

    async def _session_alive(self, name: str) -> bool:
        try:
            view = await self._sandbox.sessions.view(name)
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(view, "running", False))

    # ---- supervision: restart-on-crash -------------------------------------

    def _ensure_supervisor(self) -> None:
        if self._supervisor is None or self._supervisor.done():
            try:
                self._supervisor = asyncio.create_task(self._supervise_loop())
            except RuntimeError:
                # No running loop (e.g. constructed outside async context) — supervision
                # is opportunistic; callers can drive _supervise_once() directly.
                self._supervisor = None

    async def _supervise_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._supervise_interval_s)
                await self._supervise_once()
        except asyncio.CancelledError:  # pragma: no cover - teardown path
            raise
        except Exception:  # noqa: BLE001 — supervision must never die silently-then-loudly
            _LOG.warning("preview supervisor pass failed", exc_info=True)

    async def _supervise_once(self) -> None:
        """One supervision pass: health-check every live, supervised preview and
        restart any that crashed (up to MAX_RESTARTS). Safe to call directly (tests
        drive it without the interval timer)."""
        async with self._lock:
            for session in list(self._sessions.values()):
                if not session._supervise:
                    continue
                if session.status is PreviewStatus.STOPPED:
                    continue
                # A CRASHED session is retried until the restart budget is spent; once
                # `restart_count` hits the cap it's terminal and we stop spinning on it.
                if (
                    session.status is PreviewStatus.CRASHED
                    and session.restart_count >= self.MAX_RESTARTS
                ):
                    continue
                # Initial startup failures are model-visible immediately. Do not
                # multiply one broken command into three hidden background launches;
                # the model can repair the command/code and explicitly call start.
                # Automatic restart is armed only after this preview has actually
                # served successfully once.
                if session.status is PreviewStatus.CRASHED and not session._auto_restart_armed:
                    continue
                if await self._probe_health(
                    session.port,
                    require_success=self._requires_successful_root(session),
                ):
                    # Recovered or steady — make sure the URL/status reflect health, but
                    # only after confirming the responder is OUR process (P1 #2).
                    if session.status in (PreviewStatus.STARTING, PreviewStatus.RESTARTING):
                        await self._confirm_and_mark_running(session)
                    continue
                # Not answering health. ONLY restart a preview whose PROCESS has ACTUALLY
                # exited. A STARTING/UNAVAILABLE/RESTARTING session (still booting, or up
                # but unroutable), or a slow/headless RUNNING one, whose shell session is
                # still alive is NOT crashed — it is not-yet-restartable. Restarting a
                # live session would re-`exec` into a still-busy shell (ShellSessionManager
                # raises busy), get misclassified as CRASHED by `_launch`, and burn a
                # restart from the budget for nothing. So the liveness guard applies to
                # EVERY state, not just RUNNING: the budget decrements only on a genuine
                # process exit.
                if await self._session_alive(session.name):
                    continue
                # An explicit recovery attempt remains disarmed until it proves
                # healthy. If it exits first, surface the crash without silently
                # launching it again in the background.
                if not session._auto_restart_armed:
                    session.status = PreviewStatus.CRASHED
                    session._reset_budget_on_healthy = False
                    continue
                await self._restart(session)

    async def _restart(self, session: PreviewSession) -> None:
        # Liveness guard — the SINGLE chokepoint every restart path funnels through
        # (the supervisor AND start()'s CRASHED-refresh path). NEVER re-exec a preview
        # whose PROCESS is still alive: re-issuing into a busy shell raises
        # (ShellSessionManager busy), is then misread as a crash by `_launch`, and burns
        # a restart from the budget for a process that never exited. A live-but-unhealthy
        # preview is slow/booting/unroutable, not crashed, so leave `restart_count`
        # untouched and report it as STARTING. The budget decrements ONLY on a genuine
        # process exit. Centralizing the check here (not at each call site) means every
        # current and future caller of the re-exec point is covered.
        if await self._session_alive(session.name):
            session.status = PreviewStatus.STARTING
            session.detail = "process running; not yet answering health checks"
            return
        if session.restart_count >= self.MAX_RESTARTS:
            session._reset_budget_on_healthy = False
            session._auto_restart_armed = False
            session.status = PreviewStatus.CRASHED
            session.detail = f"crashed; restart budget ({self.MAX_RESTARTS}) exhausted"
            return
        session.restart_count += 1
        # A restarted OS process is a new live projection even when it uses the
        # same accepted command, port, and sandbox. Rotate at the actual re-exec
        # boundary so a pre-FINISHED observation cannot authorize a later process.
        session.projection_id = f"pv_{uuid.uuid4().hex}"
        session.status = PreviewStatus.RESTARTING
        session.detail = f"crash detected; restart #{session.restart_count}"
        _LOG.info("restarting crashed preview %s on port %d", session.name, session.port)
        await self._launch(session)
        if session.status is PreviewStatus.CRASHED and session.restart_count >= self.MAX_RESTARTS:
            session._auto_restart_armed = False
            session.detail = f"crashed; restart budget ({self.MAX_RESTARTS}) exhausted"

    async def _recover_explicit(
        self,
        session: PreviewSession,
        *,
        command: str,
        exec_dir: str | None,
        intent: dict[str, Any],
    ) -> None:
        """Give a crashed preview one explicit, non-amplifying recovery attempt.

        A user/model may fix the command/server or release a colliding listener after
        any failed launch. An explicit ``preview_start`` therefore gets ONE attempt
        without spending or resetting the automatic restart budget. A failure stays
        disarmed; only observed health resets that budget and re-arms supervision.
        """

        prior_restart_count = session.restart_count
        session._auto_restart_armed = False
        session._reset_budget_on_healthy = True
        if await self._session_alive(session.name):
            session.status = PreviewStatus.STARTING
            session.detail = "process running; waiting for explicit recovery health"
            return

        # Commit replacement intent only at the actual launch boundary. A mislabeled
        # CRASHED-but-live process launches nothing; staging new metadata in that case
        # would falsely attribute the old process and make a later restart execute a
        # command that had never been accepted as the running generation.
        session.command = command
        session.exec_dir = exec_dir
        session.intent = intent
        session.projection_id = f"pv_{uuid.uuid4().hex}"
        session.status = PreviewStatus.RESTARTING
        session.detail = "explicit recovery of crashed preview"
        _LOG.info(
            "explicitly recovering crashed preview %s on port %d",
            session.name,
            session.port,
        )
        await self._launch(session)
        if session.status is PreviewStatus.CRASHED:
            launch_detail = session.detail
            session.restart_count = prior_restart_count
            session._reset_budget_on_healthy = False
            session.detail = (
                f"explicit recovery failed; background restart remains disarmed: {launch_detail}"
            )

    # ---- status / logs / stop / list ---------------------------------------

    async def _refresh(self, session: PreviewSession) -> None:
        """Re-probe health for one session and update its status/URL (no restart)."""
        if session.status is PreviewStatus.STOPPED:
            return
        if await self._probe_health(
            session.port,
            require_success=self._requires_successful_root(session),
        ):
            await self._confirm_and_mark_running(session)
        elif session.status not in {PreviewStatus.CRASHED, PreviewStatus.STOPPED}:
            if await self._session_alive(session.name):
                session.detail = "running; health probe momentarily unanswered"
            else:
                session.status = PreviewStatus.CRASHED
                session.detail = "process exited"

    async def status(self, name: str | None = None) -> list[PreviewSession]:
        """Current status of one preview (by name) or all. Re-probes health first."""
        async with self._lock:
            targets = (
                [self._sessions[name]]
                if name is not None and name in self._sessions
                else list(self._sessions.values())
                if name is None
                else []
            )
            for session in targets:
                await self._refresh(session)
            return targets

    async def restart_canonical(self) -> PreviewSession | None:
        """Recover the selected managed preview from its exact accepted intent.

        The UI restart path must never fall back to ``SandboxSession.ensure_preview``:
        that legacy helper serves a guessed workspace directory and is not the runtime
        the user was viewing.  This method reuses only an intent already accepted by
        :meth:`start`; absent or malformed state fails closed.
        """

        async with self._lock:
            target: PreviewSession | None = None
            for name in reversed(self._selection_order):
                candidate = self._sessions.get(name)
                if candidate is not None:
                    target = candidate
                    break
            if target is None and self._sessions:
                target = next(reversed(self._sessions.values()))
            if target is None or not isinstance(target.intent, dict):
                return None

            values = {
                key: target.intent.get(key) for key in ("serve_dir", "command", "framework", "cwd")
            }
            if any(value is not None and not isinstance(value, str) for value in values.values()):
                return None
            serve_dir = values["serve_dir"]
            command = values["command"]
            framework = values["framework"]
            cwd = values["cwd"]
            supervise = target._supervise
            if not (serve_dir or command or framework):
                return None

            if target.status is PreviewStatus.STOPPED:
                port = await self._allocate_port(reclaim_name=target.name)
                registered = False
                try:
                    resolved = self._resolve_command(
                        port,
                        serve_dir=serve_dir,
                        command=command,
                        framework=framework,
                    )
                    exec_dir = await self._launch_workspace(cwd)
                    target = PreviewSession(
                        name=target.name,
                        port=port,
                        command=resolved,
                        exec_dir=exec_dir,
                        intent=dict(target.intent),
                        _supervise=supervise,
                    )
                    self._sessions[target.name] = target
                    registered = True
                    await self._launch(target)
                except BaseException:
                    if not registered:
                        self._release_port_lease(port)
                    raise
            else:
                await self._refresh(target)
                if target.status is PreviewStatus.CRASHED:
                    resolved = self._resolve_command(
                        target.port,
                        serve_dir=serve_dir,
                        command=command,
                        framework=framework,
                    )
                    exec_dir = await self._launch_workspace(cwd)
                    await self._recover_explicit(
                        target,
                        command=resolved,
                        exec_dir=exec_dir,
                        intent=dict(target.intent),
                    )
            self._record_explicit_selection(target)

        if target._supervise:
            self._ensure_supervisor()
        return target

    async def restore_sealed(self, contract: Any) -> PreviewSession | None:
        """Revalidate and launch one host-derived sealed runtime contract.

        The contract supplies only raw PreviewStart intent.  ``start`` performs
        the normal command grammar, port allocation, cwd resolution, health, and
        ownership checks; persisted resolved commands and ports are never run.
        """

        contract_id = getattr(contract, "contract_id", None)
        start_kwargs = getattr(contract, "start_kwargs", None)
        expected_intent_fn = getattr(contract, "intent", None)
        if (
            not isinstance(contract_id, str)
            or not contract_id.startswith("sealed-preview:")
            or not callable(start_kwargs)
            or not callable(expected_intent_fn)
        ):
            return None
        try:
            kwargs = start_kwargs()
            expected_intent = expected_intent_fn()
        except Exception:  # noqa: BLE001 - malformed persisted authority fails closed
            return None
        if not isinstance(kwargs, dict) or not isinstance(expected_intent, dict):
            return None
        session = await self.start(**kwargs)
        if not self._is_servable(session) or session.intent != expected_intent:
            return None
        self._sealed_contracts[contract_id] = (session.name, dict(expected_intent))
        return session

    async def resolve_sealed_contract(self, contract: Any) -> PreviewSession | None:
        """Resolve only a current healthy session host-bound to ``contract``."""

        contract_id = getattr(contract, "contract_id", None)
        if not isinstance(contract_id, str):
            return None
        async with self._lock:
            binding = self._sealed_contracts.get(contract_id)
            if binding is None:
                return None
            name, expected_intent = binding
            session = self._sessions.get(name)
            if (
                session is None
                or session.status is PreviewStatus.STOPPED
                or session.intent != expected_intent
            ):
                return None
            expected_sandbox = (
                getattr(session, "sandbox_instance_id", None),
                getattr(session, "sandbox_generation", None),
            )
            current_sandbox = (
                getattr(self._sandbox, "id", None),
                getattr(self._sandbox, "generation", None),
            )
            if expected_sandbox != current_sandbox:
                return None
            if not await self._probe_health(
                session.port,
                require_success=self._requires_successful_root(session),
            ):
                return None
            if await self._verify_owns_port(session) is False:
                return None
            if (
                getattr(self._sandbox, "id", None),
                getattr(self._sandbox, "generation", None),
            ) != expected_sandbox:
                return None
            await self._confirm_and_mark_running(session)
            return session

    async def resolve_active_projection(self, projection: Any) -> PreviewSession | None:
        """Match one exact live projection without restart, repair, or allocation."""

        from .preview_projection import projection_identity

        async with self._lock:
            session = self._sessions.get(getattr(projection, "session_name", ""))
            if session is None or session.status is PreviewStatus.STOPPED:
                return None
            # Read the *current* SandboxSession identity before probing.  The
            # PreviewSession fields describe the generation that originally became
            # healthy; a dead/recreated box must not inherit that cached authority.
            # In particular, do not let the health probe lazily create a new box.
            expected_sandbox = (
                getattr(projection, "sandbox_instance_id", None),
                getattr(projection, "sandbox_generation", None),
            )
            current_sandbox = (
                getattr(self._sandbox, "id", None),
                getattr(self._sandbox, "generation", None),
            )
            if current_sandbox != expected_sandbox:
                return None
            if not await self._probe_health(
                session.port,
                require_success=self._requires_successful_root(session),
            ):
                return None
            # A typed backend-death probe may replace the underlying instance.
            # Recheck after the await so that replacement fails this request closed.
            if (
                getattr(self._sandbox, "id", None),
                getattr(self._sandbox, "generation", None),
            ) != expected_sandbox:
                return None
            if await self._verify_owns_port(session) is False:
                return None
            if (
                getattr(self._sandbox, "id", None),
                getattr(self._sandbox, "generation", None),
            ) != expected_sandbox:
                return None
            current = session.to_dict()
            current_identity = (
                current.get("projection_id"),
                current.get("name"),
                current.get("port"),
                current.get("launch_kind"),
                current.get("intent_digest"),
                current.get("sandbox_instance_id"),
                current.get("sandbox_generation"),
            )
            return session if current_identity == projection_identity(projection) else None

    async def logs(self, name: str | None = None, *, tail_chars: int = 4000) -> dict[str, str]:
        """Recent stdout/stderr per preview (from the in-sandbox shell session)."""
        async with self._lock:
            if name is not None:
                names = [name] if name in self._sessions else []
            else:
                names = list(self._sessions.keys())
        out: dict[str, str] = {}
        for n in names:
            try:
                view = await self._sandbox.sessions.view(n, tail_chars)
                out[n] = getattr(view, "output", "")
            except Exception as exc:  # noqa: BLE001
                out[n] = f"(logs unavailable: {exc})"
        return out

    async def stop(self, name: str | None = None) -> list[str]:
        """Stop one preview (by name) or all. Returns the names stopped."""
        async with self._lock:
            names = (
                [name]
                if name is not None and name in self._sessions
                else list(self._sessions.keys())
                if name is None
                else []
            )
            stopped: list[str] = []
            for n in names:
                stopped.append(await self._stop_locked(n))
            return stopped

    async def _stop_locked(self, name: str) -> str:
        """Stop one tracked preview. Caller owns `_lock` when coordinating with state."""
        session = self._sessions[name]
        try:
            stop_server = getattr(self._sandbox.sessions, "stop_foreground_server", None)
            if stop_server is not None:
                await stop_server(
                    name,
                    expected_command=session.command,
                    expected_port=session.port,
                )
            else:
                await self._sandbox.sessions.kill_foreground(name)
        except Exception:  # noqa: BLE001 — best-effort; mark stopped regardless
            _LOG.debug("kill_foreground failed for preview %s", name, exc_info=True)
        session.status = PreviewStatus.STOPPED
        session.url = None
        session.detail = "stopped on request"
        self._release_port_lease(session.port)
        return name

    def list(self) -> list[PreviewSession]:
        """Snapshot of every preview this manager tracks (a fresh copy)."""
        return list(self._sessions.values())

    async def aclose(self) -> None:
        """Cancel supervision and stop all previews (conversation teardown)."""
        self._closed = True
        task, self._supervisor = self._supervisor, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.stop(None)
        for port in tuple(self._port_leases):
            self._release_port_lease(port)


def _default_port_pool(sandbox: Any) -> list[int]:
    """The curated platform preview-port pool, in preference order.

    Built from the SAME curated ports the rest of the platform exposes (USER_PORTS),
    minus the noVNC bridge. On a SHARED-host backend (process/local) the agent-server's
    own control ports (8000/8800/5173) are excluded so the platform can never allocate
    a preview onto a port the dev stack already owns — keeping the model out of port
    selection must not put the PLATFORM into a port collision either."""
    from disco.core.loop.preview_target import (
        PREVIEW_PORTS,
        managed_host_preview_ports,
        reserved_control_ports,
    )
    from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS

    ordered = [p for p in PREVIEW_PORTS if p in USER_PORTS and p != NOVNC_PORT]
    explicit = getattr(sandbox, "shares_host_network", None)
    host_shared = (
        explicit
        if isinstance(explicit, bool)
        else (getattr(sandbox, "backend_name", "") or "") == "process"
    )
    if host_shared:
        reserved = reserved_control_ports()
        ordered = [p for p in ordered if p not in reserved]
        dynamic = list(managed_host_preview_ports())
        # Conversations start at different points in the large range, avoiding
        # a herd on its first socket while retaining deterministic allocation.
        identity = str(getattr(sandbox, "conversation_id", "") or "")
        if dynamic and identity:
            offset = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big") % len(
                dynamic
            )
            dynamic = dynamic[offset:] + dynamic[:offset]
        ordered.extend(port for port in dynamic if port not in ordered)
    return ordered
