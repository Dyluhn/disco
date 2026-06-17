"""Report export + audio-overview routes (RP-07 / C2)."""

from __future__ import annotations

import contextlib
import posixpath

from disco.core import DEFAULT_OWNER_ID, ReportEvent
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Query, Response

from ..report_audio import (
    TtsBackendError,
    TtsDisabled,
    TurnScriptError,
    generate_report_audio,
    report_audio_cache_dir,
)
from ..runtime import ConversationRuntime
from ._common import _AUDIO_MEDIA_TYPES


def make_report_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/conversations/{conversation_id}/report/export")
    async def export_report(conversation_id: str, fmt: str = Query(...)) -> Response:
        """Export the latest Deep Research report as MD, PDF, or DOCX.

        Query param `fmt` must be md, pdf, or docx.
        Returns 200 with the file streamed (Content-Type + Content-Disposition).
        Returns 404 when no ReportEvent exists for this conversation.
        Returns 400 for an unknown format."""
        if runtime is None:
            raise HTTPException(
                status_code=503, detail={"ok": False, "reason": "no_runtime"}
            )

        valid_fmts = frozenset({"md", "pdf", "docx"})
        if fmt not in valid_fmts:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown export format: {fmt!r}. Valid: md, pdf, docx",
            )

        try:
            result = await runtime.export_report(
                conversation_id, fmt, owner_id=DEFAULT_OWNER_ID
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result is None:
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": "no_report"},
            )

        payload, media_type, ext = result

        # Build a safe filename from the conversation id
        safe_cid = conversation_id.replace("/", "-").replace("..", "-")
        filename = f"report-{safe_cid}{ext}"

        headers: dict[str, str] = {
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if media_type:
            headers["Content-Type"] = media_type

        return Response(
            content=payload,
            status_code=200,
            media_type=media_type,
            headers=headers,
        )

    @router.post("/conversations/{conversation_id}/report/audio")
    async def report_audio(
        conversation_id: str,
        mode: str = Query("podcast"),
    ) -> dict:
        """Generate (or return the cached) audio overview for the latest Deep
        Research report on this conversation.  Runs the audio pipeline in-process
        (no sandbox), caching mp3 + transcript per conversation.

        ``mode`` query param: ``podcast`` (default — two-host dialogue) or
        ``single`` (single-voice honest walkthrough).  The two modes write
        separate cache files so re-pressing with a different mode generates a
        fresh artifact instead of colliding (WALK-21 / D3).

        404 → no ReportEvent; 400 → unknown mode; 503 → TTS disabled in
        Settings; 502 → synth/LLM failure. 200 →
        ``{"ok": True, "mp3_url": ..., "transcript_url": ...}``."""
        if mode not in ("podcast", "single"):
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "reason": "invalid_mode", "detail": f"Unknown mode {mode!r}"},
            )

        report: ReportEvent | None = None
        with contextlib.suppress(Exception):
            for e in reversed(await store.get_events(conversation_id)):
                if isinstance(e, ReportEvent):
                    report = e
                    break
        if report is None:
            raise HTTPException(
                status_code=404, detail={"ok": False, "reason": "no_report"}
            )

        from disco.core.llm import ConfigStore

        tts = ConfigStore().load().tts
        out_dir = report_audio_cache_dir() / conversation_id
        try:
            mp3_path, transcript_path = await generate_report_audio(
                report,
                tts_settings=tts,
                out_dir=out_dir,
                mode=mode,
                # Prefer an encrypted-store key for the remote TTS provider; falls
                # back to the env var by name when the runtime/store isn't wired.
                resolve_key=runtime._resolve_secret if runtime is not None else None,
            )
        except TtsDisabled:
            raise HTTPException(
                status_code=503, detail={"ok": False, "reason": "tts_disabled"}
            ) from None
        except (TtsBackendError, TurnScriptError) as exc:
            raise HTTPException(
                status_code=502,
                detail={"ok": False, "reason": "tts_backend", "detail": str(exc)},
            ) from exc

        return {
            "ok": True,
            "mp3_url": f"/conversations/{conversation_id}/report/audio/{mp3_path.name}",
            "transcript_url": (
                f"/conversations/{conversation_id}/report/audio/{transcript_path.name}"
            ),
        }

    @router.get("/conversations/{conversation_id}/report/audio/{name}")
    async def report_audio_file(conversation_id: str, name: str) -> Response:
        """Serve a cached audio-overview file (mp3 / transcript) as an attachment.
        Jailed: canonical basename only + resolve-jail to this conversation's cache."""
        if "/" in name or ".." in name:
            raise HTTPException(status_code=404)
        _, ext = posixpath.splitext(name)
        media_type = _AUDIO_MEDIA_TYPES.get(ext.lower())
        if media_type is None:
            raise HTTPException(status_code=404)
        cid_cache = (report_audio_cache_dir() / conversation_id).resolve()
        fpath = (cid_cache / name).resolve()
        if not fpath.is_relative_to(cid_cache) or not fpath.is_file():
            raise HTTPException(status_code=404)
        return Response(
            content=fpath.read_bytes(),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    return router
