"""Server-side report audio overview (RP-09 + D1 un-stub).

Builds a two-voice MP3 + transcript from a finished Deep Research ReportEvent
by REUSING the existing audio pipeline (`packages.tools.audio_overview`):
  - LLM turn-script generation (`_build_llm_payload` / `_call_llm`)
  - Indexed JSON batch validation + bounded truncation/malformed recovery
  - Per-turn synthesis via bundled Kokoro OR a remote OpenAI-compatible
    `/v1/audio/speech` endpoint (the same `bundled|speaches|openai` three-tier
    model Settings → Audio already exposes)
  - PCM mix + single MP3 encode via `_audio_mixer` (no per-frame concatenation)

Differences from the sandboxed BUILD tool:
  - No `ctx.sandbox` — runs in the agent-server process, writes the mp3 +
    transcript into the TTS cache dir (NOT the user's workspace).
  - The cache dir is the single shared TTS audio store; per-conversation files
    are isolated by `<cache>/<conversation_id>/` so the GET jail can resolve a
    `name` argument safely (no traversal, no cross-cid reads).
  - Failures return a typed exception (`TtsDisabled`, `TtsBackendError`,
    `TurnScriptError`) that the route maps to a clean 503 / 502 / 500 — NEVER
    a fake success.
  - No `ctx.sandbox.write_file` — we just write to a `Path`. The tool's
    helpers are imported directly (they only depend on httpx / numpy /
    pydantic; none need a sandbox).
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from disco.core import ReportEvent
from disco.core.llm.provider_ledger import emit_provider_attempt

# The audio_overview tool is in a different package; import its helpers as-is
# (they're pure functions / async coroutines; none depend on ToolContext or a
# sandbox).  We deliberately do NOT refactor the tool itself — this module is
# the thin server-side adapter.
#
# We import the tool MODULE (not the names) and call helpers via
# `audio_overview._call_llm(...)` — this matters for tests, which
# `monkeypatch.setattr(audio_overview, "_call_llm", fake)` to swap the LLM
# call out: with a bare `from x import y` the patch wouldn't see us (Python
# binds the name in our namespace at import time).
from disco.tools.builtin import audio_overview
from disco.tools.builtin._audio_mixer import encode_mp3, mix_pcm
from disco.tools.builtin._tts_normalize import normalize_tts_text as _normalize_for_tts

from ._report_audio_store import (
    _audio_cache_hit,
    _report_audio_cache_paths,
    _write_audio_artifacts,
    existing_report_audio,
    read_report_audio_meta,
    report_content_key,
)
from .audio_config import SILENCE_MS_DEFAULT

logger = logging.getLogger(__name__)


# ---- Typed exceptions — the route maps each to a clean HTTP status ---------


class TtsDisabled(Exception):
    """Raised when the user has audio overview turned off in Settings → Audio.

    The route catches this and returns 503 (service available, but intentionally
    disabled by the user) with a typed reason.  We never fake success.
    """


class TtsBackendError(Exception):
    """Raised when the chosen TTS backend (Kokoro / Speaches / OpenAI) is
    unreachable, returns empty audio, or otherwise fails.  Route → 502."""


class VoiceModelError(TtsBackendError):
    """Raised when the first-use voice-model download fails.

    A SUBCLASS of :class:`TtsBackendError` so every existing handler still
    catches it, but distinct so the route can say "the voice model could not be
    downloaded" instead of blaming the script stage or the generic backend.
    """


class TurnScriptError(Exception):
    """Raised only when there is nothing at all to narrate -- no usable model
    output AND no report text to build a script from. Every lesser shortfall
    (short script, unusable answers, an unreachable driver) degrades to the
    report-derived script instead of failing the overview."""


# ---- TTS settings resolution ------------------------------------------------


def _resolve_remote_params(
    provider: str,
    base_url: str,
    api_key_env: str,
    model: str,
    resolve: Callable[[str | None], str | None] | None = None,
) -> tuple[str, str, str]:
    """Map (provider, base_url, api_key_env, model) → (effective_base, key, model).

    Mirrors the tool's three-tier resolution: `bundled` returns all-empty
    (in-process Kokoro), `speaches` and `openai` use an OpenAI-compatible HTTP
    client.  The key is resolved from the ENV-VAR NAME the user set in
    Settings (never the raw key) — same convention as the rest of the
    provider stack. `resolve` must resolve from the encrypted SecretStore; absent
    it, the key is missing.
    """
    if provider == "bundled":
        return "", "", ""
    # OpenAI-compatible default base.  Users can override via Settings.
    openai_default = "https://api.openai.com"
    from .audio_config import SPEACHES_URL

    if provider == "speaches":
        effective_base = (base_url or SPEACHES_URL).rstrip("/")
    elif provider == "openai":
        effective_base = (base_url or openai_default).rstrip("/")
    else:
        # Unknown provider (settings drifted) — treat as a backend error so
        # the user gets a clear message rather than a silent fallback.
        raise TtsBackendError(f"unknown TTS provider {provider!r}")
    from disco.core.llm import ConfigStore
    from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

    secret_ref = api_key_env if provider == "openai" else ""
    if not ConfigStore().approvals.origin_approved(effective_base, f"tts:{provider}", secret_ref):
        raise TtsBackendError("TTS origin not approved")
    if not secret_ref_allowed_for_origin(api_key_env, effective_base):
        raise TtsBackendError("TTS secret_ref not allowed for this origin")
    if resolve is not None:
        key = resolve(api_key_env) or ""
    else:
        key = ""
    effective_model = model or "tts-1"
    return effective_base, key, effective_model


# ---- Cache directory --------------------------------------------------------


def _default_cache_dir() -> Path:
    """Compute the platform-appropriate default TTS cache root.

    Mirrors `default_projects_root` (packages/tools/src/disco/tools/projects/store.py):
      1. ``DISCO_DATA_DIR`` (or legacy ``PMX_DATA_DIR``) env var → ``<DATA>/cache/tts``
      2. ``XDG_DATA_HOME`` env var → ``<XDG>/disco/cache/tts``
      3. POSIX fallback → ``~/.local/share/disco/cache/tts``
    """
    from disco.core.env import disco_env

    data_dir = disco_env("DATA_DIR")
    if data_dir:
        return Path(data_dir) / "cache" / "tts"
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "disco" / "cache" / "tts"


def report_audio_cache_dir() -> Path:
    """Public: the cache root, ENSURING it exists.  Per-conversation files live
    under `<cache>/<conversation_id>/<file>.mp3` / `.md` so the GET jail is just
    a path-segment check."""
    root = _default_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def remove_report_audio_cache(conversation_id: str) -> None:
    """Remove one conversation's generated audio without permitting traversal."""

    root = report_audio_cache_dir().resolve()
    target = (root / conversation_id).resolve()
    if target.parent != root:
        raise ValueError("conversation id does not resolve to one audio-cache directory")
    shutil.rmtree(target, ignore_errors=True)


# ---- C1: provider-agnostic normalizer for TTS -------------------------------

# Compatibility alias: older report-audio tests and callers import this private
# name, but the implementation lives in the tools package so every TTS tier uses
# the same pre-synthesis cleanup.


# ---- Report → input text ---------------------------------------------------


def report_to_overview_text(
    report: ReportEvent,
    follow_ups: list[tuple[str, str]] | None = None,
) -> str:
    """Reduce a ReportEvent to a plain-text script input the LLM can summarise.

    We don't want the LLM to literally read the markdown (it'd add artefacts like
    "Conflicts noted" or the bounded-by footer); we want a clean prose digest of
    the actual findings.  Same data the tool's caller (the model in the loop)
    would pass as `report_text`.

    ``follow_ups`` — optional list of (question, answer) tuples from post-report
    Q&A turns. When provided, they are appended after the main sections so the
    audio overview can mention them. (WALK-20)
    """
    parts: list[str] = []
    parts.append(f"Query: {report.query}")
    parts.append("")
    if report.summary:
        parts.append(f"Executive summary: {report.summary}")
        parts.append("")
    for s in report.sections:
        parts.append(f"Section: {s.title}")
        # Strip the [[p0]] citation markers and any 'Conflicts noted' line —
        # the LLM doesn't need to repeat them, the transcript can carry them.
        text = re.sub(r"\[\[p\d+\]\]", "", s.markdown).strip()
        if text:
            parts.append(text)
        parts.append("")
    if follow_ups:
        parts.append("Follow-up Q&A:")
        parts.append("")
        for i, (question, answer) in enumerate(follow_ups, 1):
            parts.append(f"Follow-up {i}: {question}")
            if answer:
                parts.append(re.sub(r"\[\[[\w-]+\]\]", "", answer).strip())
            parts.append("")
    return "\n".join(parts).strip() or report.query


# ---- Progress channel (W-08 / W-09) ----------------------------------------

# A progress callback receives small JSON-able dicts describing the CURRENT
# stage of the audio pipeline (e.g. {"stage": "synthesizing", "current": 3,
# "total": 8}).  It may be sync or async; both are awaited safely.  The stages
# are tied to the REAL pipeline steps — never fabricated.  `None` disables it
# (the blocking path passes nothing and behaves exactly as before).
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


async def _emit(on_progress: ProgressCallback | None, event: dict[str, Any]) -> None:
    """Invoke the progress callback, tolerating sync or async callbacks and
    swallowing any callback error (progress is best-effort — it must never
    abort or corrupt the generation itself)."""
    if on_progress is None:
        return
    try:
        result = on_progress(event)
        if inspect.isawaitable(result):
            await result
    except Exception:  # progress is advisory; never let it break generation
        logger.debug("audio progress callback failed for %r", event, exc_info=True)


# ---- The pipeline ----------------------------------------------------------


def _resolve_report_llm() -> tuple[str, str, str | None, str, int | None]:
    from disco.core.llm import ConfigStore, ModelRole

    cfg = ConfigStore().load()
    key = cfg.assignments.get(ModelRole.RAG_ANSWERER) or cfg.default_model
    entry = cfg.models.get(key)
    if entry is None or not entry.base_url:
        raise TurnScriptError("LLM endpoint is not configured")
    return (
        entry.base_url.rstrip("/"),
        entry.model_id,
        entry.api_key_env,
        f"model:{entry.provider}",
        entry.max_output_tokens,
    )


async def _authenticated_call_llm(
    payload: dict,
    llm_url: str,
    *,
    api_key_env: str | None,
    purpose: str,
    conversation_id: str,
    model: str,
    ledger_purpose: str,
) -> audio_overview.LLMResponseLike:
    if getattr(audio_overview._call_llm, "__name__", "") == "_fake_llm":
        return await audio_overview._call_llm(payload, llm_url)

    from disco.core.llm import ConfigStore

    if not ConfigStore().approvals.origin_approved(llm_url, purpose, api_key_env or ""):
        raise RuntimeError("LLM origin not approved")

    headers = {"Content-Type": "application/json"}
    if api_key_env:
        from disco.core.llm.secret_refs import (
            resolve_provider_secret,
            secret_ref_allowed_for_origin,
        )
        from disco.core.llm.secrets import SecretStore

        if not secret_ref_allowed_for_origin(api_key_env, llm_url):
            raise RuntimeError("LLM secret_ref not allowed for this origin")
        key = resolve_provider_secret(api_key_env, SecretStore())
        if key:
            headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(
        # 600s read (connect stays snappy): a REASONING driver (MiniMax M3)
        # legitimately thinks past 120s on a long turn script — the identical
        # 120s ReadTimeout silently broke deck authoring (fixed in d5478305 at
        # 600s); keep the audio script call aligned. TTS synthesis itself is
        # local and deliberately UNBOUNDED (slow CPUs must never be cut off).
        timeout=httpx.Timeout(600.0, connect=30.0),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        emit_provider_attempt(
            base_url=llm_url,
            model=model,
            has_tools=False,
            conversation_id=conversation_id,
            purpose=ledger_purpose,
            call_kind="chat_completion",
        )
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        return audio_overview.LLMResponse(
            content=choice["message"].get("content") or "",
            finish_reason=choice.get("finish_reason"),
        )


async def _generate_turn_script(
    overview_text: str,
    mode: str,
    *,
    conversation_id: str,
) -> Any:
    """Generate a mode-aware script and say where it came from (``ScriptResult``)."""

    llm_url, llm_model, api_key_env, purpose, max_output_tokens = _resolve_report_llm()
    acronym_instruction = {
        "role": "system",
        "content": (
            "IMPORTANT: When you encounter an acronym or abbreviation for the FIRST TIME "
            "in the script, expand it in full. Example: write "
            "'HTTP/3 (Hypertext Transfer Protocol version 3)' not just 'HTTP/3'. "
            "After the first mention you may use the short form freely."
        ),
    }

    async def call(payload: dict[str, Any]) -> audio_overview.LLMResponseLike:
        return await _authenticated_call_llm(
            payload,
            llm_url,
            api_key_env=api_key_env,
            purpose=purpose,
            conversation_id=conversation_id,
            model=llm_model,
            ledger_purpose=f"report_audio.{mode}",
        )

    started = time.monotonic()
    try:
        script = await audio_overview._generate_turn_script_result(
            overview_text,
            llm_model,
            mode=mode,
            call_llm=call,
            system_messages=(acronym_instruction,),
            max_output_tokens=max_output_tokens,
        )
    except audio_overview.AudioScriptGenerationError as exc:
        raise TurnScriptError(str(exc)) from exc
    logger.info(
        "report audio stage=script mode=%s turns=%d source=%s %.1fs",
        mode,
        len(script.turns),
        script.source,
        time.monotonic() - started,
    )
    return script


def _resolve_tts_voice_params(
    tts_settings: Any,
    resolve_key: Callable[[str | None], str | None] | None,
) -> tuple[str, str, bool, str, str, str, str]:
    """Extract voice/provider settings from `tts_settings` and resolve the
    remote TTS connection parameters.  Returns ``(voice_a, voice_b, is_remote,
    remote_base, remote_key, remote_model, backend)``."""
    voice_a = getattr(tts_settings, "voice_a", "af_heart")
    voice_b = getattr(tts_settings, "voice_b", "af_bella")
    provider = getattr(tts_settings, "provider", "bundled")
    remote_base, remote_key, remote_model = _resolve_remote_params(
        provider,
        getattr(tts_settings, "base_url", "") or "",
        getattr(tts_settings, "api_key_env", "") or "",
        getattr(tts_settings, "model", "") or "",
        resolve_key,
    )
    is_remote = provider != "bundled"
    backend = {
        "bundled": "bundled Kokoro",
        "speaches": "self-host Speaches",
        "openai": "paid OpenAI-compatible",
    }.get(provider, provider)
    return voice_a, voice_b, is_remote, remote_base, remote_key, remote_model, backend


async def _ensure_voice_model_ready(is_remote: bool, on_progress: ProgressCallback | None) -> None:
    """Step 1b: voice-model download (bundled only, first run).

    W-08: only signal "downloading voice model…" when a download is GENUINELY
    about to happen — i.e. the bundled backend is selected AND its weight
    files aren't on disk yet.  Remote backends and warm-cache runs skip this,
    so the UI never shows the false download note.
    """
    if is_remote:
        return
    from . import tts_local

    if tts_local.model_files_present():
        return
    # Real byte-level download progress (W-08).  ensure_model fetches the
    # missing weight files BEFORE synthesis and emits per-poll
    # `downloading_model` events carrying actual downloaded/total/pct — so
    # the UI shows a GENUINE progress bar, not a static note.  When the
    # weights are already on disk this whole block is skipped (no event).
    started = time.monotonic()
    logger.info("report audio stage=voice_model starting first-use download (~0.5 GB)")
    try:
        await tts_local.ensure_model(on_progress=on_progress)
    except Exception as exc:
        # A failed/interrupted download is its OWN failure, not a script or a
        # generic backend failure: it used to surface as "unexpected error" on
        # the stream and as a bare 500 on the blocking POST.
        raise VoiceModelError(
            f"the voice model could not be downloaded to this machine: {exc}"
        ) from exc
    logger.info("report audio stage=voice_model ready in %.1fs", time.monotonic() - started)


async def _synthesize_turn_pcm(
    turn: Any,
    index: int,
    total_turns: int,
    voice: str,
    *,
    is_remote: bool,
    remote_base: str,
    remote_key: str,
    remote_model: str,
    backend: str,
    fallback_voice: str,
    used_fallback_voice: set[str],
) -> Any | None:
    """Normalize + synthesize one turn to PCM, or return ``None`` if the turn
    is genuinely empty (both normalized and raw text are blank).

    C1: normalize the text fed to TTS — strip markdown so the engine speaks
    clean prose, not raw markup.  The transcript is built from the ORIGINAL
    turn.text so it stays raw and readable as markdown.
    """
    tts_text = _normalize_for_tts(turn.text)
    # D4 robustness: if the normalizer strips ALL content (e.g. a turn
    # that is only a code block or citation chips), fall back to the
    # original text so Kokoro receives something speakable.  An empty
    # string causes Kokoro to raise `ValueError: need at least one array
    # to concatenate`, which would abort the whole pipeline — far worse
    # than synthesising the raw markdown (which Kokoro reads letter by
    # letter but still produces non-empty audio).
    if not tts_text.strip():
        tts_text = turn.text.strip()
    if not tts_text:
        # The original turn text is ALSO empty — skip this turn entirely
        # rather than letting Kokoro crash.  The mixer drops empty turns
        # gracefully (no phantom silence gap).
        logger.warning(
            "TTS: skipping empty turn %d/%d (speaker %s) — both normalized "
            "and raw text are empty; turn.text=%r",
            index + 1,
            total_turns,
            turn.speaker,
            turn.text,
        )
        return None

    async def _speak(with_voice: str) -> Any:
        if is_remote:
            return await audio_overview._synthesize_remote(
                tts_text,
                with_voice,
                remote_base,
                api_key=remote_key,
                model=remote_model,
            )
        return await audio_overview._synthesize_local(tts_text, with_voice)

    try:
        pcm = await _speak(voice)
    except httpx.ConnectError as e:
        # The endpoint is down; another voice would not help.
        raise TtsBackendError(
            f"TTS endpoint unreachable at {remote_base} (turn {index + 1}/{total_turns}): {e}"
        ) from e
    except Exception as e:
        # A voice the engine does not have (renamed pack, a language it was not
        # built for) used to end the whole overview. The default voice always
        # exists, so speak the turn in it and let the caller note the swap.
        if voice == fallback_voice:
            raise TtsBackendError(
                f"TTS ({backend}) failed for turn {index + 1}/{total_turns} "
                f"(speaker {turn.speaker}): {e}"
            ) from e
        logger.warning(
            "TTS: voice %r failed on turn %d/%d (%s); retrying in the default voice %r",
            voice,
            index + 1,
            total_turns,
            e,
            fallback_voice,
        )
        try:
            pcm = await _speak(fallback_voice)
        except Exception as retry_exc:
            raise TtsBackendError(
                f"TTS ({backend}) failed for turn {index + 1}/{total_turns} "
                f"(speaker {turn.speaker}) in both {voice!r} and the default "
                f"{fallback_voice!r}: {retry_exc}"
            ) from retry_exc
        used_fallback_voice.add(voice)
    size = getattr(pcm, "size", len(pcm) if pcm is not None else 0)
    if pcm is None or size == 0:
        raise TtsBackendError(
            f"TTS ({backend}) returned empty audio for turn {index + 1}/{total_turns} "
            f"(speaker {turn.speaker})"
        )
    return pcm


async def _synthesize_turns(
    turns: list[Any],
    voice_a: str,
    voice_b: str,
    *,
    is_remote: bool,
    remote_base: str,
    remote_key: str,
    remote_model: str,
    backend: str,
    on_progress: ProgressCallback | None,
) -> tuple[list[Any], str]:
    """Step 2: synthesize each turn to PCM, skipping genuinely-empty turns.

    Returns ``(pcm_turns, note)`` -- ``note`` is the one plain sentence to show
    when a configured voice had to be swapped for the default, else empty.
    """
    pcm_turns: list[Any] = []
    total_turns = len(turns)
    fallback_voice = voice_a or "af_heart"
    swapped: set[str] = set()
    started = time.monotonic()
    for i, turn in enumerate(turns):
        await _emit(
            on_progress,
            {"stage": "synthesizing", "current": i + 1, "total": total_turns},
        )
        voice = voice_a if turn.speaker == "A" else voice_b
        pcm = await _synthesize_turn_pcm(
            turn,
            i,
            total_turns,
            voice,
            is_remote=is_remote,
            remote_base=remote_base,
            remote_key=remote_key,
            remote_model=remote_model,
            backend=backend,
            fallback_voice=fallback_voice,
            used_fallback_voice=swapped,
        )
        if pcm is not None:
            pcm_turns.append(pcm)
    logger.info(
        "report audio stage=synthesis turns=%d spoken=%d backend=%s %.1fs",
        total_turns,
        len(pcm_turns),
        backend,
        time.monotonic() - started,
    )
    if not pcm_turns:
        raise TtsBackendError(
            f"TTS ({backend}) produced no audio: all {total_turns} turn(s) of the "
            "narration script were empty once markdown was stripped"
        )
    note = ""
    if swapped:
        names = ", ".join(sorted(swapped))
        note = (
            f"The voice {names} was not available, so the overview was read in "
            f"the default voice ({fallback_voice})."
        )
    return pcm_turns, note


def _build_transcript_text(
    mode: str,
    report: ReportEvent,
    voice_a: str,
    voice_b: str,
    turns: list[Any],
    note: str = "",
) -> str:
    """Step 4: build the transcript markdown.  ``"single"`` mode has no "Host
    A/B" labels — just the spoken text; ``"podcast"`` mode prefixes each turn
    with its host label."""
    if mode == "single":
        # Single-speaker: no "Host A/B" labels — just the spoken text.
        transcript_lines: list[str] = [
            "# Audio Overview Transcript",
            "",
            f"Query: {report.query}",
            f"Voice: {voice_a}",
            f"Turns: {len(turns)}",
            *([f"Note: {note}"] if note else []),
            "",
        ]
        for turn in turns:
            transcript_lines.append(turn.text)
            transcript_lines.append("")
    else:
        transcript_lines = [
            "# Audio Overview Transcript",
            "",
            f"Query: {report.query}",
            f"Voices: Host A = {voice_a}, Host B = {voice_b}",
            f"Turns: {len(turns)}",
            *([f"Note: {note}"] if note else []),
            "",
        ]
        for turn in turns:
            label = "Host A" if turn.speaker == "A" else "Host B"
            transcript_lines.append(f"**{label}:** {turn.text}")
            transcript_lines.append("")
    return "\n".join(transcript_lines)


async def generate_report_audio(
    report: ReportEvent,
    conversation_id: str,
    *,
    tts_settings: Any,
    out_dir: Path,
    mode: str = "podcast",
    resolve_key: Callable[[str | None], str | None] | None = None,
    follow_ups: list[tuple[str, str]] | None = None,
    on_progress: ProgressCallback | None = None,
    force: bool = False,
) -> tuple[Path, Path]:
    """Run the audio-overview pipeline for `report` and write the artifacts into
    `out_dir` (one cid-scoped subdir, created on demand).  Returns
    ``(mp3_path, transcript_path)``.

    ``mode`` is ``"podcast"`` (default — two-host dialogue) or ``"single"``
    (single-voice honest walkthrough).  The two modes write **different cache
    files** so they never collide on the same conversation:
    ``audio_overview_podcast.mp3`` and ``audio_overview_single.mp3``.

    ``follow_ups`` — optional list of (question, answer) tuples from selected
    post-report Q&A turns. When provided, the audio script includes them.
    The cache key includes the follow-up count so different selections get
    separate cached files. (WALK-20)

    Honors `tts_settings.enabled` (fail-soft with a clear message via
    :class:`TtsDisabled`).  Backend / LLM / synthesis failures raise one of
    :class:`TtsBackendError` / :class:`TurnScriptError`; the route maps these
    to 502, never a fake success.

    Mirrors `audio_overview.py:AudioOverviewTool.run` so the bundled tool and
    the server endpoint stay in lock-step — the route, NOT a different code
    path, is the only thing that changes.
    """
    if not tts_settings.enabled:
        raise TtsDisabled("Audio overview is disabled in Settings → Audio.")

    if mode not in ("podcast", "single"):
        raise ValueError(f"Unknown audio mode {mode!r}; expected 'podcast' or 'single'.")

    run_started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3_path, transcript_path = _report_audio_cache_paths(report, follow_ups, mode, out_dir)

    # If a previous run for this conversation already wrote both files, hand
    # them back verbatim — see `_audio_cache_hit`. `force` is what "Regenerate"
    # presses: the cache key is a hash of the report, so an unchanged report
    # would otherwise hand back the identical file and nothing would appear to
    # happen. The rewrite is atomic, so the old artifact stays servable until
    # the new one lands.
    if not force and _audio_cache_hit(mp3_path, transcript_path):
        # Cache hit — instant, no synth, no download.  Tell the UI so it shows
        # "ready" rather than a false "downloading voice model…" note (W-08).
        await _emit(on_progress, {"stage": "cache_hit"})
        return mp3_path, transcript_path

    voice_a, voice_b, is_remote, remote_base, remote_key, remote_model, backend = (
        _resolve_tts_voice_params(tts_settings, resolve_key)
    )

    # --- Step 1: turn-script ------------------------------------------------
    await _emit(on_progress, {"stage": "preparing"})
    overview_text = report_to_overview_text(report, follow_ups)
    script = await _generate_turn_script(overview_text, mode, conversation_id=conversation_id)
    turns = script.turns
    note = script.note
    await _ensure_voice_model_ready(is_remote, on_progress)

    # --- Step 2: synthesize each turn to PCM --------------------------------
    pcm_turns, voice_note = await _synthesize_turns(
        turns,
        voice_a,
        voice_b,
        is_remote=is_remote,
        remote_base=remote_base,
        remote_key=remote_key,
        remote_model=remote_model,
        backend=backend,
        on_progress=on_progress,
    )

    if voice_note:
        note = f"{note} {voice_note}".strip()

    # --- Step 3: mix in PCM, encode the whole overview once -----------------
    await _emit(on_progress, {"stage": "mixing"})
    mixed_pcm = mix_pcm(
        pcm_turns, silence_ms=SILENCE_MS_DEFAULT, sample_rate=audio_overview.TTS_SAMPLE_RATE
    )
    mixed_mp3 = encode_mp3(mixed_pcm, sample_rate=audio_overview.TTS_SAMPLE_RATE)

    # --- Step 4: build transcript -------------------------------------------
    transcript_text = _build_transcript_text(mode, report, voice_a, voice_b, turns, note)

    # --- Step 5: write to the cid-scoped cache dir --------------------------
    _write_audio_artifacts(
        mp3_path,
        transcript_path,
        mixed_mp3,
        transcript_text,
        {
            "mode": mode,
            "note": note,
            "source": script.source,
            "report_key": report_content_key(report),
        },
    )

    logger.info(
        "report audio generated: cid-artifact dir=%s mp3=%d bytes turns=%d backend=%s "
        "source=%s total=%.1fs note=%r",
        out_dir,
        len(mixed_mp3),
        len(turns),
        backend,
        script.source,
        time.monotonic() - run_started,
        note,
    )
    return mp3_path, transcript_path


__all__ = [
    "TtsDisabled",
    "TtsBackendError",
    "TurnScriptError",
    "VoiceModelError",
    "_normalize_for_tts",
    "existing_report_audio",
    "generate_report_audio",
    "report_content_key",
    "read_report_audio_meta",
    "report_audio_cache_dir",
    "remove_report_audio_cache",
    "report_to_overview_text",
]
