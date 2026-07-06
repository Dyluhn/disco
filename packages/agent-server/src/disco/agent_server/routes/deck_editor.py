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
from disco.core.brand.tokens import Theme
from disco.core.design import direction_from_markdown, to_brand_tokens
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..runtime import ConversationRuntime
from ..auth import current_owner_id
from ._common import _declared_artifacts, require_owned_conversation
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


async def _reload_deck_image_assets(
    runtime: ConversationRuntime, conversation_id: str, base: str, authored: Any
) -> dict[int, bytes]:
    """Reload the generated per-slide images so a re-render (template export / UI edit)
    re-embeds them instead of dropping image slides back to the [image] placeholder.

    C7 image bytes live on the rendered Element, not in the authored sidecar, so any
    route that re-lowers from the sidecar must reload them. They persist on disk as
    "{base}_img_{i}.png" (the convention _stage_assets wrote), keyed by authored-slide
    index. Missing/invalid assets are skipped → that slide renders the placeholder."""
    assets: dict[int, bytes] = {}
    for i, slide in enumerate(getattr(authored, "slides", []) or []):
        if getattr(slide, "image_prompt", None) is None:
            continue
        data = await _read_artifact_bytes(runtime, conversation_id, f"{base}_img_{i}.png")
        if data and (data.startswith(b"\x89PNG\r\n\x1a\n") or data[:3] == b"\xff\xd8\xff"):
            assets[i] = data
    return assets


async def _direction_brand_override(
    runtime: ConversationRuntime, conversation_id: str
) -> Theme | None:
    raw = await _read_artifact_bytes(
        runtime, conversation_id, ".disco/context/design_direction.md"
    )
    if raw is None:
        return None
    direction = direction_from_markdown(raw.decode("utf-8", errors="replace"))
    return to_brand_tokens(direction) if direction is not None else None


# [W-22] Deck PDF export renders the .pptx → .pdf with LibreOffice, which ships ONLY
# in the sandbox CONTAINER image (pmx-sandbox: gvisor / local / podman) — NOT on the
# host. The `process` (dev) backend runs tools directly on the host with no such image,
# so PDF would have nothing to convert with. These are the backends that can produce a
# deck PDF; everything else → 409 + the PDF affordance is hidden (no false affordance).
_PDF_CAPABLE_BACKENDS: frozenset[str] = frozenset({"gvisor", "local", "podman"})


def _deck_pdf_capable(runtime: ConversationRuntime) -> bool:
    """True iff the active sandbox backend is a container that COULD ship LibreOffice.

    This is the cheap BACKEND-TYPE pre-gate only — it does NOT prove LibreOffice is
    actually installed in the deployed image (a stale image predating the
    libreoffice-impress layer passes this but cannot convert). The real availability
    check is `_deck_pdf_available`, which probes ``command -v soffice`` in the box."""
    name = runtime.sandbox_backend_name()
    return name in _PDF_CAPABLE_BACKENDS


class _SofficeUnavailable(RuntimeError):
    """Raised when the sandbox image has no ``soffice`` (LibreOffice) on PATH.

    BW-10: a container backend whose deployed image predates the libreoffice-impress
    layer passes the backend-type gate but cannot actually convert. We surface this as
    an HONEST, specific message ("PDF unavailable in this sandbox image") instead of a
    generic render failure — and the OPS fix is to rebuild/redeploy the image."""


async def _probe_soffice_in_sandbox(
    runtime: ConversationRuntime, *, owner_id: str
) -> bool:
    """Spin a THROWAWAY sandbox and probe ``command -v soffice`` — the HONEST capability
    check for deck→PDF export (BW-10). Returns True iff LibreOffice is on PATH in the
    deployed image. Any spin/probe error → False (treated as unavailable, never a false
    affordance). Mirrors the probe pattern in _pptx_render.convert_to_pdf."""
    import uuid as _uuid

    try:
        svc = runtime._sandbox_service_now()
    except Exception:  # noqa: BLE001 — no sandbox service → not available
        return False
    cid = f"probe-soffice-{_uuid.uuid4().hex[:12]}"
    try:
        instance = await svc.create(
            runtime._sandbox_spec, owner_id=owner_id, conversation_id=cid
        )
    except Exception:  # noqa: BLE001 — cannot spin a box → not available
        return False
    try:
        probe = await instance.exec_shell("command -v soffice", timeout_s=10)
        return getattr(probe, "exit_code", 1) == 0
    except Exception:  # noqa: BLE001 — probe error → not available
        return False
    finally:
        try:
            await instance.destroy()
        except Exception:  # noqa: BLE001 — teardown best-effort
            pass


async def _deck_pdf_available(
    runtime: ConversationRuntime, *, owner_id: str
) -> tuple[bool, str | None]:
    """Honest deck→PDF capability: backend-type gate AND a real ``soffice`` probe.

    Returns ``(available, reason)`` where ``reason`` is a stable machine code
    (``"no_container_backend"`` | ``"soffice_missing"``) when unavailable, else None.
    Used by the FE capabilities endpoint so the PDF button is hidden when the image
    truly cannot convert — not merely when the backend type is wrong (BW-10)."""
    if not _deck_pdf_capable(runtime):
        return False, "no_container_backend"
    if not await _probe_soffice_in_sandbox(runtime, owner_id=owner_id):
        return False, "soffice_missing"
    return True, None


async def _render_deck_pdf_in_sandbox(
    runtime: ConversationRuntime, pptx_bytes: bytes, *, owner_id: str
) -> bytes:
    """Spin a THROWAWAY sandbox, write the rendered .pptx in-box, convert it to .pdf
    with headless LibreOffice (`soffice`), read the bytes back, and tear the box down.

    Mirrors the transient-sandbox pattern of the (now-removed) DR `_render_docx_in_sandbox`:
    the deck export isn't tied to a live conversation sandbox, so it gets its own
    short-lived one. Uses ONLY the existing sandbox compose/exec API
    (`create`/`write_file`/`exec_shell`/`read_file`/`destroy`). Raises RuntimeError on a
    converter failure/timeout; the caller maps it to a 422 render_failed."""
    import uuid as _uuid

    svc = runtime._sandbox_service_now()
    cid = f"export-deck-pdf-{_uuid.uuid4().hex[:12]}"
    instance = await svc.create(runtime._sandbox_spec, owner_id=owner_id, conversation_id=cid)
    try:
        # BW-10: probe FIRST so a stale image (no LibreOffice) yields an HONEST,
        # specific failure instead of a generic soffice-not-found render error.
        probe = await instance.exec_shell("command -v soffice", timeout_s=10)
        if getattr(probe, "exit_code", 1) != 0:
            raise _SofficeUnavailable(
                "PDF unavailable in this sandbox image — LibreOffice (soffice) is not "
                "installed. Rebuild/redeploy the sandbox image (deploy/sandbox/Dockerfile "
                "ships libreoffice-impress) to enable deck PDF export."
            )
        await instance.write_file("_deck.pptx", pptx_bytes)
        # soffice --convert-to pdf writes "<stem>.pdf" into --outdir; "." = the jailed
        # workspace root where we wrote the input.
        res = await instance.exec_shell(
            "soffice --headless --convert-to pdf --outdir . _deck.pptx", timeout_s=120
        )
        if getattr(res, "timed_out", False):
            raise RuntimeError("soffice timed out after 120s")
        if res.exit_code != 0:
            detail = (res.stderr or "").strip() or f"exit code {res.exit_code}"
            raise RuntimeError(f"soffice failed in the sandbox: {detail}")
        pdf = await instance.read_file("_deck.pdf")
        if not pdf.startswith(b"%PDF"):
            raise RuntimeError("soffice produced no valid PDF (missing %PDF header)")
        return pdf
    finally:
        try:
            await instance.destroy()
        except Exception:  # noqa: BLE001 — teardown best-effort
            pass


class DeckPatchBody(BaseModel):
    patch: list[dict[str, Any]]


def _coerce_patch_ops(patch: object) -> "list[dict[str, Any]]":
    """apply_patch's engine consumes plain dicts; the route body may carry
    typed JsonPatchOperation models (advertised-schema typing) or raw dicts."""
    out: list[dict[str, Any]] = []
    items: list[object] = list(patch) if isinstance(patch, list) else []
    for op in items:
        if isinstance(op, dict):
            out.append(op)
            continue
        dump = getattr(op, "model_dump", None)
        if callable(dump):
            dumped = dump(exclude_none=True)
            if isinstance(dumped, dict):
                out.append(dumped)
    return out


async def _editable_sidecar(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    path: str,
) -> tuple[str, str, ConversationRuntime]:
    base = _jail_base(path)
    authored_rel = f"{base}.authored.json"
    if runtime is None:
        raise HTTPException(status_code=404)
    if authored_rel not in await _declared_artifacts(store, conversation_id):
        raise HTTPException(status_code=404)
    if not await _sidecar_is_current(store, conversation_id, base):
        raise HTTPException(status_code=404)
    return base, authored_rel, runtime


async def _read_authored_json(
    runtime: ConversationRuntime, conversation_id: str, authored_rel: str
) -> Any:
    raw = await _read_artifact_bytes(runtime, conversation_id, authored_rel)
    if raw is None:
        raise HTTPException(status_code=404)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=404) from exc


async def _read_authored_deck(
    runtime: ConversationRuntime, conversation_id: str, authored_rel: str
) -> Any:
    from disco.tools.builtin._deck_schema import AuthoredDeck

    authored_json = await _read_authored_json(runtime, conversation_id, authored_rel)
    try:
        return AuthoredDeck.model_validate(authored_json)
    except Exception as exc:  # noqa: BLE001 — malformed sidecar → 404 (not editable)
        raise HTTPException(status_code=404) from exc


async def _lower_deck_with_direction_brand(
    runtime: ConversationRuntime,
    conversation_id: str,
    base: str,
    authored: Any,
    *,
    template: str | None,
    brand_enabled: bool,
) -> Any:
    from disco.tools.builtin._deck_schema import lower_deck

    image_assets = await _reload_deck_image_assets(runtime, conversation_id, base, authored)
    brand = await _direction_brand_override(runtime, conversation_id)
    brand_override = brand if brand_enabled else None
    if template is None:
        return lower_deck(
            authored,
            brand_override=brand_override,
            image_assets=image_assets,
        )
    return lower_deck(
        authored,
        theme_override=template,
        brand_override=brand_override,
        image_assets=image_assets,
    )


async def _get_deck_for_editor_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    path: str,
) -> dict[str, Any]:
    from disco.tools.builtin._deck_schema import lower_deck_for_editor

    _base, authored_rel, live_runtime = await _editable_sidecar(
        store, runtime, conversation_id, path
    )
    authored = await _read_authored_deck(live_runtime, conversation_id, authored_rel)
    lowered = lower_deck_for_editor(authored)
    return dataclasses.asdict(lowered)


async def _get_deck_render_inline_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    path: str,
    template: str | None,
) -> Any:
    from disco.core.brand import is_valid_template
    from disco.tools.builtin._pptx_render import render_html
    from fastapi import Response

    base, authored_rel, live_runtime = await _editable_sidecar(
        store, runtime, conversation_id, path
    )
    if template is not None and not is_valid_template(template):
        raise HTTPException(status_code=400, detail=f"Unknown template {template!r}")
    authored = await _read_authored_deck(live_runtime, conversation_id, authored_rel)
    brand_enabled = template is None or template == "disco-light"
    try:
        deck = await _lower_deck_with_direction_brand(
            live_runtime,
            conversation_id,
            base,
            authored,
            template=template,
            brand_enabled=brand_enabled,
        )
        html_str = render_html(deck)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail={"reason": "render_failed", "message": str(exc)}
        ) from exc

    return Response(
        content=html_str.encode("utf-8"),
        media_type="text/html; charset=utf-8",
    )


async def _export_deck_with_template_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    path: str,
    template: str,
    fmt: str,
    owner_id: str,
) -> Any:
    from disco.core.brand import is_valid_template
    from disco.tools.builtin._pptx_render import render_html, render_pptx
    from fastapi import Response

    base, authored_rel, live_runtime = await _editable_sidecar(
        store, runtime, conversation_id, path
    )
    if not is_valid_template(template):
        raise HTTPException(status_code=400, detail=f"Unknown template {template!r}")
    if fmt not in ("pptx", "html", "pdf"):
        raise HTTPException(status_code=400, detail="fmt must be 'pptx', 'html', or 'pdf'")
    if fmt == "pdf" and not _deck_pdf_capable(live_runtime):
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "no_container_backend",
                "message": (
                    "Deck PDF export needs a container sandbox backend "
                    "(gVisor / local / podman) — LibreOffice ships in the sandbox "
                    "image, not on the host. The active backend is "
                    f"{live_runtime.sandbox_backend_name() or 'none'}."
                ),
            },
        )

    authored = await _read_authored_deck(live_runtime, conversation_id, authored_rel)
    try:
        deck = await _lower_deck_with_direction_brand(
            live_runtime,
            conversation_id,
            base,
            authored,
            template=template,
            brand_enabled=template == "disco-light",
        )
        if fmt == "html":
            body: bytes = render_html(deck).encode("utf-8")
            media = "text/html; charset=utf-8"
            ext = "html"
        elif fmt == "pdf":
            pptx_bytes = render_pptx(deck)
            body = await _render_deck_pdf_in_sandbox(
                live_runtime, pptx_bytes, owner_id=owner_id
            )
            media = "application/pdf"
            ext = "pdf"
        else:
            body = render_pptx(deck)
            media = (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
            ext = "pptx"
    except HTTPException:
        raise
    except _SofficeUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "pdf_unavailable", "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail={"reason": "render_failed", "message": str(exc)}
        ) from exc

    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{base}.{ext}"'},
    )


async def _deck_export_capabilities_response(
    runtime: ConversationRuntime | None,
    *,
    owner_id: str,
) -> dict[str, Any]:
    if runtime is None:
        return {"pptx": True, "html": True, "pdf": False, "pdf_reason": "no_runtime"}
    available, reason = await _deck_pdf_available(runtime, owner_id=owner_id)
    return {"pptx": True, "html": True, "pdf": available, "pdf_reason": reason}


async def _patch_deck_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    body: DeckPatchBody,
    path: str,
) -> dict[str, Any]:
    from disco.tools.builtin._deck_patch import PatchError, apply_patch
    from disco.tools.builtin._deck_schema import AuthoredDeck, lower_deck_for_editor
    from disco.tools.builtin._pptx_render import render_html, render_pptx

    base, authored_rel, live_runtime = await _editable_sidecar(
        store, runtime, conversation_id, path
    )
    html_rel = f"{base}.html"
    pptx_rel = f"{base}.pptx"
    authored_json = await _read_authored_json(live_runtime, conversation_id, authored_rel)

    try:
        patched_json = apply_patch(
            authored_json,
            list(_coerce_patch_ops(body.patch)),
        )
    except (PatchError, KeyError, IndexError, ValueError, TypeError) as exc:
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

    try:
        deck = await _lower_deck_with_direction_brand(
            live_runtime,
            conversation_id,
            base,
            authored_deck,
            template=None,
            brand_enabled=True,
        )
        html_str = render_html(deck)
        pptx_bytes = render_pptx(deck)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail={"reason": "render_failed", "message": str(exc)}
        ) from exc

    session = live_runtime.live_session(conversation_id)
    if session is None:
        raise HTTPException(status_code=409, detail={"reason": "no_live_sandbox"})

    targets: list[tuple[str, bytes]] = [
        (authored_rel, json.dumps(patched_json, indent=2).encode()),
        (html_rel, html_str.encode()),
        (pptx_rel, pptx_bytes),
    ]
    prior: dict[str, bytes | None] = {
        rel: await _read_artifact_bytes(live_runtime, conversation_id, rel)
        for rel, _ in targets
    }
    try:
        for rel, data in targets:
            await session.write_file(rel, data)
    except Exception as exc:  # noqa: BLE001 — roll back, then surface a clean 500
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

    pdf_rel = f"{base}.pdf"
    pdf_stale = (
        await _read_artifact_bytes(live_runtime, conversation_id, pdf_rel)
    ) is not None
    lowered = lower_deck_for_editor(authored_deck)
    return {
        "ok": True,
        "lowered": dataclasses.asdict(lowered),
        "html_file": html_rel,
        "pptx_file": pptx_rel,
        "pdf_stale": pdf_stale,
    }


def make_deck_editor_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/conversations/{conversation_id}/deck/editor")
    async def get_deck_for_editor(
        conversation_id: str,
        request: Request,
        path: str = Query(..., description="Deck base name (no extension)."),
    ) -> dict[str, Any]:
        """Return the LoweredDeck for `{path}.authored.json` (editor geometry)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _get_deck_for_editor_response(store, runtime, conversation_id, path)

    @router.get("/conversations/{conversation_id}/deck/editor/render")
    async def get_deck_render_inline(
        conversation_id: str,
        request: Request,
        path: str = Query(..., description="Deck base name (no extension)."),
        template: str | None = Query(None, description="Template id, e.g. 'disco-light'."),
    ) -> Any:
        """Return the INLINE HTML render of the deck for the WYSIWYG editor iframe substrate.

        Identical render path to GET /deck/export?fmt=html but WITHOUT
        ``Content-Disposition: attachment``, so the frontend can ``fetch()`` it and
        inject it as an iframe ``srcDoc``. The live render reads the stored authored
        sidecar + optional theme override — same jailing and staleness guards as the
        export and PUT routes. Template defaults to the deck's own theme when omitted."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _get_deck_render_inline_response(
            store, runtime, conversation_id, path, template
        )

    @router.get("/conversations/{conversation_id}/deck/export")
    async def export_deck_with_template(
        conversation_id: str,
        request: Request,
        path: str = Query(..., description="Deck base name (no extension)."),
        template: str = Query("disco-light", description="Template id, '{name}-{mode}'."),
        fmt: str = Query("pptx", description="'pptx', 'html', or 'pdf'."),
    ) -> Any:
        """Render the deck with a chosen TEMPLATE and return it as a download.

        Pure render-on-demand from the stored ``{base}.authored.json`` (live session
        OR host snapshot via ``_read_artifact_bytes``) — re-themed via the lower_deck
        ``theme_override`` WITHOUT mutating the sidecar. Needs no live session, so the
        slide-deck template selector works on finished decks too. 400 on an unknown
        template / fmt.

        ``pptx`` and ``html`` render in-process. ``pdf`` (W-22) renders the .pptx first
        (the SAME ``render_pptx`` path pptx export uses) then converts it to PDF with
        headless LibreOffice inside a THROWAWAY sandbox — so it requires a container
        backend (the sandbox image ships LibreOffice; the host self-hoster may not). On
        the ``process`` (no-container) backend the route returns 409
        {"reason": "no_container_backend"} and the UI hides the PDF button (no false
        affordance)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _export_deck_with_template_response(
            store,
            runtime,
            conversation_id,
            path,
            template,
            fmt,
            owner_id=current_owner_id(request),
        )

    @router.get("/conversations/{conversation_id}/deck/export/capabilities")
    async def deck_export_capabilities(
        conversation_id: str, request: Request
    ) -> dict[str, Any]:
        """Report which export formats this conversation's sandbox can ACTUALLY produce.

        BW-10: deck→PDF needs LibreOffice. Rather than the FE guessing from the backend
        TYPE alone (a stale image passes that but cannot convert), this probes
        ``command -v soffice`` in a throwaway box so the PDF button is hidden HONESTLY
        when the deployed image lacks it. ``pptx``/``html`` render in-process → always
        available."""
        await require_owned_conversation(request, store, conversation_id)
        return await _deck_export_capabilities_response(
            runtime,
            owner_id=current_owner_id(request),
        )

    @router.put("/conversations/{conversation_id}/deck/editor")
    async def patch_deck(
        conversation_id: str,
        body: DeckPatchBody,
        request: Request,
        path: str = Query(..., description="Deck base name (no extension)."),
    ) -> dict[str, Any]:
        """Apply the patch, re-render, and write authored.json + html + pptx back.

        Mirrors DeckPatchTool.run: read → apply_patch → validate (422 on failure,
        workspace untouched) → lower_deck → render. Write-back requires a live
        sandbox session (409 otherwise — no writes)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _patch_deck_response(store, runtime, conversation_id, body, path)

    return router
