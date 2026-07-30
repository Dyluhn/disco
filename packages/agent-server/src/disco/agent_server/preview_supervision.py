"""Crash supervision and explicit preview recovery."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from .preview_launch import _PreviewLaunchResource
from .preview_models import PreviewSession, PreviewStatus

_LOG = logging.getLogger("disco.agent_server.preview_manager")


class _PreviewSupervisedResource(_PreviewLaunchResource):
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
