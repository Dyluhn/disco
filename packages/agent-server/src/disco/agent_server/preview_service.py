"""Live-preview proxy + sandbox wake — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The preview-facing
accessors move out of runtime.py into a `PreviewService` collaborator
constructed once in `ConversationRuntime`: cid-prefix resolution
(`resolve_cid_prefix`), upstream port resolution (`port_upstream`), the
suspended-sandbox wake (`wake_for_preview`), the passive availability probe
(`preview`), and the 'Restart preview' rematerialize path (`ensure_preview`).

The service reaches the runtime's live state (`_executors`, `_wake_locks`,
`_store`) + the `_project_store_now` / `_loop_for` / `_maybe_rehydrate`
resolvers via a back-reference. `preview_upstream` stays on the runtime and
calls the `port_upstream` delegator. Every moved method keeps a one-line
delegator on `ConversationRuntime` because routes call each on the runtime
(and `wake_for_preview`'s internal cross-calls route back through the runtime).
"""

from __future__ import annotations

import asyncio
from typing import Any

from disco.core import DEFAULT_OWNER_ID
from disco.tools.projects import StorageStatus
from disco.tools.sandbox._container import PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.port_owner import port_owners


class PreviewService:
    def __init__(self, rt: Any) -> None:
        self._rt = rt

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        """Full conversation id whose uuid part starts with cid8 — live executors only
        (a preview without a live sandbox is a 503 anyway). Ambiguous (>1) → None."""
        matches = [
            cid
            for cid in self._rt._executors.keys()
            if cid.removeprefix("conv_").startswith(cid8)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def port_upstream(self, conversation_id: str, port: int) -> str | None:
        """Generalized upstream resolution for any curated USER port (BP-10).
        expose_port itself refuses non-USER ports — defense stays in the backend."""
        executor = self._rt._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return None
        if getattr(getattr(session, "_service", None), "name", "?") == "podman":
            return None  # stub here
        return session.expose_port(port)

    async def wake_for_preview(self, cid8: str, port: int) -> str | None:
        """Wake a suspended sandbox if a preview request hits it.
        Restores the workspace and the built-in static preview server on 8000.
        It does NOT restart agent-started dev servers (vite/express) — requests
        for ports nothing listens on after wake will proxy to a 502.
        """
        cid = self._rt.resolve_cid_prefix(cid8)
        if cid is not None:
            return self._rt.port_upstream(cid, port)

        try:
            summaries = await self._rt._store.list_conversation_summaries(
                owner_id=DEFAULT_OWNER_ID, limit=500, cursor=None
            )
            matches = [
                s.conversation_id
                for s in summaries
                if s.conversation_id.removeprefix("conv_").startswith(cid8)
            ]
            if len(matches) != 1:
                return None
            cid = matches[0]
        except Exception:
            return None

        lock = self._rt._wake_locks.setdefault(cid, asyncio.Lock())
        async with lock:
            if cid in self._rt._executors:
                return self._rt.port_upstream(cid, port)
            woke = await self._rt.ensure_preview(cid)
            if woke:
                return self._rt.port_upstream(cid, port)
            return None

    async def preview(self, conversation_id: str) -> dict[str, Any]:
        """Backend-aware live preview availability. The browser iframes the agent-server's
        proxy (/conversations/{id}/preview-app/), which forwards to the active backend's
        dev server — so previews work over the tailnet via the one reachable origin, with no
        random container ports exposed. Podman is an honest labeled stub; never a fake URL."""
        executor = self._rt._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return {"available": False, "reason": "The agent hasn't started a sandbox yet."}
        if getattr(getattr(session, "_service", None), "name", "?") == "podman":
            return {
                "available": False,
                "stub": True,
                "reason": "Preview isn't wired for the Podman backend in this environment "
                "— it's completed at deployment.",
            }
        # Passive probe ONLY: this GET is polled by the UI, and a read path must not
        # create a sandbox (that's _ensure()'s side effect) or surface its failures
        # as a 500. No live instance → no preview, plainly stated.
        inst = getattr(session, "_instance", None)
        if inst is None:
            return {
                "available": False,
                "reason": "The agent's sandbox isn't running yet.",
                "owner": None,
            }
        try:
            owners = await port_owners(inst, sorted(USER_PORTS))
        except Exception:  # noqa: BLE001 — a probe must never 500 the preview endpoint
            owners = {}
        owner = owners.get(PREVIEW_PORT)

        ns = f"pmx-{session.sessions.namespace}"

        def _owner_json(o):  # bound ports only; normalized session name
            sess = o.session
            if sess and sess.startswith(ns):
                sess = sess[len(ns):]
            return {"pid": o.pid, "cmdline": o.cmdline, "session": sess}

        ports_payload = [
            {"port": p, "owner": _owner_json(o)}
            for p, o in sorted(owners.items())
            if o is not None and o.pid is not None
        ]

        if owner is None or owner.pid is None:
            return {
                "available": False,
                "reason": f"No dev server detected. Run one on port {PREVIEW_PORT} inside the "
                "sandbox to see a live preview.",
                "owner": None,
                "ports": ports_payload,
            }

        return {
            "available": True,
            "proxy": True,
            "owner": _owner_json(owner),
            "ports": ports_payload,
        }

    async def ensure_preview(self, conversation_id: str) -> bool:
        """Backend half of the UI 'Restart preview' button (§E7). Bounded + safe: the
        same idempotent restart as SandboxSession.ensure_preview.

        After a clean FINISH the sandbox is torn down (the G safe-leak fix in `_run`),
        which would make this button a dead affordance. Instead, re-materialize through
        the documented resume path: re-compose the loop/executor (lazy sandbox),
        rehydrate the snapshot, then start the preview — the user explicitly asked to
        see the artifact again, and that's exactly what the snapshot is for."""
        executor = self._rt._executors.get(conversation_id)
        if executor is None:
            store = self._rt._project_store_now()
            if store is None or store.status() != StorageStatus.OK:
                return False
            try:
                record = store.get(conversation_id)
            except Exception:  # noqa: BLE001 — manifest unreadable: nothing to revive
                return False
            if record is None or record.files_missing:
                return False  # never had a build snapshot (e.g. research) — no preview
            self._rt._loop_for(conversation_id)  # registers a fresh executor + lazy sandbox
            await self._rt._maybe_rehydrate(conversation_id)
            executor = self._rt._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return False
        try:
            return await session.ensure_preview()
        except Exception:  # noqa: BLE001
            return False
