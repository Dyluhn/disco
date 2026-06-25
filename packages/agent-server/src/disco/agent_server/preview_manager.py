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
import logging
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_LOG = logging.getLogger(__name__)


class PreviewStatus(str, Enum):
    """Lifecycle state of one preview, as the platform sees it."""

    STARTING = "starting"  # process launched, not yet answering health
    RUNNING = "running"  # answering health on the platform port; URL exposed
    UNAVAILABLE = "unavailable"  # process up but the backend can't expose a URL (graceful degrade)
    RESTARTING = "restarting"  # crash detected; supervisor is re-issuing the command
    CRASHED = "crashed"  # crashed and the restart budget is exhausted
    STOPPED = "stopped"  # stopped on request


# How a bare framework name maps to a start command. `{port}` is filled with the
# PLATFORM-allocated port — never anything the model supplied. Anything not listed
# falls through to the static file server (the safe MVP default).
_FRAMEWORK_COMMANDS: dict[str, str] = {
    "static": "python3 -m http.server {port} -d {dir}",
    "http": "python3 -m http.server {port} -d {dir}",
    "vite": "npm run dev -- --port {port} --host 0.0.0.0",
    "next": "npm run dev -- -p {port}",
    "nextjs": "npm run dev -- -p {port}",
    "react": "PORT={port} npm start",
    "cra": "PORT={port} npm start",
    "astro": "npm run dev -- --port {port} --host 0.0.0.0",
    "svelte": "npm run dev -- --port {port} --host 0.0.0.0",
    "node": "PORT={port} npm start",
    "express": "PORT={port} npm start",
}

# Port flags a model might bake into a raw `command`. We SCRUB these so the platform
# port can never be overridden through the command string — the platform port wins.
_PORT_FLAG_RE = re.compile(
    r"""(?xi)
    (?:^|\s)
    (?:
        --port(?:=|\s+)\d+        # --port 3000 | --port=3000
      | -p(?:=|\s+)\d+            # -p 3000     | -p=3000
      | PORT=\d+                  # PORT=3000 env assignment
    )
    """
)

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


# tmux session-name prefix the ShellSessionManager writes (see shell_sessions._PREFIX).
# Used to confirm a listening port is owned by THIS preview's shell session.
_TMUX_PREFIX = "disco"


class PreviewCommandError(ValueError):
    """A raw `command` cannot be made platform-port-safe (it binds a hardcoded port via
    a form the platform can't override). The model must remove the port or use the
    literal `{port}` placeholder. Surfaced to the model as a recoverable tool failure."""


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
    status: PreviewStatus = PreviewStatus.STARTING
    url: str | None = None
    restart_count: int = 0
    detail: str = ""
    _supervise: bool = field(default=True, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "status": self.status.value,
            "url": self.url,
            "command": self.command,
            "exec_dir": self.exec_dir,
            "intent": self.intent,
            "restart_count": self.restart_count,
            "detail": self.detail,
        }


class NoPreviewPortAvailableError(RuntimeError):
    """The platform's preview port pool is exhausted — every curated port is in use."""


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
        self._lock = asyncio.Lock()
        self._supervisor: asyncio.Task[None] | None = None
        self._closed = False
        self._auto_preview_coordinated = False  # P1 #2: legacy auto-preview stood down once

    # ---- platform-owned port allocation ------------------------------------

    async def _allocate_port(self) -> int:
        """THE single place a preview port is chosen. The model has no input here — the
        port comes from the curated platform pool, skipping (a) any port already held by
        one of our previews, (b) any port the SANDBOX is already tracking as a service —
        the legacy static auto-preview or an agent-launched dev server (P1 #2: allocating
        onto one would mean the legacy server's response falsely validates ours), and
        (c) any port with a LIVE listener right now (a real owner we don't track). This
        is the ownership point that kills port-fixation AND cross-server contamination."""
        taken = {s.port for s in self._sessions.values() if s.status is not PreviewStatus.STOPPED}
        taken |= self._sandbox_tracked_ports()
        for port in self._pool:
            if port in taken:
                continue
            # A real listener already owns this curated port (not one of ours) →
            # skip it, or its response would falsely validate a process that EADDRINUSE'd.
            if await self._probe_health(port):
                continue
            return port
        raise NoPreviewPortAvailableError(
            f"all {len(self._pool)} platform preview ports are in use: {sorted(self._pool)}"
        )

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

    def _reject_positional_port(self, residual: str) -> None:
        """Parse the (operator-free, placeholder-blanked, flag-scrubbed) command with
        `shlex.split` and reject any BARE positional port-like token — a hardcoded port the
        platform can't override that the flag-scrub didn't catch (`http.server <port>`, or a
        port positioned anywhere). HEURISTIC: a numeric token immediately following a flag
        (a token starting with `-`) is treated as that flag's VALUE, not a port, so legit
        numeric args like `--workers 4` / `--timeout 30` are not false-rejected. An
        unparseable command (e.g. unbalanced quotes) is refused rather than silently run."""
        try:
            tokens = shlex.split(residual)
        except ValueError as exc:
            raise PreviewCommandError(
                f"preview command could not be parsed as a single shell command ({exc}). "
                "Provide a simple foreground command with the serve port as '{port}'."
            ) from exc
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

        Precedence: an explicit `command` (port scrubbed/validated + injected) > a known
        `framework` template > static file serving of `serve_dir` (the safe MVP default).

        A raw command never gets to pick the port: any `--port/-p/PORT=` the model
        embedded is stripped and the platform port is injected via `PORT=`. A raw command
        that ALSO binds a port through a form the scrub can't override (a positional
        `http.server <port>`, a `host:port` bind) is REJECTED (`PreviewCommandError`) —
        unless it uses the literal `{port}` placeholder, which the platform fills with
        ITS port (the explicit, safe way to put the platform port at a positional slot).
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
            scrubbed = _PORT_FLAG_RE.sub(" ", command).strip()
            has_placeholder = "{port}" in scrubbed
            # Reject ANY other hardcoded/positional port the flag-scrub can't override —
            # even on the `{port}`-placeholder path. The placeholder is the ONE sanctioned
            # way to put the platform port at a positional slot, but the REST of the command
            # must still be port-clean: a second, hardcoded port alongside `{port}` (e.g.
            # 'http.server {port} 9999') would bind a model-chosen port the platform doesn't
            # own (P1 #1). Blank the placeholder out so only OTHER ports trip the check.
            residual = scrubbed.replace("{port}", " ") if has_placeholder else scrubbed
            # shlex-tokenize the single command and reject any BARE positional port-like
            # token (covers `http.server [flags] <port> [more]` and a port positioned
            # anywhere). A numeric token immediately after a flag is that flag's value, not
            # a port, so `--workers 4` / `--timeout 30` pass. The `{port}` placeholder is the
            # only sanctioned way to express the serve port positionally.
            self._reject_positional_port(residual)
            # Backstop for `host:port` / `:port` BIND forms (gunicorn `-b :8000`, `serve -l
            # 0.0.0.0:8000`) that carry a colon and so are not a bare integer token above.
            bound = _RAW_HARDCODED_PORT_RE.search(residual)
            if bound is not None:
                raise PreviewCommandError(
                    "preview command binds a hardcoded port "
                    f"({bound.group(0).strip()!r}) — the platform owns the port, so it "
                    "can't be supplied here. Remove the port (the platform injects PORT="
                    "<its port>), or put the literal placeholder '{port}' where the port "
                    "goes (e.g. 'python3 -m http.server {port} -d dist')."
                )
            if has_placeholder:
                # Model explicitly delegated the port slot to the platform — fill it with
                # OUR port (and still export PORT for env-reading servers).
                filled = scrubbed.replace("{port}", str(port)).strip()
                return f"PORT={port} {filled}"
            # Inject the platform port as an env var (honored by Node/Vite/Next/CRA/
            # Flask-via-env, …). The static template path below is used when the model
            # gives a dir/framework instead — that bakes the port into the flag directly.
            return f"PORT={port} {scrubbed}"
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
            return None  # a listener exists but no tmux attribution → inconclusive
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
                    await self._restart(existing)
                return existing

            port = await self._allocate_port()
            resolved = self._resolve_command(
                port, serve_dir=serve_dir, command=command, framework=framework
            )
            # P1 #3: run the server FROM the workspace (not serve_dir) so the static
            # `-d <serve_dir>` resolves to workspace/<serve_dir> — running from serve_dir
            # with `-d <serve_dir>` served `<serve_dir>/<serve_dir>` (404 / wrong tree).
            exec_dir = cwd or self._sandbox_workspace() or serve_dir
            session = PreviewSession(
                name=key,
                port=port,
                command=resolved,
                exec_dir=exec_dir,
                intent={
                    "serve_dir": serve_dir,
                    "command": command,
                    "framework": framework,
                },
                _supervise=supervise,
            )
            self._sessions[key] = session
            await self._launch(session)

        if supervise:
            self._ensure_supervisor()
        return session

    async def _launch(self, session: PreviewSession) -> None:
        """Issue the (re)start command in the sandbox + poll health to a verdict."""
        session.status = PreviewStatus.STARTING
        try:
            await self._sandbox.sessions.exec(
                session.name, session.command, exec_dir=session.exec_dir
            )
        except Exception as exc:  # noqa: BLE001 — surface as a status, never raise into a tool
            session.status = PreviewStatus.CRASHED
            session.detail = f"failed to launch: {exc}"
            return
        await self._poll_until_healthy(session)

    async def _poll_until_healthy(self, session: PreviewSession) -> None:
        for _ in range(self._health_attempts):
            if await self._probe_health(session.port):
                # Health alone is not enough — confirm OUR process owns the port before
                # RUNNING (a foreign server answering ⇒ CRASHED, not a false success).
                if not await self._confirm_and_mark_running(session):
                    return
                return
            await asyncio.sleep(self._health_interval_s)
        # Never answered in time. Distinguish "process is up but no URL" from a crash:
        # if the shell session is still running, it's just slow/headless → STARTING.
        if await self._session_alive(session.name):
            session.status = PreviewStatus.STARTING
            session.detail = "process running; not yet answering health checks"
        else:
            session.status = PreviewStatus.CRASHED
            session.detail = "process exited before serving"

    def _mark_running(self, session: PreviewSession) -> None:
        """Healthy on the platform port — expose the URL, or degrade gracefully."""
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

    async def _probe_health(self, port: int) -> bool:
        """Backend-agnostic liveness: curl the server from INSIDE the sandbox (the one
        place it's reachable on every backend, sealed-network included). True iff it
        answers with any HTTP status."""
        try:
            res = await self._sandbox.fetch_inside(port, "/", timeout_s=5)
        except Exception:  # noqa: BLE001 — a probe must never raise into supervision
            return False
        return res is not None

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
                if await self._probe_health(session.port):
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
            session.status = PreviewStatus.CRASHED
            session.detail = f"crashed; restart budget ({self.MAX_RESTARTS}) exhausted"
            return
        session.restart_count += 1
        session.status = PreviewStatus.RESTARTING
        session.detail = f"crash detected; restart #{session.restart_count}"
        _LOG.info("restarting crashed preview %s on port %d", session.name, session.port)
        await self._launch(session)

    # ---- status / logs / stop / list ---------------------------------------

    async def _refresh(self, session: PreviewSession) -> None:
        """Re-probe health for one session and update its status/URL (no restart)."""
        if session.status is PreviewStatus.STOPPED:
            return
        if await self._probe_health(session.port):
            await self._confirm_and_mark_running(session)
        elif session.status is PreviewStatus.RUNNING:
            if await self._session_alive(session.name):
                session.detail = "running; health probe momentarily unanswered"
            else:
                session.status = PreviewStatus.CRASHED
                session.detail = "process exited"

    async def status(self, name: str | None = None) -> list[PreviewSession]:
        """Current status of one preview (by name) or all. Re-probes health first."""
        async with self._lock:
            targets = (
                [self._sessions[name]] if name is not None and name in self._sessions
                else list(self._sessions.values()) if name is None
                else []
            )
            for session in targets:
                await self._refresh(session)
            return targets

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
                [name] if name is not None and name in self._sessions
                else list(self._sessions.keys()) if name is None
                else []
            )
            stopped: list[str] = []
            for n in names:
                session = self._sessions[n]
                try:
                    await self._sandbox.sessions.kill_foreground(n)
                except Exception:  # noqa: BLE001 — best-effort; mark stopped regardless
                    _LOG.debug("kill_foreground failed for preview %s", n, exc_info=True)
                session.status = PreviewStatus.STOPPED
                session.url = None
                session.detail = "stopped on request"
                stopped.append(n)
            return stopped

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


def _default_port_pool(sandbox: Any) -> list[int]:
    """The curated platform preview-port pool, in preference order.

    Built from the SAME curated ports the rest of the platform exposes (USER_PORTS),
    minus the noVNC bridge. On a SHARED-host backend (process/local) the agent-server's
    own control ports (8000/8800/5173) are excluded so the platform can never allocate
    a preview onto a port the dev stack already owns — keeping the model out of port
    selection must not put the PLATFORM into a port collision either."""
    from disco.core.loop.preview_target import PREVIEW_PORTS, reserved_control_ports
    from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS

    ordered = [p for p in PREVIEW_PORTS if p in USER_PORTS and p != NOVNC_PORT]
    backend = getattr(sandbox, "backend_name", "") or ""
    if backend in ("process", "local"):
        reserved = reserved_control_ports()
        ordered = [p for p in ordered if p not in reserved]
    return ordered
