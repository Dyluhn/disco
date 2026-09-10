"""Starting, selecting, and health-proving preview generations."""

from __future__ import annotations

import asyncio
import shlex
from abc import abstractmethod
from typing import Any

from .preview_command import _PreviewCommandResource
from .preview_models import _TMUX_PREFIX, PreviewSession, PreviewStatus


class _PreviewLaunchResource(_PreviewCommandResource):
    @abstractmethod
    def _ensure_supervisor(self) -> None: ...

    @abstractmethod
    async def _recover_explicit(
        self,
        session: PreviewSession,
        *,
        command: str,
        exec_dir: str | None,
        intent: dict[str, Any],
    ) -> None: ...

    @abstractmethod
    async def _refresh(self, session: PreviewSession) -> None: ...

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
