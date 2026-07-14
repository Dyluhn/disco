"""Report export + audio-overview routes (RP-07 / C2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import posixpath
from pathlib import Path
from typing import Annotated, Any

from disco.core import MessageEvent, ReportEvent
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..report_audio import (
    TtsBackendError,
    TtsDisabled,
    TurnScriptError,
    generate_report_audio,
    report_audio_cache_dir,
)
from ..runtime import ConversationRuntime
from ._common import _AUDIO_MEDIA_TYPES, require_owned_conversation

# ── Request body models ───────────────────────────────────────────────────────


class ExportBody(BaseModel):
    """Optional body for the export endpoint.

    ``follow_up_seqs`` — the seq values of USER MessageEvents whose Q&A pairs
    to include in the exported document (WALK-20). Empty / absent = export the
    report only (byte-identical to the pre-WALK-20 baseline).

    ``theme`` / ``mode`` — brand theme selection for PDF exports (§1.5).
    Defaults to disco/light.  Unknown theme → 400.  MD is byte-identical
    regardless of theme.
    """

    follow_up_seqs: list[int] | None = None
    theme: str = "disco"
    mode: str = "light"


class AudioBody(BaseModel):
    """Optional body for the audio endpoint.

    ``follow_up_seqs`` — the seq values of USER MessageEvents whose Q&A pairs
    to include in the audio overview (WALK-20). Empty / absent = report only.
    """

    follow_up_seqs: list[int] | None = None


# Keep the public optional-body defaults unchanged while making FastAPI's body
# binding explicit. FastAPI deep-copies field defaults for each request.
_DEFAULT_EXPORT_BODY = ExportBody()
_DEFAULT_AUDIO_BODY = AudioBody()


# ── Helper: gather follow-up pairs from the event log ─────────────────────────


def _audio_generation_failure(
    exc: TtsBackendError | TurnScriptError,
) -> dict[str, str]:
    """Return the stable wire payload for a known audio-generation failure."""
    reason = "turn_script" if isinstance(exc, TurnScriptError) else "tts_backend"
    return {"reason": reason, "detail": str(exc)}


def _gather_follow_up_pairs(
    events: list[Any],
    report: ReportEvent,
    follow_up_seqs: list[int],
) -> list[tuple[str, str]]:
    """Return (question, answer) tuples for the selected follow-up turns.

    ``follow_up_seqs`` lists the ``seq`` values of the USER MessageEvents that
    represent the *question* side of each desired follow-up pair.  For each
    matching user message we look for the immediately following assistant
    message and bundle them into a ``(question, answer)`` tuple.

    Ordering follows the original event sequence, not the order of the supplied
    seq list.  System-plumbing messages (``<system-reminder>`` / ``<reground-anchors>``)
    are silently skipped — same filter as ``useDeepResearch.followUps``.
    """
    report_seq = report.seq or 0
    selected = set(follow_up_seqs)

    # Collect all clean post-report messages in event order.
    post_report: list[MessageEvent] = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and (e.seq or 0) > report_seq
        and e.message.role in ("user", "assistant")
        and not (e.message.content or "").startswith("<system-reminder>")
        and not (e.message.content or "").startswith("<reground-anchors>")
    ]

    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(post_report):
        msg = post_report[i]
        if msg.message.role == "user" and (msg.seq or -1) in selected:
            question = msg.message.content or ""
            answer = ""
            if i + 1 < len(post_report) and post_report[i + 1].message.role == "assistant":
                answer = post_report[i + 1].message.content or ""
                i += 2
            else:
                i += 1
            pairs.append((question, answer))
        else:
            i += 1
    return pairs


# ── Helper: resolve the export payload (bytes + media type + extension) ───────


async def _resolve_export_payload(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    conversation_id: str,
    fmt: str,
    follow_up_seqs: list[int],
    theme: str = "disco",
    mode: str = "light",
) -> tuple[bytes, str | None, str]:
    """Resolve ``(payload, media_type, ext)`` for a report export.

    Both formats serialize inline (md/pdf in-process) — bypassing
    ConversationRuntime so follow-ups, theme/mode, AND the generated title
    (W-10) can be threaded without touching runtime.py / the DR-service module.
    Raises HTTPException(404) when no report exists, (400) on a serializer
    error or unknown theme.
    """
    # Validate the theme early so callers get a clear 400 before any I/O. Validate
    # against the GALLERY catalogue ("{theme}-{mode}") — stricter than resolve_theme,
    # which would silently light-fall-back an unknown mode (e.g. theme=ink mode=dark).
    # EXCEPT for md: markdown has no styling, so the export is byte-identical
    # regardless of theme/mode (see test_markdown_byte_parity_with_theme). Gallery-
    # rejecting an md export over a cosmetic, ignored theme is wrong — a valid-tokens
    # combo like neutral-dark (not a curated gallery entry) produces the SAME md. So
    # the gallery check applies only where the theme actually renders (pdf, etc.).
    if fmt != "md":
        from disco.core.brand import is_valid_template as _is_valid_template

        if not _is_valid_template(f"{theme}-{mode}"):
            raise HTTPException(status_code=400, detail=f"Unknown template {theme!r}/{mode!r}")

    # Both formats serialize inline so theme/mode, follow-ups, AND the generated
    # TITLE (W-10) are honored.  The runtime.export_report path threads none of
    # these, so we no longer route through it.
    from ..report_export import (
        EXTENSIONS,
        MEDIA_TYPES,
        serialize_markdown,
        serialize_pdf,
    )

    events = await store.get_events(conversation_id)
    report: ReportEvent | None = next(
        (e for e in reversed(events) if isinstance(e, ReportEvent)), None
    )
    if report is None:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "no_report"})

    # W-10: prefer the real generated conversation title for the cover/title
    # page; the serializers fall back to report.query when it's None/empty.
    title: str | None = None
    with contextlib.suppress(Exception):
        title = await store.get_title(conversation_id)

    follow_ups = _gather_follow_up_pairs(events, report, follow_up_seqs)
    try:
        if fmt == "md":
            return (
                serialize_markdown(report, follow_ups, title).encode("utf-8"),
                MEDIA_TYPES[fmt],
                EXTENSIONS[fmt],
            )
        # pdf
        return (
            serialize_pdf(report, follow_ups, theme=theme, mode=mode, title=title),
            MEDIA_TYPES[fmt],
            EXTENSIONS[fmt],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _resolve_audio_inputs(
    store: SqliteEventStore,
    conversation_id: str,
    mode: str,
    follow_up_seqs: list[int],
) -> tuple[ReportEvent, list[tuple[str, str]] | None, Any, Path]:
    """Resolve (report, follow_ups, tts_settings, out_dir) for an audio request.

    Shared by the blocking POST and the SSE-streaming endpoint so they agree
    exactly on inputs / cache key.  Raises HTTPException(400) on a bad mode and
    (404) when no ReportEvent exists for the conversation.
    """
    if mode not in ("podcast", "single"):
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "reason": "invalid_mode", "detail": f"Unknown mode {mode!r}"},
        )

    report: ReportEvent | None = None
    all_events: list[Any] = []
    with contextlib.suppress(Exception):
        all_events = await store.get_events(conversation_id)
        for e in reversed(all_events):
            if isinstance(e, ReportEvent):
                report = e
                break
    if report is None:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "no_report"})

    follow_ups: list[tuple[str, str]] | None = None
    if follow_up_seqs:
        follow_ups = _gather_follow_up_pairs(all_events, report, follow_up_seqs)

    from disco.core.llm import ConfigStore

    tts = ConfigStore().load().tts
    out_dir = report_audio_cache_dir() / conversation_id
    return report, follow_ups, tts, out_dir


# ── Router factory ────────────────────────────────────────────────────────────


def make_report_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.post("/api/conversations/{conversation_id}/report/export")
    async def export_report(
        conversation_id: str,
        request: Request,
        fmt: str = Query(...),
        body: Annotated[ExportBody, Body()] = _DEFAULT_EXPORT_BODY,
    ) -> Response:
        """Export the latest Deep Research report as MD or PDF.

        Query param ``fmt`` must be md or pdf.  Optional JSON body
        ``{"follow_up_seqs": [5, 12]}`` includes the selected follow-up Q&A
        pairs in the exported document (WALK-20).

        Returns 200 with the file streamed (Content-Type + Content-Disposition).
        Returns 404 when no ReportEvent exists for this conversation.
        Returns 400 for an unknown format."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)

        valid_fmts = frozenset({"md", "pdf"})
        if fmt not in valid_fmts:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown export format: {fmt!r}. Valid: md, pdf",
            )

        # Resolve the export bytes (follow-up-aware md/pdf inline, else runtime path).
        payload, media_type, ext = await _resolve_export_payload(
            store,
            runtime,
            conversation_id,
            fmt,
            body.follow_up_seqs or [],
            theme=body.theme,
            mode=body.mode,
        )

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
        request: Request,
        mode: str = Query("podcast"),
        body: Annotated[AudioBody, Body()] = _DEFAULT_AUDIO_BODY,
    ) -> dict:
        """Generate (or return the cached) audio overview for the latest Deep
        Research report on this conversation.  Runs the audio pipeline in-process
        (no sandbox), caching mp3 + transcript per conversation.

        ``mode`` query param: ``podcast`` (default — two-host dialogue) or
        ``single`` (single-voice honest walkthrough).  The two modes write
        separate cache files so re-pressing with a different mode generates a
        fresh artifact instead of colliding (WALK-21 / D3).

        Optional JSON body ``{"follow_up_seqs": [5, 12]}`` includes the selected
        follow-up Q&A pairs in the audio script (WALK-20).  The cache key
        includes the follow-up count so different selections produce separate
        cached files.

        404 → no ReportEvent; 400 → unknown mode; 503 → TTS disabled in
        Settings; 502 → synth/LLM failure. 200 →
        ``{"ok": True, "mp3_url": ..., "transcript_url": ...}``."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        report, follow_ups, tts, out_dir = await _resolve_audio_inputs(
            store, conversation_id, mode, body.follow_up_seqs or []
        )
        try:
            mp3_path, transcript_path = await generate_report_audio(
                report,
                tts_settings=tts,
                out_dir=out_dir,
                mode=mode,
                follow_ups=follow_ups,
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
                detail={"ok": False, **_audio_generation_failure(exc)},
            ) from exc

        return {
            "ok": True,
            "mp3_url": f"/conversations/{conversation_id}/report/audio/{mp3_path.name}",
            "transcript_url": (
                f"/conversations/{conversation_id}/report/audio/{transcript_path.name}"
            ),
        }

    @router.post("/conversations/{conversation_id}/report/audio/stream")
    async def report_audio_stream(
        conversation_id: str,
        request: Request,
        mode: str = Query("podcast"),
        body: AudioBody | None = None,
    ) -> StreamingResponse:
        """SSE variant of the audio endpoint that streams REAL staged progress
        (W-09).  Emits one ``data: {json}`` frame per pipeline stage:

          - ``{"stage": "preparing"}``           — generating the turn-script
          - ``{"stage": "downloading_model"}``   — ONLY on a genuine first-run
                                                    bundled-model download (W-08)
          - ``{"stage": "synthesizing", "current": n, "total": m}`` — per turn
          - ``{"stage": "mixing"}``              — mixing + encoding
          - ``{"stage": "cache_hit"}``           — warm cache, instant
          - ``{"stage": "done", "mp3_url": ..., "transcript_url": ...}`` — final
          - ``{"stage": "error", "reason": ..., "detail": ...}`` — failure

        The blocking POST endpoint above remains the fallback (the FE falls back
        to it if streaming is unavailable).  Bad mode → 400, no report → 404
        (raised before the stream opens)."""
        body = body or AudioBody()
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        report, follow_ups, tts, out_dir = await _resolve_audio_inputs(
            store, conversation_id, mode, body.follow_up_seqs or []
        )

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def _on_progress(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def _run() -> None:
            try:
                mp3_path, transcript_path = await generate_report_audio(
                    report,
                    tts_settings=tts,
                    out_dir=out_dir,
                    mode=mode,
                    follow_ups=follow_ups,
                    resolve_key=runtime._resolve_secret if runtime is not None else None,
                    on_progress=_on_progress,
                )
                await queue.put(
                    {
                        "stage": "done",
                        "mp3_url": (
                            f"/conversations/{conversation_id}/report/audio/{mp3_path.name}"
                        ),
                        "transcript_url": (
                            f"/conversations/{conversation_id}/report/audio/{transcript_path.name}"
                        ),
                    }
                )
            except TtsDisabled:
                await queue.put({"stage": "error", "reason": "tts_disabled"})
            except (TtsBackendError, TurnScriptError) as exc:
                await queue.put({"stage": "error", **_audio_generation_failure(exc)})
            except Exception as exc:  # never hang the stream on an unexpected error
                await queue.put({"stage": "error", "reason": "internal", "detail": str(exc)})
            finally:
                await queue.put(None)  # sentinel: generation finished

        async def _events():
            task = asyncio.create_task(_run())
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                # Client disconnect / generator close: stop the background run.
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/conversations/{conversation_id}/report/audio/{name}")
    async def report_audio_file(conversation_id: str, name: str, request: Request) -> Response:
        """Serve a cached audio-overview file (mp3 / transcript) as an attachment.
        Jailed: canonical basename only + resolve-jail to this conversation's cache."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
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
