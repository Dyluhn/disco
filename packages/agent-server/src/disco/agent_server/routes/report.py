"""Report export + audio-overview routes (RP-07 / C2)."""

from __future__ import annotations

import contextlib
import posixpath
from typing import Any

from disco.core import DEFAULT_OWNER_ID, MessageEvent, ReportEvent
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from ..report_audio import (
    TtsBackendError,
    TtsDisabled,
    TurnScriptError,
    generate_report_audio,
    report_audio_cache_dir,
)
from ..runtime import ConversationRuntime
from ._common import _AUDIO_MEDIA_TYPES

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


# ── Helper: gather follow-up pairs from the event log ─────────────────────────


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
            if (
                i + 1 < len(post_report)
                and post_report[i + 1].message.role == "assistant"
            ):
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

    With ``follow_up_seqs`` (md/pdf) gather the selected Q&A pairs and serialize
    inline — bypassing ConversationRuntime so follow-ups can be threaded without
    touching runtime.py.  Otherwise use the existing runtime export path.
    Raises HTTPException(404) when no report exists, (400) on a serializer error
    or unknown theme.
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
            raise HTTPException(
                status_code=400, detail=f"Unknown template {theme!r}/{mode!r}"
            )

    # md/pdf ALWAYS serialize inline so theme/mode (and any follow-ups) are honored.
    # The runtime.export_report path below does NOT thread theme/mode — routing
    # md/pdf through it dropped dark-mode PDFs to light. Only docx (sandbox/pandoc)
    # needs the runtime path.
    if fmt in ("md", "pdf"):
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
            raise HTTPException(
                status_code=404, detail={"ok": False, "reason": "no_report"}
            )

        follow_ups = _gather_follow_up_pairs(events, report, follow_up_seqs)
        try:
            if fmt == "md":
                return (
                    serialize_markdown(report, follow_ups).encode("utf-8"),
                    MEDIA_TYPES[fmt],
                    EXTENSIONS[fmt],
                )
            return (
                serialize_pdf(report, follow_ups, theme=theme, mode=mode),
                MEDIA_TYPES[fmt],
                EXTENSIONS[fmt],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await runtime.export_report(
            conversation_id, fmt, owner_id=DEFAULT_OWNER_ID
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "no_report"})
    return result


# ── Router factory ────────────────────────────────────────────────────────────


def make_report_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/conversations/{conversation_id}/report/export")
    async def export_report(
        conversation_id: str,
        fmt: str = Query(...),
        body: ExportBody = ExportBody(),
    ) -> Response:
        """Export the latest Deep Research report as MD, PDF, or DOCX.

        Query param ``fmt`` must be md, pdf, or docx.  Optional JSON body
        ``{"follow_up_seqs": [5, 12]}`` includes the selected follow-up Q&A
        pairs in the exported document (WALK-20).

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
        mode: str = Query("podcast"),
        body: AudioBody = AudioBody(),
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
            raise HTTPException(
                status_code=404, detail={"ok": False, "reason": "no_report"}
            )

        # WALK-20: gather follow-up Q&A pairs for the selected seqs.
        follow_ups: list[tuple[str, str]] | None = None
        follow_up_seqs = body.follow_up_seqs or []
        if follow_up_seqs:
            follow_ups = _gather_follow_up_pairs(all_events, report, follow_up_seqs)

        from disco.core.llm import ConfigStore

        tts = ConfigStore().load().tts
        out_dir = report_audio_cache_dir() / conversation_id
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
