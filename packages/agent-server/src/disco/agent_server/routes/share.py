"""Share-link routes + the read-only static viewer (RP-06)."""

from __future__ import annotations

from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..auth import current_owner_id
from ..runtime import ConversationRuntime
from ..share_viewer import _SHARE_VIEWER_HTML
from ._common import require_owned_conversation


async def _create_share_response(
    runtime: ConversationRuntime, conversation_id: str, owner_id: str
) -> dict:
    result = await runtime.create_share_link_async(
        conversation_id,
        owner_id=owner_id,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "reason": result.get("reason", "unknown")},
        )
    return {
        "ok": True,
        "token": result["token"],
        "url": f"/share/{result['token']}",
        "conversation_id": conversation_id,
        "bundle_seq": result["bundle_seq"],
    }


def _revoke_share_response(runtime: ConversationRuntime, token: str, owner_id: str) -> dict:
    ok = runtime.revoke_share_link(
        token,
        owner_id=owner_id,
    )
    return {"ok": ok, "token": token, "revoked": ok}


async def _export_share_bundle_response(
    runtime: ConversationRuntime, conversation_id: str, owner_id: str
) -> dict:
    result = await runtime.share_export(conversation_id, owner_id=owner_id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "reason": result.get("reason", "unknown")},
        )
    return result["bundle"]


async def _import_share_bundle_response(
    runtime: ConversationRuntime, bundle: dict, owner_id: str
) -> dict:
    result = await runtime.share_import(
        bundle,
        owner_id=owner_id,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "reason": result.get("reason", "invalid_bundle")},
        )
    return result


async def _share_bundle_response(runtime: ConversationRuntime, token: str) -> dict:
    row = runtime.lookup_share_link(token)
    if row is None:
        # 404 with NO distinguishing detail — revoked and never-issued
        # tokens are intentionally conflated. A probe that varies the
        # token string sees 404 every time.
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "not_found"})
    result = await runtime.share_export(
        row["conversation_id"],
        owner_id=row["owner_id"],
        before_seq=row["bundle_seq"],
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "reason": result.get("reason", "unknown")},
        )
    # Surface the bundle_seq the share_tokens row recorded at issue
    # time so the UI can show "this link is N events behind the
    # current run" if the conversation has progressed since then.
    bundle = result["bundle"]
    # Title/surface live in the conversations table rather than the event
    # log, so the token row captures them separately at issue time.
    bundle["title"] = row["bundle_title"]
    bundle["surface"] = row["bundle_surface"]
    bundle["share"] = {
        "token": token,
        "owner_id": row["owner_id"],
        "created_at": row["created_at"],
        "bundle_seq_at_issue": row["bundle_seq"],
    }
    return bundle


def make_share_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.post("/api/conversations/{conversation_id}/share")
    async def create_share(conversation_id: str, request: Request) -> dict:
        """Create a revocable share link for a conversation.

        Returns 200 with the token (and its public URL) on success, 404 when
        the conversation does not exist for the default owner. The token is
        base62 (~22 chars / 131 bits) and lives in the `share_tokens` table;
        revocation flips `revoked_at` (a 410 Gone is a probe, so revoked
        tokens look identical to never-issued ones to the viewer)."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _create_share_response(runtime, conversation_id, current_owner_id(request))

    @router.get("/api/share")
    async def list_share_links(request: Request) -> dict:
        """List active, owner-scoped share links."""
        if runtime is None:
            return {"links": []}
        return {"links": runtime.list_share_links(owner_id=current_owner_id(request))}

    @router.delete("/api/share/{token}")
    async def revoke_share(token: str, request: Request) -> dict:
        """Revoke a share link. Returns 200 with `revoked: true/false` (the
        `false` case = token didn't exist, was already revoked, or is not
        owned by the caller). Owner-scoped: a caller can only revoke a
        token it issued (the WHERE clause filters by owner_id)."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        return _revoke_share_response(runtime, token, current_owner_id(request))

    @router.get("/api/conversations/{conversation_id}/share/bundle")
    async def export_share_bundle(conversation_id: str, request: Request) -> dict:
        """Build the scrubbed JSON bundle for a conversation without
        issuing a share link. Useful for direct export (download the
        bundle) and for the reviewer's standalone-rung check. The bundle
        IS the share-viewer payload: same shape, same scrubbing."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _export_share_bundle_response(
            runtime, conversation_id, current_owner_id(request)
        )

    @router.post("/api/share/import")
    async def import_share_bundle(bundle: dict, request: Request) -> dict:
        """Import an exported bundle as a READ-ONLY local conversation. Untrusted
        input → fail-closed validation + re-scrub on ingest + an importer-minted cid
        + an `origin="imported"` marker the read-only guard keys on. 422 (typed
        reason) on any validation failure — never a partial import."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        return await _import_share_bundle_response(runtime, bundle, current_owner_id(request))

    @router.get("/share/{token}")
    async def share_viewer(token: str) -> HTMLResponse:
        """Serve the self-contained, read-only viewer without token-state disclosure."""
        # Confirmed-revoked vs never-issued are 404 in both cases: the
        # static viewer cannot tell the difference and neither can a
        # probe. The HTML page itself is the same either way.
        return HTMLResponse(_SHARE_VIEWER_HTML)

    @router.get("/api/share/{token}/bundle")
    async def share_bundle(token: str) -> dict:
        """Fetch the scrubbed bundle for a valid share token. The endpoint
        rebuilds the bundle on every call from the token's captured sequence
        boundary. Appends made after link creation are never included. 404 for
        missing or revoked tokens."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        return await _share_bundle_response(runtime, token)

    return router
