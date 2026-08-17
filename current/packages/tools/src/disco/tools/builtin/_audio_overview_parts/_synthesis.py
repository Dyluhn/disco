"""Local/remote TTS synthesis: text -> float32 mono PCM @ 24 kHz.

Extracted from ``audio_overview.py``. ``_synthesize_local`` deliberately imports
``disco.agent_server.tts_local`` INSIDE the function body — this is the upward
``disco.tools`` -> ``disco.agent_server`` import, whitelisted only as a tracked
debt edge (see ``.importlinter``) and only in this lazy, function-local form.
Never hoist this import to module level and never add a new one.

``_synthesize_local``/``_synthesize_remote`` are re-exported as module-level
attributes of ``audio_overview`` (and imported directly by ``report_audio.py`` /
``probes.py`` / tests) precisely so ``monkeypatch.setattr``/``mock.patch``
targeting ``disco.tools.builtin.audio_overview._synthesize_local`` etc. keep
working: the real callers (``AudioOverviewTool._synthesize_turns``) resolve the
name at call time through ``audio_overview``'s own module globals.
"""

from __future__ import annotations

import io
import wave
from typing import Any

import httpx

from .._tts_normalize import normalize_tts_text

# Kokoro v1.0 (and Speaches-Kokoro) output 24 kHz mono. Both backends produce PCM
# at this rate, so the overview is mixed in PCM and encoded to MP3 once.
TTS_SAMPLE_RATE = 24000


async def _synthesize_local(text: str, voice: str) -> Any:
    """Bundled in-process Kokoro (default). Returns float32 PCM @ 24 kHz. Lazy-
    imports the agent-server TTS module (loads the model on first use)."""
    from .. import audio_overview

    text = _text_for_synthesis(text)
    return await audio_overview._tts_local_synthesize(text, voice)


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
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0), trust_env=False, follow_redirects=False
    ) as client:
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
