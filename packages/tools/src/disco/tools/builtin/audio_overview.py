"""Audio overview tool — two-voice TTS from a finished deep-research report.

Pipeline:
  0. Resolve TTS settings from ConfigStore (toggle / backend / voices). If the
     feature is disabled, fail soft so the model never loads (saves RAM).
  1. Accept the report text.
  2. Call an LLM (OpenAI-compatible chat completions) to generate a JSON
     turn-script: [{"speaker": "A"|"B", "text": "..."}].
  3. Validate the JSON schema + retry ONCE on malformed output.
  4. Synthesise each turn to float32 PCM @ 24 kHz — bundled in-process Kokoro
     (default, ONNX/CPU) or a remote Speaches endpoint, per Settings → Audio.
  5. Mix the per-turn PCM with inter-turn silence, then encode the whole
     overview to MP3 ONCE via _audio_mixer (one sample rate end to end).
  6. Write MP3 + transcript through ctx.sandbox.write_file (jailed).
  7. Return ToolOutcome with artifact paths.

Fail-soft: if the chosen TTS backend is unreachable/disabled, return
success=False with a clear message — never crash the loop.
"""

from __future__ import annotations

import io
import json
import os
import re
import wave
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ._audio_mixer import encode_mp3, mix_pcm
from ._tts_normalize import normalize_tts_text

# Kokoro v1.0 (and Speaches-Kokoro) output 24 kHz mono. Both backends produce PCM
# at this rate, so the overview is mixed in PCM and encoded to MP3 once.
TTS_SAMPLE_RATE = 24000

# ---- turn-script schema -----------------------------------------------------


class Turn(BaseModel):
    speaker: str = Field(pattern=r"^(A|B)$")
    text: str = Field(min_length=1)


@dataclass
class _ResolvedTts:
    """Resolved TTS settings for one overview run (the output of Step 0)."""

    provider: str
    backend: str
    voice_a: str
    voice_b: str
    is_remote: bool
    remote_base: str
    remote_key: str
    remote_model: str


_TURN_SCRIPT_PROMPT = """You are a podcast scriptwriter. Given a research report, write a short
two-host audio script covering the key insights. Host A is the lead analyst;
Host B is the co-host who asks follow-ups and adds colour.

Output ONLY a JSON array of turns. Each turn is an object with:
  "speaker": "A" or "B"
  "text": one or two sentences (natural spoken language, no markdown)

Keep turns conversational. ~12-20 turns total. Each turn's text should be
~100-200 tokens (a comfortable spoken sentence or two). Start with Host A
introducing the topic.

Report to convert:
{report_text}

Output ONLY valid JSON array, nothing else:"""

_SINGLE_SCRIPT_PROMPT = """You are an audio narrator. Given a research report, write an honest,
thorough single-voice walkthrough of the key findings.
Cover the main insights, openly discuss any gaps or tensions in the sources,
and give the listener a balanced, honest assessment of what the research shows.

Output ONLY a JSON array of turns. Each turn is an object with:
  "speaker": "A"
  "text": two to three sentences (natural spoken language, no markdown)

Keep it informative and conversational. ~10-16 turns total. Each turn should be
~100-200 tokens. Start with a brief introduction to the topic.

Report to narrate:
{report_text}

Output ONLY valid JSON array, nothing else:"""


def _build_llm_payload(report_text: str, mode: str = "podcast") -> dict:
    from disco.agent_server.audio_config import LLM_MODEL

    prompt = _SINGLE_SCRIPT_PROMPT if mode == "single" else _TURN_SCRIPT_PROMPT
    return {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt.format(report_text=report_text),
            }
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
    }


async def _call_llm(payload: dict, llm_url: str) -> str:
    """Call the LLM to generate the turn-script. Returns raw response text."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> str:
    """Extract a JSON array from LLM output that may have surrounding text."""
    # Try direct parse first
    text = text.strip()
    if text.startswith("["):
        # Find the matching closing bracket
        depth = 0
        end = 0
        for i, ch in enumerate(text):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            return text[:end]
    # Try markdown code block
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Last resort: find first [ and last ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _validate_turn_script(raw_json: str) -> tuple[list[Turn] | None, str | None]:
    """Validate the JSON turn-script. Returns (turns, error_message)."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"

    if not isinstance(data, list):
        return None, "Turn script must be a JSON array of turns"

    if len(data) < 2:
        return None, "Turn script must have at least 2 turns"

    turns: list[Turn] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"Turn {i} is not an object"
        speaker = item.get("speaker")
        if speaker not in ("A", "B"):
            return None, f"Turn {i}: speaker must be 'A' or 'B', got {speaker!r}"
        text = item.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return None, f"Turn {i}: text must be a non-empty string"
        turns.append(Turn(speaker=speaker, text=text.strip()))

    return turns, None


def _validate_turn_script_single(raw_json: str) -> tuple[list[Turn] | None, str | None]:
    """Validate a single-speaker turn script (all speakers must be 'A').

    Relaxed rules vs :func:`_validate_turn_script`:
    - At least **1** turn (not 2) — a single-voice monologue may be terse.
    - Speaker must be ``'A'``; any ``'B'`` that slips through the prompt is
      silently coerced to ``'A'`` so the whole overview uses ``voice_a``.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"

    if not isinstance(data, list):
        return None, "Turn script must be a JSON array of turns"

    if len(data) < 1:
        return None, "Turn script must have at least 1 turn"

    turns: list[Turn] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"Turn {i} is not an object"
        speaker = item.get("speaker", "A")
        if speaker not in ("A", "B"):
            return None, f"Turn {i}: speaker must be 'A' or 'B', got {speaker!r}"
        text = item.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return None, f"Turn {i}: text must be a non-empty string"
        # Coerce any 'B' to 'A' — single-speaker always maps to voice_a.
        turns.append(Turn(speaker="A", text=text.strip()))

    return turns, None


async def _synthesize_local(text: str, voice: str) -> Any:
    """Bundled in-process Kokoro (default). Returns float32 PCM @ 24 kHz. Lazy-
    imports the agent-server TTS module (loads the model on first use)."""
    from disco.agent_server.tts_local import synthesize

    text = _text_for_synthesis(text)
    return await synthesize(text, voice)


async def _synthesize_remote(
    text: str,
    voice: str,
    base_url: str,
    *,
    api_key: str = "",
    model: str = "",
) -> Any:
    """Remote OpenAI-compatible `/v1/audio/speech` tier — serves BOTH the self-host
    `speaches` provider (keyless) and the paid `openai` provider (Bearer key + a
    `model`). Request WAV (so we mix in PCM like the local path) and decode to float32
    mono. Returns a numpy float32 array.

    The whole overview is mixed at TTS_SAMPLE_RATE (24 kHz) mono, so we REQUIRE the
    remote stream to match — a mismatched rate/channel count would otherwise be mixed
    as-is and play back pitch-shifted/garbled. Speaches-Kokoro and OpenAI `tts-1`
    both emit 24 kHz mono; we assert it rather than trust the docstring."""
    import numpy as np

    text = _text_for_synthesis(text)
    payload: dict[str, Any] = {"input": text, "voice": voice, "response_format": "wav"}
    if model:  # OpenAI requires a model id; Speaches ignores/defaults it
        payload["model"] = model
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # Accept a base_url with OR without a trailing /v1 (users paste either): append
    # the right suffix so https://api.openai.com and http://speaches:8000/v1 both work.
    root = base_url.rstrip("/")
    url = f"{root}/audio/speech" if root.endswith("/v1") else f"{root}/v1/audio/speech"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        with wave.open(io.BytesIO(resp.content), "rb") as w:
            frames = w.readframes(w.getnframes())
            width = w.getsampwidth()
            rate = w.getframerate()
            channels = w.getnchannels()
    if channels != 1 or rate != TTS_SAMPLE_RATE:
        raise RuntimeError(
            f"TTS endpoint returned {rate} Hz / {channels}ch; the mixer needs "
            f"{TTS_SAMPLE_RATE} Hz mono. Configure the endpoint to emit 24 kHz mono."
        )
    if width == 2:
        return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if width == 4:
        return np.frombuffer(frames, dtype="<f4").astype(np.float32)
    raise RuntimeError(f"unsupported WAV sample width {width} from the TTS endpoint")


def _text_for_synthesis(text: str) -> str:
    """Normalize text at the provider-agnostic TTS choke point.

    If normalization removes everything, fall back to the original stripped text
    so providers still receive a non-empty prompt when the caller supplied one.
    """
    normalized = normalize_tts_text(text)
    return normalized or text.strip()


# ---- tool implementation ----------------------------------------------------


class AudioOverviewArgs(BaseModel):
    report_text: str = Field(
        description="The full text of the finished research report to convert to audio."
    )
    filename: str = Field(
        default="audio_overview",
        description="Base filename (without extension) for MP3 + transcript outputs.",
    )


class AudioOverviewTool:
    definition = ToolDef(
        name="audio_overview",
        description=(
            "Generate a two-voice audio overview (MP3 + transcript) from a finished "
            "deep-research report. Two AI hosts discuss the key findings in a "
            "conversational back-and-forth. Produces an MP3 file and a transcript "
            "text file in the workspace."
        ),
        args_model=AudioOverviewArgs,
        needs=frozenset({Capability.NETWORK, Capability.FILESYSTEM}),
        base_risk=None,
        runs_in="sandbox",  # HTTP calls + sandbox-backed file writes
        read_only=False,
    )

    async def _resolve_tts_settings(
        self, tts: Any, speaches_url: str
    ) -> tuple[_ResolvedTts | None, ToolOutcome | None]:
        """Step 0: resolve the toggle / backend / voices from ConfigStore TTS
        settings. Returns (resolved, None) when enabled, or (None, fail_soft) when
        disabled — the disabled path unloads the bundled model first (RAM gate)."""
        if not tts.enabled:
            # Wire (D5): the "Off" toggle is supposed to free the Kokoro model
            # immediately, not wait for the agent-server's idle-TTL sweep
            # (PMX_TTS_IDLE_TTL_S, default 30 min). The bundled engine is
            # process-global in `disco.agent_server.tts_local`; `unload()` is a
            # no-op when nothing is loaded, so it's safe to call here. Lazy-
            # imported + suppressed so the tools package stays importable even
            # where the agent-server (which hosts the bundled engine) isn't.
            try:
                from disco.agent_server import tts_local as _tts_local

                await _tts_local.unload()
            except ImportError:
                pass  # agent-server / bundled TTS not importable — nothing to unload
            return None, ToolOutcome(
                success=False,
                content=(
                    "Audio overview is disabled in Settings → Audio. Enable it "
                    "(bundled in-process, self-host, or paid endpoint) to generate "
                    "overviews."
                ),
                error="tts disabled",
            )
        # Three-tier provider resolution. `speaches` (self-host) and `openai` (paid)
        # share the OpenAI-compatible HTTP client; they differ only by the base_url
        # default and whether a Bearer key + model are sent. `bundled` uses none of
        # these (in-process Kokoro). The key is resolved from the ENV-VAR NAME the
        # user set in Settings (never the raw key) — same convention as search/
        # extraction/model providers.
        _OPENAI_DEFAULT_BASE = "https://api.openai.com"
        remote_base = remote_key = remote_model = ""
        if tts.provider == "speaches":
            remote_base = (tts.base_url or speaches_url).rstrip("/")
        elif tts.provider == "openai":
            remote_base = (tts.base_url or _OPENAI_DEFAULT_BASE).rstrip("/")
            remote_key = os.environ.get(tts.api_key_env, "") if tts.api_key_env else ""
            remote_model = tts.model or "tts-1"
        _BACKEND_LABEL = {
            "bundled": "bundled Kokoro",
            "speaches": "self-host Speaches",
            "openai": "paid OpenAI-compatible",
        }
        resolved = _ResolvedTts(
            provider=tts.provider,
            backend=_BACKEND_LABEL.get(tts.provider, tts.provider),
            voice_a=tts.voice_a,
            voice_b=tts.voice_b,
            is_remote=tts.provider != "bundled",
            remote_base=remote_base,
            remote_key=remote_key,
            remote_model=remote_model,
        )
        return resolved, None

    async def _generate_turn_script(
        self, report_text: str, llm_url: str
    ) -> tuple[list[Turn] | None, ToolOutcome | None]:
        """Steps 1 & 2: call the LLM for a JSON turn-script, validate it, and retry
        ONCE on malformed output. Returns (turns, None) on success or
        (None, failure_outcome) on any unrecoverable error."""
        payload = _build_llm_payload(report_text)
        try:
            raw_response = await _call_llm(payload, llm_url)
        except Exception as e:
            return None, ToolOutcome(
                success=False,
                content=f"LLM call failed while generating turn-script: {e}",
                error=str(e),
            )

        raw_json = _extract_json(raw_response)
        turns, error = _validate_turn_script(raw_json)

        # One retry on malformed output.
        if error is not None:
            retry_payload = _build_llm_payload(report_text)
            retry_payload["messages"].append(
                {"role": "assistant", "content": raw_response}
            )
            retry_payload["messages"].append(
                {
                    "role": "user",
                    "content": f"Your JSON output was invalid: {error}\n\n"
                    f"Please fix the errors and output ONLY a valid JSON array "
                    f"of turns. Each turn must have 'speaker' (A or B) and "
                    f"'text' (non-empty string).",
                }
            )
            try:
                raw_response2 = await _call_llm(retry_payload, llm_url)
            except Exception as e:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"Turn-script validation failed: {error}\n"
                        f"Retry LLM call also failed: {e}"
                    ),
                    error=error,
                )

            raw_json2 = _extract_json(raw_response2)
            turns, error2 = _validate_turn_script(raw_json2)
            if error2 is not None:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"Turn-script validation failed after retry.\n"
                        f"First error: {error}\n"
                        f"Retry error: {error2}"
                    ),
                    error=error2,
                )

        return turns, None

    async def _synthesize_turns(
        self, turns: list[Turn], cfg: _ResolvedTts
    ) -> tuple[list[Any] | None, ToolOutcome | None]:
        """Step 3: synthesise each turn to float32 mono PCM @ 24 kHz via the bundled
        (in-process Kokoro) or remote (Speaches / OpenAI-compatible) backend. Returns
        (pcm_turns, None) on success or (None, failure_outcome) on the first error."""
        pcm_turns: list[Any] = []
        for i, turn in enumerate(turns):
            voice = cfg.voice_a if turn.speaker == "A" else cfg.voice_b
            try:
                if cfg.is_remote:
                    pcm = await _synthesize_remote(
                        turn.text, voice, cfg.remote_base,
                        api_key=cfg.remote_key, model=cfg.remote_model,
                    )
                else:
                    pcm = await _synthesize_local(turn.text, voice)
            except httpx.ConnectError:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"The TTS endpoint is unreachable at {cfg.remote_base}. "
                        f"Turn {i + 1}/{len(turns)} could not be synthesised. "
                        f"Switch Settings → Audio to bundled, or start/fix the endpoint."
                    ),
                    error=f"tts endpoint offline: {cfg.remote_base}",
                )
            except Exception as e:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"TTS ({cfg.backend}) failed for turn {i + 1}/{len(turns)} "
                        f"(speaker {turn.speaker}): {e}"
                    ),
                    error=str(e),
                )
            if pcm is None or getattr(pcm, "size", len(pcm) if pcm is not None else 0) == 0:
                return None, ToolOutcome(
                    success=False,
                    content=(
                        f"TTS ({cfg.backend}) returned empty audio for turn "
                        f"{i + 1}/{len(turns)} (speaker {turn.speaker})"
                    ),
                    error="Empty audio from TTS",
                )
            pcm_turns.append(pcm)
        return pcm_turns, None

    def _mix_and_encode(self, pcm_turns: list[Any], silence_ms: int) -> bytes:
        """Step 4: mix the per-turn PCM with inter-turn silence (one sample rate end
        to end), then encode the whole overview to MP3 exactly once."""
        mixed_pcm = mix_pcm(
            pcm_turns, silence_ms=silence_ms, sample_rate=TTS_SAMPLE_RATE
        )
        return encode_mp3(mixed_pcm, sample_rate=TTS_SAMPLE_RATE)

    async def _write_outputs(
        self,
        ctx: ToolContext,
        filename: str,
        mixed_mp3: bytes,
        transcript_text: str,
        cfg: _ResolvedTts,
        *,
        turn_count: int,
        silence_ms: int,
    ) -> ToolOutcome:
        """Step 6: write the MP3 + transcript through the jailed sandbox and build the
        success ToolOutcome (artifact paths + structured metadata)."""
        mp3_path = f"{filename}.mp3"
        transcript_path = f"{filename}.md"
        assert ctx.sandbox is not None, "audio_overview requires a sandbox for file writes"
        await ctx.sandbox.write_file(mp3_path, mixed_mp3)
        await ctx.sandbox.write_file(transcript_path, transcript_text.encode("utf-8"))

        return ToolOutcome(
            success=True,
            content=(
                f"Audio overview generated.\n"
                f"  MP3: {mp3_path} ({len(mixed_mp3)} bytes)\n"
                f"  Transcript: {transcript_path}\n"
                f"  Turns: {turn_count} | Voices: {cfg.voice_a} (A) + {cfg.voice_b} (B)\n"
                f"  Backend: {cfg.backend} | Silence between turns: {silence_ms}ms"
            ),
            artifacts=[mp3_path, transcript_path],
            structured={
                "mp3_path": mp3_path,
                "transcript_path": transcript_path,
                "turn_count": turn_count,
                "mp3_bytes": len(mixed_mp3),
                "voice_a": cfg.voice_a,
                "voice_b": cfg.voice_b,
                "backend": cfg.provider,
                "silence_ms": silence_ms,
            },
        )

    def _build_transcript(
        self, turns: list[Turn], voice_a: str, voice_b: str
    ) -> str:
        """Step 5: render the turn-script to a Markdown transcript."""
        transcript_lines: list[str] = [
            "# Audio Overview Transcript",
            "",
            f"Voices: Host A = {voice_a}, Host B = {voice_b}",
            f"Turns: {len(turns)}",
            "",
        ]
        for turn in turns:
            label = "Host A" if turn.speaker == "A" else "Host B"
            transcript_lines.append(f"**{label}:** {turn.text}")
            transcript_lines.append("")
        return "\n".join(transcript_lines)

    async def run(self, args: AudioOverviewArgs, ctx: ToolContext) -> ToolOutcome:
        from disco.agent_server.audio_config import (
            LLM_URL,
            SILENCE_MS_DEFAULT,
            SPEACHES_URL,
        )
        from disco.core.llm import ConfigStore

        report_text = args.report_text
        filename = args.filename

        # --- Step 0: Resolve TTS settings (toggle + backend + voices) -------
        # ConfigStore is the single source of truth; the agent-server reloads it
        # per request, so a Settings change drives the NEXT overview. `enabled`
        # is the RAM gate — when off we fail soft rather than loading the model.
        tts = ConfigStore().load().tts
        tts_cfg, disabled_outcome = await self._resolve_tts_settings(tts, SPEACHES_URL)
        if disabled_outcome is not None:
            return disabled_outcome
        assert tts_cfg is not None  # enabled path resolves a config
        voice_a = tts_cfg.voice_a
        voice_b = tts_cfg.voice_b

        # --- Steps 1 & 2: Generate turn-script via LLM (+ one retry) ---------
        turns, gen_failure = await self._generate_turn_script(report_text, LLM_URL)
        if gen_failure is not None:
            return gen_failure

        # --- Step 3: Synthesise each turn to PCM ----------------------------
        assert turns is not None  # validated above
        pcm_turns, synth_failure = await self._synthesize_turns(turns, tts_cfg)
        if synth_failure is not None:
            return synth_failure
        assert pcm_turns is not None

        # --- Step 4: Mix in PCM, then encode the whole overview to MP3 once --
        mixed_mp3 = self._mix_and_encode(pcm_turns, SILENCE_MS_DEFAULT)

        # --- Step 5: Build transcript ---------------------------------------
        transcript_text = self._build_transcript(turns, voice_a, voice_b)

        # --- Step 6: Write through sandbox (jailed) + return outcome ---------
        return await self._write_outputs(
            ctx,
            filename,
            mixed_mp3,
            transcript_text,
            tts_cfg,
            turn_count=len(turns),
            silence_ms=SILENCE_MS_DEFAULT,
        )
