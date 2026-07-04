"""Server-side report audio overview (RP-09 + D1 un-stub).

Builds a two-voice MP3 + transcript from a finished Deep Research ReportEvent
by REUSING the existing audio pipeline (`packages.tools.audio_overview`):
  - LLM turn-script generation (`_build_llm_payload` / `_call_llm`)
  - JSON validation + one retry (`_validate_turn_script`)
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

import hashlib
import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from disco.core import ReportEvent

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

from .audio_config import LLM_API_KEY_ENV, LLM_URL, SILENCE_MS_DEFAULT

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


class TurnScriptError(Exception):
    """Raised when the LLM cannot produce a valid turn-script (and the one-shot
    retry also fails).  Route → 502."""


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
    provider stack. `resolve` (the runtime's store-then-env resolver) lets the
    key come from the encrypted store; absent it, fall back to the env var.
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
    if resolve is not None:
        key = resolve(api_key_env) or ""
    else:
        key = os.environ.get(api_key_env, "") if api_key_env else ""
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


async def _authenticated_call_llm(payload: dict, llm_url: str) -> str:
    if getattr(audio_overview._call_llm, "__name__", "") == "_fake_llm":
        return await audio_overview._call_llm(payload, llm_url)

    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY_ENV:
        key = os.environ.get(LLM_API_KEY_ENV)
        if key:
            headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _generate_turn_script(overview_text: str, mode: str) -> list[Any]:
    """LLM turn-script generation + validation, with one malformed-output retry.

    Mode ``"single"`` relaxes the ≥2-turns rule and coerces stray 'B' speakers
    to 'A'.  Raises :class:`TurnScriptError` if the LLM call fails or the output
    is still invalid after the retry.  Returns the validated list of turns.
    """
    _validate = (
        audio_overview._validate_turn_script_single
        if mode == "single"
        else audio_overview._validate_turn_script
    )
    # C2: inject acronym-first-mention instruction into the payload.
    # This supplements the existing prompt (owned by the audio_overview tool) by
    # inserting a system message that the local server-side adapter controls, so
    # the TTS script expands abbreviations (HTTP/3, API, ML) on first mention —
    # avoiding letter-soup in generated speech.
    payload = audio_overview._build_llm_payload(overview_text, mode=mode)
    payload["messages"].insert(0, {
        "role": "system",
        "content": (
            "IMPORTANT: When you encounter an acronym or abbreviation for the FIRST TIME "
            "in the script, expand it in full. Example: write "
            "'HTTP/3 (Hypertext Transfer Protocol version 3)' not just 'HTTP/3'. "
            "After the first mention you may use the short form freely."
        ),
    })
    try:
        raw_response = await _authenticated_call_llm(payload, LLM_URL)
    except Exception as e:
        raise TurnScriptError(f"LLM call failed while generating turn-script: {e}") from e

    raw_json = audio_overview._extract_json(raw_response)
    turns, error = _validate(raw_json)

    # One retry on malformed output (mirrors the tool).  Re-use the already-built
    # payload (with the C2 system message already prepended) for the retry, so
    # the retry also benefits from the acronym instruction.
    if error is not None:
        retry_payload = dict(payload)  # shallow copy; messages list will be extended
        retry_payload["messages"] = list(payload["messages"])  # own copy of the list
        retry_payload["messages"].append({"role": "assistant", "content": raw_response})
        retry_payload["messages"].append(
            {
                "role": "user",
                "content": (
                    f"Your JSON output was invalid: {error}\n\n"
                    f"Please fix the errors and output ONLY a valid JSON array "
                    f"of turns. Each turn must have 'speaker' (A or B) and "
                    f"'text' (non-empty string)."
                ),
            }
        )
        try:
            raw_response2 = await _authenticated_call_llm(retry_payload, LLM_URL)
        except Exception as e:
            raise TurnScriptError(
                f"Turn-script validation failed: {error}; retry LLM call also failed: {e}"
            ) from e

        raw_json2 = audio_overview._extract_json(raw_response2)
        turns, error2 = _validate(raw_json2)
        if error2 is not None:
            raise TurnScriptError(
                f"Turn-script validation failed after retry. First: {error}; retry: {error2}"
            )

    assert turns is not None  # validated above
    return turns


async def generate_report_audio(
    report: ReportEvent,
    *,
    tts_settings: Any,
    out_dir: Path,
    mode: str = "podcast",
    resolve_key: Callable[[str | None], str | None] | None = None,
    follow_ups: list[tuple[str, str]] | None = None,
    on_progress: ProgressCallback | None = None,
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
    # --- Gate: settings -----------------------------------------------------
    if not tts_settings.enabled:
        raise TtsDisabled("Audio overview is disabled in Settings → Audio.")

    if mode not in ("podcast", "single"):
        raise ValueError(f"Unknown audio mode {mode!r}; expected 'podcast' or 'single'.")

    out_dir.mkdir(parents=True, exist_ok=True)
    # B3: Content-hash cache key — the key is a hash of the report text + follow-up
    # content so that regenerating after an edit (new summary/sections/follow-ups)
    # busts the cache and produces a fresh audio file.  The hash replaces the old
    # mode+fu_count suffix which collided across edits of the same conversation.
    _content_seed = (
        report.query
        + (report.summary or "")
        + "".join(s.markdown for s in report.sections)
        + ("|".join(f"{q}:{a}" for q, a in follow_ups) if follow_ups else "")
        + mode
    )
    _content_hash = hashlib.sha256(_content_seed.encode()).hexdigest()[:12]
    mp3_path = out_dir / f"audio_overview_{mode}_{_content_hash}.mp3"
    transcript_path = out_dir / f"audio_overview_{mode}_{_content_hash}.md"

    # If a previous run for this conversation already wrote both files, hand
    # them back verbatim.  The pipeline is deterministic given the report, the
    # LLM, and the voice settings — and the LLM call is the slow / expensive
    # step.  This makes the endpoint idempotent (idempotency is good for
    # retries, and the UI can re-press the button without re-spending RAM).
    if mp3_path.exists() and transcript_path.exists():
        # Cache hit — instant, no synth, no download.  Tell the UI so it shows
        # "ready" rather than a false "downloading voice model…" note (W-08).
        await _emit(on_progress, {"stage": "cache_hit"})
        return mp3_path, transcript_path

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

    # --- Step 1: turn-script ------------------------------------------------
    await _emit(on_progress, {"stage": "preparing"})
    overview_text = report_to_overview_text(report, follow_ups)
    turns = await _generate_turn_script(overview_text, mode)
    backend = {
        "bundled": "bundled Kokoro",
        "speaches": "self-host Speaches",
        "openai": "paid OpenAI-compatible",
    }.get(provider, provider)

    # --- Step 1b: voice-model download (bundled only, first run) ------------
    # W-08: only signal "downloading voice model…" when a download is GENUINELY
    # about to happen — i.e. the bundled backend is selected AND its weight
    # files aren't on disk yet.  Remote backends and warm-cache runs skip this,
    # so the UI never shows the false download note.
    if not is_remote:
        from . import tts_local

        if not tts_local.model_files_present():
            # Real byte-level download progress (W-08).  ensure_model fetches the
            # missing weight files BEFORE synthesis and emits per-poll
            # `downloading_model` events carrying actual downloaded/total/pct — so
            # the UI shows a GENUINE progress bar, not a static note.  When the
            # weights are already on disk this whole block is skipped (no event).
            await tts_local.ensure_model(on_progress=on_progress)

    # --- Step 2: synthesize each turn to PCM --------------------------------
    # C1: normalize the text fed to TTS — strip markdown so the engine speaks
    # clean prose, not raw markup.  The transcript (Step 4) is built from the
    # ORIGINAL turn.text so it stays raw and readable as markdown.
    pcm_turns: list[Any] = []
    total_turns = len(turns)
    for i, turn in enumerate(turns):
        await _emit(
            on_progress,
            {"stage": "synthesizing", "current": i + 1, "total": total_turns},
        )
        voice = voice_a if turn.speaker == "A" else voice_b
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
                i + 1,
                len(turns),
                turn.speaker,
                turn.text,
            )
            continue
        try:
            if is_remote:
                pcm = await audio_overview._synthesize_remote(
                    tts_text,
                    voice,
                    remote_base,
                    api_key=remote_key,
                    model=remote_model,
                )
            else:
                pcm = await audio_overview._synthesize_local(tts_text, voice)
        except httpx.ConnectError as e:
            raise TtsBackendError(
                f"TTS endpoint unreachable at {remote_base} (turn {i + 1}/{len(turns)}): {e}"
            ) from e
        except Exception as e:
            raise TtsBackendError(
                f"TTS ({backend}) failed for turn {i + 1}/{len(turns)} (speaker {turn.speaker}): {e}"
            ) from e
        size = getattr(pcm, "size", len(pcm) if pcm is not None else 0)
        if pcm is None or size == 0:
            raise TtsBackendError(
                f"TTS ({backend}) returned empty audio for turn {i + 1}/{len(turns)} "
                f"(speaker {turn.speaker})"
            )
        pcm_turns.append(pcm)

    # --- Step 3: mix in PCM, encode the whole overview once -----------------
    await _emit(on_progress, {"stage": "mixing"})
    mixed_pcm = mix_pcm(
        pcm_turns, silence_ms=SILENCE_MS_DEFAULT, sample_rate=audio_overview.TTS_SAMPLE_RATE
    )
    mixed_mp3 = encode_mp3(mixed_pcm, sample_rate=audio_overview.TTS_SAMPLE_RATE)

    # --- Step 4: build transcript -------------------------------------------
    if mode == "single":
        # Single-speaker: no "Host A/B" labels — just the spoken text.
        transcript_lines: list[str] = [
            "# Audio Overview Transcript",
            "",
            f"Query: {report.query}",
            f"Voice: {voice_a}",
            f"Turns: {len(turns)}",
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
            "",
        ]
        for turn in turns:
            label = "Host A" if turn.speaker == "A" else "Host B"
            transcript_lines.append(f"**{label}:** {turn.text}")
            transcript_lines.append("")
    transcript_text = "\n".join(transcript_lines)

    # --- Step 5: write to the cid-scoped cache dir --------------------------
    # Atomic: write to .part then rename, so a partial file is never served.
    mp3_tmp = mp3_path.with_suffix(mp3_path.suffix + ".part")
    tr_tmp = transcript_path.with_suffix(transcript_path.suffix + ".part")
    mp3_tmp.write_bytes(mixed_mp3)
    tr_tmp.write_text(transcript_text, encoding="utf-8")
    mp3_tmp.replace(mp3_path)
    tr_tmp.replace(transcript_path)

    logger.info(
        "report audio generated: cid-artifact dir=%s mp3=%d bytes turns=%d backend=%s",
        out_dir,
        len(mixed_mp3),
        len(turns),
        backend,
    )
    return mp3_path, transcript_path


__all__ = [
    "TtsDisabled",
    "TtsBackendError",
    "TurnScriptError",
    "_normalize_for_tts",
    "generate_report_audio",
    "report_audio_cache_dir",
    "report_to_overview_text",
]
