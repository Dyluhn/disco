"""Canonical conversation-scoped preview resource authority."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from .preview_models import PreviewSession, PreviewStatus
from .preview_supervision import _PreviewSupervisedResource

_LOG = logging.getLogger("disco.agent_server.preview_manager")


class PreviewResource(_PreviewSupervisedResource):
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

    def _canonical_restart_target(self) -> PreviewSession | None:
        for name in reversed(self._selection_order):
            candidate = self._sessions.get(name)
            if candidate is not None:
                return candidate
        return next(reversed(self._sessions.values())) if self._sessions else None

    @staticmethod
    def _restart_values(
        target: PreviewSession,
    ) -> tuple[str | None, str | None, str | None, str | None] | None:
        if not isinstance(target.intent, dict):
            return None
        values = tuple(
            target.intent.get(key) for key in ("serve_dir", "command", "framework", "cwd")
        )
        if any(value is not None and not isinstance(value, str) for value in values):
            return None
        serve_dir, command, framework, cwd = values
        return (
            cast(tuple[str | None, str | None, str | None, str | None], values)
            if serve_dir or command or framework
            else None
        )

    async def _relaunch_stopped(
        self,
        target: PreviewSession,
        values: tuple[str | None, str | None, str | None, str | None],
    ) -> PreviewSession:
        serve_dir, command, framework, cwd = values
        port = await self._allocate_port(reclaim_name=target.name)
        registered = False
        try:
            resolved = self._resolve_command(
                port,
                serve_dir=serve_dir,
                command=command,
                framework=framework,
            )
            replacement = PreviewSession(
                name=target.name,
                port=port,
                command=resolved,
                exec_dir=await self._launch_workspace(cwd),
                intent=dict(target.intent),
                _supervise=target._supervise,
            )
            self._sessions[target.name] = replacement
            registered = True
            await self._launch(replacement)
            return replacement
        except BaseException:
            if not registered:
                self._release_port_lease(port)
            raise

    async def _repair_crashed(
        self,
        target: PreviewSession,
        values: tuple[str | None, str | None, str | None, str | None],
    ) -> None:
        await self._refresh(target)
        if target.status is not PreviewStatus.CRASHED:
            return
        serve_dir, command, framework, cwd = values
        resolved = self._resolve_command(
            target.port,
            serve_dir=serve_dir,
            command=command,
            framework=framework,
        )
        await self._recover_explicit(
            target,
            command=resolved,
            exec_dir=await self._launch_workspace(cwd),
            intent=dict(target.intent),
        )

    async def restart_canonical(self) -> PreviewSession | None:
        """Recover the selected managed preview from its exact accepted intent."""

        async with self._lock:
            target = self._canonical_restart_target()
            values = self._restart_values(target) if target is not None else None
            if target is None or values is None:
                return None
            if target.status is PreviewStatus.STOPPED:
                target = await self._relaunch_stopped(target, values)
            else:
                await self._repair_crashed(target, values)
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
        # One conversation has one authoritative committed Preview revision.
        # A freshly revalidated sealed contract supersedes every older binding,
        # even when the compatible session name and raw intent are unchanged.
        self._sealed_contracts.clear()
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
        except Exception:
            _LOG.debug("kill_foreground failed for preview %s", name, exc_info=True)
            # A process we failed to terminate remains ours.  Retain its live
            # state and kernel lease so another manager cannot claim the port.
            raise
        session.status = PreviewStatus.STOPPED
        session.url = None
        session.detail = "stopped on request"
        self._release_port_lease(session.port)
        return name

    def list(self) -> list[PreviewSession]:
        """Snapshot of every preview this manager tracks (a fresh copy)."""
        return list(self._sessions.values())

    def canonical_url(self) -> str | None:
        """URL of the selected servable session, without fabricating a fallback."""
        session = self.canonical_session()
        return session.url if session is not None else None

    def owns_sandbox(self) -> bool:
        """Whether this resource must destroy its isolated sandbox at close."""
        return self._owns_sandbox

    async def aclose(self) -> None:
        """Cancel supervision and stop all previews (conversation teardown)."""
        if (
            self._closed
            and not self._owns_sandbox
            and not self._port_leases
            and all(session.status is PreviewStatus.STOPPED for session in self._sessions.values())
        ):
            return
        self._closed = True
        task, self._supervisor = self._supervisor, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if not self._owns_sandbox:
            await self.stop(None)
            for port in tuple(self._port_leases):
                self._release_port_lease(port)
            return

        # Destroying the owned sandbox is the process-termination proof.  If
        # destruction fails, retain both ownership and leases so another resource
        # cannot claim ports that may still be serving.
        await self._sandbox.destroy()
        self._owns_sandbox = False
        for session in self._sessions.values():
            session.status = PreviewStatus.STOPPED
            session.url = None
            session.detail = "owned sandbox destroyed"
        for port in tuple(self._port_leases):
            self._release_port_lease(port)
