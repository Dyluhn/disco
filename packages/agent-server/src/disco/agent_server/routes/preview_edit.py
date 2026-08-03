"""A1.1b — the click-to-edit preview route.

``GET /conversations/{cid}/preview-edit/{path:path}``

Serves a workspace HTML file as a *server-stamped, selection-instrumented* page so
the frontend's "Edit" mode can iframe it (``src=``) and offer click-to-edit. This
is the corrected keystone wiring (A1 design, "CORRECTED architecture" §): the
static-site preview is normally CLIENT-assembled (``deriveSrcDoc`` → ``srcDoc``),
and the ``/artifacts`` inline route is jailed to DECLARED artifacts with a
``default-src 'none'`` CSP that would block any injected script. Neither can host
the selection agent, so edit-mode needs its own route.

What it does, per request:
  1. Jail ``path`` to the conversation workspace (reject ``..`` / absolute / escape
     — mirrors files.py / preview.py).
  2. For FINISHED, read only the immutable workspace named by its final seal;
     otherwise use the symlink-safe mutable host mirror.
  3. Non-HTML or missing → 404.
  4. ``stamp_oids(html, path)`` — add ``data-oid="{relpath}:{line}"`` to every
     element (the keystone; serve-time only, the agent's source stays UNSTAMPED).
  5. Inject the in-frame selection agent as ``<script nonce="…">`` before
     ``</body>`` (the SAME canonical script the frontend runs — see below).
  6. Return ``text/html`` under a TAILORED CSP that permits the nonce'd script
     but otherwise locks the page down (``default-src 'self'``; ``script-src
     'nonce-…'``; ``frame-ancestors 'self'``).

Selection-script single source of truth: the IIFE lives in the co-located package
asset ``disco/agent_server/selection_agent.js`` (read here via
``importlib.resources`` so an installed package still finds it). The
frontend imports the SAME file via a Vite ``?raw`` import (selectionAgent.ts), so
the script the browser runs and the script injected here can never drift — there
is no second copy. We do NOT duplicate the script text in this module.

Sibling inlining: NOT needed for v1. The iframe's document base IS this route
(``/conversations/{cid}/preview-edit/{entry.html}``), so a relative ``<link
href="style.css">`` / ``<script src="app.js">`` resolves to
``/conversations/{cid}/preview-edit/style.css`` — i.e. back through THIS route,
which serves any workspace file (HTML gets stamped; non-HTML is returned with its
natural media type, unstamped). So relative siblings resolve without inlining. We
therefore do NOT mirror deriveSrcDoc's client-side inlining (that exists only
because a ``srcDoc`` iframe has no usable base URL; ``src=`` does).
"""

from __future__ import annotations

import importlib.resources
import posixpath
import secrets

from disco.core import ConversationStatus, StatusEvent
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, StorageStatus, is_runtime_secret_path
from fastapi import APIRouter, HTTPException, Request, Response

from ..oid_stamp import stamp_oids
from ..runtime import ConversationRuntime
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from ._common import require_owned_conversation


def _read_mutable_workspace_file_safe(
    runtime: ConversationRuntime, conversation_id: str, norm: str
) -> bytes | None:
    """Read a workspace file SYMLINK-SAFELY from the host ProjectStore snapshot.

    Deliberately NOT files.py's ``_read_artifact_bytes``: mutable reads there may use the LIVE
    container session first, where ``read_file`` runs ``cat -- target`` and FOLLOWS
    symlinks — so a planted ``leak.html -> /etc/passwd`` would escape the workspace.
    This route serves ANY workspace path (not just declared artifacts), so it must be
    symlink-safe. The host snapshot path resolves the real path and verifies it stays
    inside the workspace (``.resolve()`` follows symlinks, ``is_relative_to`` rejects
    escapes). Tradeoff: a just-written file not yet snapshotted won't preview-edit —
    acceptable, since click-to-edit targets a BUILT static site.
    """
    if is_runtime_secret_path(norm):
        return None
    ps = runtime.projects.current_project_store() if runtime is not None else None
    if ps is None or ps.status() != StorageStatus.OK:
        return None
    try:
        workspace = ps.path_for(conversation_id).resolve()
        resolved = (workspace / norm).resolve()
    except Exception:  # noqa: BLE001 — unresolvable path → not served
        return None
    if not resolved.is_relative_to(workspace) or not resolved.is_file():
        return None
    try:
        return resolved.read_bytes()
    except Exception:  # noqa: BLE001
        return None


async def _read_workspace_file_safe(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    conversation_id: str,
    norm: str,
) -> bytes | None:
    """Read FINISHED bytes from their seal; otherwise read the mutable mirror."""

    if is_runtime_secret_path(norm):
        return None
    try:
        events = await store.get_events(conversation_id)
    except Exception as exc:  # noqa: BLE001 — unknown state cannot authorize mutable bytes
        raise HTTPException(status_code=503, detail="workspace evidence unavailable") from exc
    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    if latest_status is None or latest_status.status is not ConversationStatus.FINISHED:
        return _read_mutable_workspace_file_safe(runtime, conversation_id, norm)

    project_store = runtime.projects.current_project_store()
    if project_store is None or project_store.status() != StorageStatus.OK:
        raise HTTPException(status_code=503, detail="finished workspace unavailable")
    try:
        committed = resolve_committed_workspace(events, project_store, conversation_id)
        with project_store.open_verified_version(
            conversation_id,
            committed.event.version_seq,
        ) as verified:
            if norm not in {entry.path for entry in verified.files}:
                return None
            return verified.read_bytes(norm, max_bytes=50 * 1024 * 1024)
    except WorkspaceCommitUnavailable as exc:
        raise HTTPException(status_code=503, detail="finished workspace is unsealed") from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="finished workspace proof failed") from exc


# Media types this route serves. HTML is stamped + script-injected; the sibling
# asset types are served as-is so relative refs from the stamped page resolve
# through this same route (see "Sibling inlining" in the module docstring).
_HTML_EXTS = frozenset({".html", ".htm"})
_SIBLING_MEDIA_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


def _load_selection_agent_script() -> str:
    """Read the canonical in-frame selection-agent IIFE from the co-located
    package asset. Single source of truth shared with the frontend (which imports
    the same file via Vite ``?raw``). Uses the importlib.resources pattern so an
    installed package still finds it."""
    return (
        importlib.resources.files("disco.agent_server")
        .joinpath("selection_agent.js")
        .read_text(encoding="utf-8")
    )


def make_preview_edit_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/conversations/{conversation_id}/preview-edit/{path:path}")
    async def preview_edit(conversation_id: str, path: str, request: Request) -> Response:
        """Serve a workspace HTML file stamped + selection-injected for edit mode.

        404 uniformly on any rejection (traversal, missing file, non-HTML, no
        runtime) — no probe. Relative sibling assets (css/js/img) requested by the
        stamped page resolve back through this route and are served with their
        natural media type, UNSTAMPED."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        norm = posixpath.normpath(path)
        # Jail: reject traversal / absolute paths (mirrors files.py + preview jails).
        if posixpath.isabs(norm) or norm.startswith("..") or norm == ".":
            raise HTTPException(status_code=404)
        if runtime is None:
            raise HTTPException(status_code=404)

        _, ext = posixpath.splitext(norm)
        ext = ext.lower()

        data = await _read_workspace_file_safe(store, runtime, conversation_id, norm)
        if data is None:
            raise HTTPException(status_code=404)
        if len(data) > 50 * 1024 * 1024:  # 50 MB cap (parity with files.py)
            raise HTTPException(status_code=404)

        # A relative sibling asset (css/js/img/font) referenced by the stamped page:
        # serve it as-is so relative refs resolve through this route (no inlining).
        if ext not in _HTML_EXTS:
            media = _SIBLING_MEDIA_TYPES.get(ext)
            if media is None:
                # Not HTML and not a known sibling asset → 404 (keep the surface tight).
                raise HTTPException(status_code=404)
            return Response(
                content=data,
                media_type=media,
                headers={
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "private, no-store",
                },
            )

        # HTML → stamp + inject the selection agent under a per-response nonce.
        try:
            html = data.decode("utf-8")
        except UnicodeDecodeError as exc:  # not real HTML text → 404
            raise HTTPException(status_code=404) from exc

        stamped = stamp_oids(html, norm)
        nonce = secrets.token_urlsafe(16)
        script_tag = f'<script nonce="{nonce}">\n{_load_selection_agent_script()}\n</script>'
        # Inject before </body> (case-insensitive); fall back to appending so a
        # body-less fragment still gets the agent.
        lowered = stamped.lower()
        idx = lowered.rfind("</body>")
        if idx != -1:
            injected = stamped[:idx] + script_tag + "\n" + stamped[idx:]
        else:
            injected = stamped + "\n" + script_tag

        # Tailored CSP: permit the nonce'd selection script (default-src 'none' would
        # block it — that is why this route exists separately from the files.py inline
        # path). Same-origin only otherwise; no external loads. We deliberately do NOT
        # set frame-ancestors: the app UI and the agent-server are SPLIT origins
        # (UI :8088 frames the agent :8000), so 'self' would make the browser reject
        # the edit iframe; the existing live-preview proxy likewise sets no
        # frame-ancestors. The iframe's own sandbox attr + default-src 'self' (no
        # script except the nonce'd agent) are the active containment here.
        csp = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            f"script-src 'nonce-{nonce}'"
        )
        return Response(
            content=injected,
            media_type="text/html",
            headers={
                "Content-Security-Policy": csp,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    return router
