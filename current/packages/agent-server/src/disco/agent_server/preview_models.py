"""Preview intent, identity, and serialized session contracts."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - the process backend is POSIX-only today
    _fcntl = None  # type: ignore[assignment]

_LOG = logging.getLogger("disco.agent_server.preview_manager")


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


@dataclass(frozen=True, slots=True)
class _PreviewPortPool:
    """Typed configuration boundary for platform-owned preview ports."""

    ports: tuple[int, ...]


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
            # `url` is the HOST-published address, for the USER's browser. It is
            # NOT reachable from inside the sandbox.
            "url": self.url,
            # …and this is the one every IN-SANDBOX consumer must use: the agent's
            # shell (curl), and equally the agent's own `browser` tool, which also
            # runs inside the sandbox.
            #
            # Counted-promotion failure 2026-07-27 (p4_ff_node_pause seed 400025):
            # only `url` was published structurally, so the agent passed the host
            # address to `browser navigate` and the navigation could not connect.
            # The in-sandbox address existed only in prose that warned about
            # `curl` alone, which read as an endorsement for the browser. Both
            # addresses are now first-class fields so neither consumer has to
            # infer which one it is entitled to.
            "in_sandbox_url": f"http://localhost:{self.port}/",
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
