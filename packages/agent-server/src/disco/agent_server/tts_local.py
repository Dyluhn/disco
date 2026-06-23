"""In-process Kokoro TTS — the bundled local voice backend (RP-09).

Mirrors the codebase doctrine already used by `retrieval/local_encoders.py`:
bundled, ONNX/CPU (deliberately NOT torch — light, no ROCm pain on AMD), lazy-
loaded on first use, offloaded to a thread so the single uvicorn event loop never
blocks. Adds an idle-TTL unload so the ~0.5 GB model is freed when unused — which
is what the Settings "disable TTS" toggle and inactivity actually reclaim.

Returns RAW PCM (float32, 24 kHz, mono) per turn — the caller mixes PCM and encodes
the whole overview to MP3 once (clean; no MP3-frame concatenation). Weights are
fetched on first use to the app data dir (pinned URL + SHA256), NOT bundled.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import urllib.request
from pathlib import Path

import numpy as np
from disco.core.env import disco_env

_LOG = logging.getLogger(__name__)

# Pinned Kokoro v1.0 ONNX weights + voice pack (kokoro-onnx model-files-v1.0),
# SHA256-verified on download so an upstream release move fails loudly rather than
# caching garbage. Checksums computed from the downloaded artifacts 2026-06-12.
_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
_MODEL_SHA = "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
_VOICES_SHA = "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"

# Output format is fixed by the Kokoro v1.0 model.
SAMPLE_RATE = 24000
CHANNELS = 1

_IDLE_TTL_S = 1800  # free the model after 30 min idle (mirrors SandboxSettings.idle_ttl_s)


def _data_dir() -> Path:
    base = disco_env("TTS_DIR") or os.path.expanduser("~/.cache/disco-tts")
    d = Path(base)
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_files_present() -> bool:
    """True when both bundled Kokoro weight files already exist on disk (so a
    generation run will NOT trigger a download).

    W-08: the audio pipeline keys the "downloading voice model…" UI status on
    this — the note is honest only when a download is genuinely about to happen.
    Mirrors the same exists-and-nonzero check `_fetch` uses to skip the download.
    """
    d = _data_dir()
    model = d / "kokoro-v1.0.onnx"
    voices = d / "voices-v1.0.bin"
    return all(p.exists() and p.stat().st_size > 0 for p in (model, voices))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, dest: Path, sha: str) -> None:
    """Download `url` → `dest` on first use and verify its SHA256. A checksum
    mismatch raises (never caches garbage from a moved/replaced release).

    Logs progress at 10 % intervals so the server log shows the download is
    alive — the first-run Kokoro download is ~300 MB and takes 30–90 s on a
    fast connection (WALK-13 / D1).
    """
    if dest.exists() and dest.stat().st_size > 0:
        return
    _LOG.info(
        "TTS: downloading %s → %s (~300 MB, first run only — this may take a minute)",
        url,
        dest,
    )
    tmp = dest.with_suffix(dest.suffix + ".part")

    _last_bucket: list[int] = [-1]  # mutable closure for progress tracking

    def _hook(count: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        pct = min(100, count * block_size * 100 // total_size)
        bucket = (pct // 10) * 10  # log at 0, 10, 20, …, 100 %
        if bucket != _last_bucket[0]:
            _last_bucket[0] = bucket
            _LOG.info("TTS: download %d%% — %s", bucket, dest.name)

    urllib.request.urlretrieve(url, tmp, reporthook=_hook)  # noqa: S310 — pinned GitHub release URL
    got = _sha256(tmp)
    if got != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"TTS weight checksum mismatch for {url}: got {got}, want {sha}")
    tmp.rename(dest)


class _KokoroEngine:
    """Warm, single-instance Kokoro. Construction loads the ONNX session (slow);
    `synthesize` runs inference. Both are sync/CPU-bound — callers wrap in a thread."""

    def __init__(self) -> None:
        d = _data_dir()
        model = d / "kokoro-v1.0.onnx"
        voices = d / "voices-v1.0.bin"
        _fetch(_MODEL_URL, model, _MODEL_SHA)
        _fetch(_VOICES_URL, voices, _VOICES_SHA)
        from kokoro_onnx import Kokoro  # heavy import, deferred to first load

        self._k = Kokoro(str(model), str(voices))

    def synthesize(self, text: str, voice: str) -> np.ndarray:
        """Return float32 PCM samples at SAMPLE_RATE (mono). Raises on bad voice."""
        samples, sr = self._k.create(text, voice=voice, speed=1.0, lang="en-us")
        if sr != SAMPLE_RATE:  # defensive — the v1.0 model is fixed at 24 kHz
            raise RuntimeError(f"unexpected Kokoro sample rate {sr} (expected {SAMPLE_RATE})")
        return np.asarray(samples, dtype=np.float32)


# ---- module-level lazy singleton + lifecycle ------------------------------

_engine: _KokoroEngine | None = None
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(1)  # one synthesis at a time — bounds CPU vs the encoders
_last_used: float = 0.0


async def _get_engine() -> _KokoroEngine:
    """Lazy warm-once load under a lock (first call also downloads weights)."""
    global _engine, _last_used
    async with _lock:
        if _engine is None:
            _engine = await asyncio.to_thread(_KokoroEngine)
            # Stamp the idle clock at LOAD, not just after a successful synth — so a
            # model that loads but whose synth then raises (e.g. a typo'd voice) is
            # still reclaimable by the idle sweep, not pinned resident until restart.
            _last_used = time.monotonic()
    return _engine


async def synthesize(text: str, voice: str) -> np.ndarray:
    """Synthesize one turn → float32 PCM (24 kHz mono), off the event loop.

    Loads the model lazily on first call. CPU-bound inference runs in a thread so
    the agent-server's SSE streams / tool calls never stall."""
    global _last_used
    engine = await _get_engine()
    async with _sem:
        pcm = await asyncio.to_thread(engine.synthesize, text, voice)
    _last_used = time.monotonic()
    return pcm


async def unload() -> None:
    """Drop the model reference so the ORT session arena is freed. Called when the
    user disables TTS in Settings or the idle-TTL elapses. RAM reclamation is
    approximate (glibc may retain pages) but the arena goes."""
    global _engine
    async with _lock:
        if _engine is not None:
            _engine = None
            _LOG.info("TTS: model unloaded (freed)")


def is_loaded() -> bool:
    return _engine is not None


async def maybe_unload_if_idle(ttl_s: int = _IDLE_TTL_S) -> None:
    """Unload if loaded and idle past `ttl_s`. Cheap to call from a periodic sweep."""
    if _engine is not None and _last_used and (time.monotonic() - _last_used) > ttl_s:
        await unload()
