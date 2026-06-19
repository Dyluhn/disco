"""A2 — in-app deck editor routes.

GET  /conversations/{cid}/deck/editor?path={base}
    Resolve the deck's `{base}.authored.json` (declared-artifact jailed), lower it
    to the editor's positional `LoweredDeck`, and return it as JSON. 404 when the
    deck carries no authored sidecar (predates A2.0 / is a Marp deck).

PUT  /conversations/{cid}/deck/editor?path={base}
    Apply an RFC-6902 JSON Patch to the authored JSON (schema-or-revert → 422 on a
    bad patch, workspace UNTOUCHED), re-render HTML + PPTX, and write all three
    back. Write-back is LIVE-SESSION-ONLY (runtime.live_session is read-only / no
    create): no live session → 409 {"reason": "no_live_sandbox"} with NO writes, so
    the editor shows a clear "re-open the build to edit" state (no false affordance).

`base` is the deck base name WITHOUT extension (the slides tool's `base_name`).
"""

from __future__ import annotations

import dataclasses
import json
import posixpath
from typing import Any

from disco.core import ObservationEvent
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..runtime import ConversationRuntime
from ._common import _declared_artifacts
from .files import _read_artifact_bytes


async def _sidecar_is_current(
    store: SqliteEventStore, conversation_id: str, base: str
) -> bool:
    """True iff the MOST RECENT ``slides_generate`` that produced this base still
    advertises the editable AuthoredDeck sidecar.

    A historical ``editable_source`` stays in the declared-artifact set forever, so a
    later same-base render to a NON-editable format (Marp/markdown fallback — it sets
    ``base_name`` but no ``editable_source``) would otherwise leave the stale sidecar
    reachable. Editing it then overwrites the newer deck (data loss) behind a false
    "Edit Slides" affordance. We therefore require the LATEST render of ``base`` to be
    the editable one."""
    authored = f"{base}.authored.json"
    matched = False
    latest_editable = False
    for e in await store.get_events(conversation_id):
        if (
            isinstance(e, ObservationEvent)
            and e.tool_result.success
            and e.tool_result.tool_name == "slides_generate"
            and e.tool_result.structured
            and e.tool_result.structured.get("base_name") == base
        ):
            matched = True
            latest_editable = e.tool_result.structured.get("editable_source") == authored
    return matched and latest_editable


def _jail_base(base: str) -> str:
    """Reject traversal / absolute paths in the deck base name, mirroring files.py.
    Returns the normalized base. Raises 404 on any escape attempt. The base must be
    a plain name (no `/`, no `..`) — the authored sidecar lives at the workspace
    root next to the rendered deck."""
    norm = posixpath.normpath(base)
    if posixpath.isabs(norm) or norm.startswith("..") or "/" in norm:
        raise HTTPException(status_code=404)
    return norm


class DeckPatchBody(BaseModel):
    patch: list[dict[str, Any]]


def make_deck_editor_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/conversations/{conversation_id}/deck/editor")
    async def get_deck_for_editor(
        conversation_id: str,
        path: str = Query(..., description="Deck base name (no extension)."),
    ) -> dict[str, Any]:
        """Return the LoweredDeck for `{path}.authored.json` (editor geometry)."""
        from disco.tools.builtin._deck_schema import AuthoredDeck, lower_deck_for_editor

        base = _jail_base(path)
        authored_rel = f"{base}.authored.json"
        if runtime is None:
            raise HTTPException(status_code=404)
        if authored_rel not in await _declared_artifacts(store, conversation_id):
            raise HTTPException(status_code=404)
        # The sidecar must be the CURRENT render of this base — not superseded by a
        # later non-editable (Marp/fallback) regeneration. Else editing it would
        # overwrite the newer deck behind a false affordance.
        if not await _sidecar_is_current(store, conversation_id, base):
            raise HTTPException(status_code=404)

        data = await _read_artifact_bytes(runtime, conversation_id, authored_rel)
        if data is None:
            raise HTTPException(status_code=404)
        try:
            authored = AuthoredDeck.model_validate(json.loads(data))
        except Exception as exc:  # noqa: BLE001 — malformed sidecar → 404 (not editable)
            raise HTTPException(status_code=404) from exc

        lowered = lower_deck_for_editor(authored)
        return dataclasses.asdict(lowered)

    @router.put("/conversations/{conversation_id}/deck/editor")
    async def patch_deck(
        conversation_id: str,
        body: DeckPatchBody,
        path: str = Query(..., description="Deck base name (no extension)."),
    ) -> dict[str, Any]:
        """Apply the patch, re-render, and write authored.json + html + pptx back.

        Mirrors DeckPatchTool.run: read → apply_patch → validate (422 on failure,
        workspace untouched) → lower_deck → render. Write-back requires a live
        sandbox session (409 otherwise — no writes)."""
        from disco.tools.builtin._deck_patch import PatchError, apply_patch
        from disco.tools.builtin._deck_schema import (
            AuthoredDeck,
            lower_deck,
            lower_deck_for_editor,
        )
        from disco.tools.builtin._pptx_render import render_html, render_pptx

        base = _jail_base(path)
        authored_rel = f"{base}.authored.json"
        html_rel = f"{base}.html"
        pptx_rel = f"{base}.pptx"
        if runtime is None:
            raise HTTPException(status_code=404)

        declared = await _declared_artifacts(store, conversation_id)
        if authored_rel not in declared:
            raise HTTPException(status_code=404)
        # Refuse to patch a sidecar superseded by a later non-editable render of the
        # same base — saving would overwrite the newer deck (data loss).
        if not await _sidecar_is_current(store, conversation_id, base):
            raise HTTPException(status_code=404)

        # Read the current authored JSON (live session → host snapshot fallback).
        raw = await _read_artifact_bytes(runtime, conversation_id, authored_rel)
        if raw is None:
            raise HTTPException(status_code=404)
        try:
            authored_json = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=404) from exc

        # Apply + validate (schema-or-revert). A bad patch / invalid result → 422
        # and the workspace is NEVER touched (no writes happen below).
        try:
            patched_json = apply_patch(authored_json, body.patch)
        except (PatchError, KeyError, IndexError, ValueError, TypeError) as exc:
            # apply_patch's move/copy paths can raise raw KeyError/IndexError/ValueError
            # on a missing/invalid `from` pointer — all are rejected patches, so they
            # must surface as a clean 422 (never a 500). No writes have happened.
            raise HTTPException(
                status_code=422,
                detail={"reason": "patch_failed", "message": str(exc)},
            ) from exc
        try:
            authored_deck = AuthoredDeck.model_validate(patched_json)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail={"reason": "schema_invalid", "message": str(exc)},
            ) from exc

        # Re-render deterministically via the C1 path.
        try:
            deck = lower_deck(authored_deck)
            html_str = render_html(deck)
            pptx_bytes = render_pptx(deck)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail={"reason": "render_failed", "message": str(exc)},
            ) from exc

        # Write-back is LIVE-SESSION-ONLY: a finished+reaped build (no live session)
        # cannot be edited. 409 with a clear reason; NO writes occur.
        session = runtime.live_session(conversation_id)
        if session is None:
            raise HTTPException(status_code=409, detail={"reason": "no_live_sandbox"})

        # Atomic-ish write-back: the route's contract is all-or-nothing — never a
        # partially-updated workspace where the authored JSON and its renders disagree.
        # Capture each target's prior bytes, write all three, and on ANY mid-sequence
        # failure roll the already-written files back to their prior content.
        targets: list[tuple[str, bytes]] = [
            (authored_rel, json.dumps(patched_json, indent=2).encode()),
            (html_rel, html_str.encode()),
            (pptx_rel, pptx_bytes),
        ]
        prior: dict[str, bytes | None] = {
            rel: await _read_artifact_bytes(runtime, conversation_id, rel)
            for rel, _ in targets
        }
        try:
            for rel, data in targets:
                await session.write_file(rel, data)
        except Exception as exc:  # noqa: BLE001 — roll back, then surface a clean 500
            # Restore EVERY pre-existing target — including the one whose write just
            # failed (it may be truncated/partial) — to its prior content. A target
            # that did not pre-exist cannot be un-created (no delete primitive), but in
            # the deck-edit flow all three always pre-exist (the deck was generated
            # before it can be edited), so the workspace is restored intact.
            for rel, _ in targets:
                restore = prior[rel]
                if restore is not None:
                    try:
                        await session.write_file(rel, restore)
                    except Exception:  # noqa: BLE001 — best-effort restore
                        pass
            raise HTTPException(
                status_code=500,
                detail={"reason": "write_failed", "message": str(exc)},
            ) from exc

        # PDF re-render needs a ToolContext (convert_to_pdf(ctx, ...)) which the route
        # layer has no clean way to construct, so a {base}.pdf is left STALE rather than
        # silently desynced — surfaced honestly to the caller. Detect it by ACTUAL
        # existence: the PDF is never in `declared` (the executor drops ToolOutcome.
        # artifacts; _declared_artifacts only sees structured), so a `pdf_rel in declared`
        # check would always be False even when a stale PDF exists.
        pdf_rel = f"{base}.pdf"
        pdf_stale = (await _read_artifact_bytes(runtime, conversation_id, pdf_rel)) is not None

        lowered = lower_deck_for_editor(authored_deck)
        return {
            "ok": True,
            "lowered": dataclasses.asdict(lowered),
            "html_file": html_rel,
            "pptx_file": pptx_rel,
            "pdf_stale": pdf_stale,
        }

    return router
