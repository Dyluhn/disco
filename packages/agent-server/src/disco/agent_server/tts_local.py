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
import contextlib
import hashlib
import inspect
import logging
import os
import time
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np

try:  # POSIX advisory file lock — present on Linux/macOS (the deploy targets).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — non-POSIX (e.g. Windows); fall back to no x-proc lock
    _fcntl = None  # type: ignore[assignment]
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


@contextlib.contextmanager
def _download_lock(dest: Path) -> Iterator[None]:
    """Single-flight guard around the first-run download of `dest`.

    Holds an EXCLUSIVE advisory lock on a sidecar ``<dest>.lock`` file for the
    duration of one download. A second caller — another asyncio task on the same
    loop (each `_fetch` runs in its own worker thread), a different thread, OR a
    different agent-server process — BLOCKS here until the holder finishes, then
    re-checks presence and skips re-downloading. `flock` is per-open-file-
    description, so separate `open()`s contend even within one process.

    On a non-POSIX platform (no `fcntl`) this degrades to a no-op guard; the
    atomic os.replace in `_fetch` still prevents a torn cache file.
    """
    if _fcntl is None:  # pragma: no cover — non-POSIX fallback
        yield
        return
    lock_path = dest.with_suffix(dest.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        _fcntl.flock(lock_f, _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(lock_f, _fcntl.LOCK_UN)


def _fetch(
    url: str,
    dest: Path,
    sha: str,
    reporthook: Callable[[int, int, int], None] | None = None,
) -> None:
    """Download `url` → `dest` on first use and verify its SHA256. A checksum
    mismatch raises (never caches garbage from a moved/replaced release).

    SINGLE-FLIGHT + ATOMIC: the download is serialized by an advisory file lock
    (`_download_lock`) so two concurrent callers never download the same weight
    file at once, and the verified bytes are published with an atomic
    ``os.replace`` from a per-process temp — so a concurrent reader never sees a
    half-written file and a failed/partial download leaves NO cache file behind.

    Logs progress at 10 % intervals so the server log shows the download is
    alive — the first-run Kokoro download is ~300 MB and takes 30–90 s on a
    fast connection (WALK-13 / D1).

    `reporthook(count, block_size, total_size)` — optional EXTRA hook (same
    signature urllib uses) so a caller (`ensure_model`) can surface REAL
    byte/percent progress to the UI in addition to the server-log buckets.
    """
    # Fast path: already present (no lock needed — the file only ever appears via
    # the atomic replace below, so a non-zero size means a complete file).
    if dest.exists() and dest.stat().st_size > 0:
        return

    with _download_lock(dest):
        # Re-check UNDER the lock: a holder we just waited on may have completed
        # the download — never re-fetch (idempotent / no double-download).
        if dest.exists() and dest.stat().st_size > 0:
            return

        _LOG.info(
            "TTS: downloading %s → %s (~300 MB, first run only — this may take a minute)",
            url,
            dest,
        )
        # Per-process temp so a stale lock (crashed holder) can't make two
        # processes collide on one `.part` name; the os.replace is the real
        # atomicity guarantee.
        tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.part")

        _last_bucket: list[int] = [-1]  # mutable closure for progress tracking

        def _hook(count: int, block_size: int, total_size: int) -> None:
            if reporthook is not None:
                reporthook(count, block_size, total_size)
            if total_size <= 0:
                return
            pct = min(100, count * block_size * 100 // total_size)
            bucket = (pct // 10) * 10  # log at 0, 10, 20, …, 100 %
            if bucket != _last_bucket[0]:
                _last_bucket[0] = bucket
                _LOG.info("TTS: download %d%% — %s", bucket, dest.name)

        try:
            urllib.request.urlretrieve(url, tmp, reporthook=_hook)  # noqa: S310 — pinned GitHub release URL
            got = _sha256(tmp)
            if got != sha:
                raise RuntimeError(
                    f"TTS weight checksum mismatch for {url}: got {got}, want {sha}"
                )
            os.replace(tmp, dest)  # atomic publish of the verified file
        finally:
            # Never leave a partial/failed temp behind (corruption guard). The
            # final cache path was either atomically published or never created.
            with contextlib.suppress(FileNotFoundError):
                Path(tmp).unlink()


# ---- W-08: real byte-level model download with progress --------------------

# A download-progress callback receives small JSON-able dicts describing the
# CURRENT state of the weight-file download (bytes downloaded / total / percent
# / which file). It may be sync or async; both are awaited safely. `None`
# disables it. The numbers are the REAL urllib byte counts — never fabricated.
DownloadProgress = Callable[[dict[str, Any]], Awaitable[None] | None]


async def ensure_model(on_progress: DownloadProgress | None = None) -> None:
    """Download any MISSING Kokoro weight file into the cache dir, emitting REAL
    byte-level progress via `on_progress`. A no-op (and zero progress events)
    when both files are already present.

    This pre-fetches the weights BEFORE synthesis so the first `synthesize()`
    loads from disk instead of blocking silently on a ~300 MB fetch with no UI
    signal. The blocking `urlretrieve` runs in a thread; the async side polls the
    shared byte counters every ~300 ms and emits a `downloading_model` event so
    the progress bar advances smoothly without flooding the SSE stream.

    Each event: ``{"stage": "downloading_model", "file": <name>, "downloaded":
    <bytes>, "total": <bytes>, "pct": <0-100>, "file_index": n, "file_total":
    m}``. `total` may be 0 briefly until the server reports Content-Length.
    """
    d = _data_dir()
    files = [
        ("kokoro-v1.0.onnx", _MODEL_URL, _MODEL_SHA),
        ("voices-v1.0.bin", _VOICES_URL, _VOICES_SHA),
    ]
    missing = [
        (name, url, sha)
        for (name, url, sha) in files
        if not ((d / name).exists() and (d / name).stat().st_size > 0)
    ]
    file_total = len(missing)
    for idx, (name, url, sha) in enumerate(missing, start=1):
        dest = d / name
        counters = {"downloaded": 0, "total": 0}

        def _hook(count: int, block_size: int, total_size: int, _c=counters) -> None:
            _c["total"] = max(0, total_size)
            done = count * block_size
            _c["downloaded"] = min(done, total_size) if total_size > 0 else done

        async def _emit(_name=name, _idx=idx, _c=counters) -> None:
            if on_progress is None:
                return
            total = _c["total"]
            downloaded = _c["downloaded"]
            pct = min(100, downloaded * 100 // total) if total > 0 else 0
            event = {
                "stage": "downloading_model",
                "file": _name,
                "downloaded": downloaded,
                "total": total,
                "pct": pct,
                "file_index": _idx,
                "file_total": file_total,
            }
            res = on_progress(event)
            if inspect.isawaitable(res):
                await res

        task = asyncio.create_task(asyncio.to_thread(_fetch, url, dest, sha, _hook))
        # Emit a 0 % frame immediately so the bar appears the instant the
        # download starts, then poll while the thread downloads.
        await _emit()
        while not task.done():
            await asyncio.sleep(0.3)
            await _emit()
        await task  # propagate a download / checksum-mismatch error to the caller
        # Final 100 % frame. Covers two cases: (a) the poll stopped just shy of the
        # last block; (b) a SINGLE-FLIGHT WAITER whose `_fetch` blocked on the lock
        # then found the file already complete — its reporthook never ran, so seed
        # both counters from the on-disk size to show a clean 100 % rather than 0 %.
        final_size = dest.stat().st_size if dest.exists() else counters["downloaded"]
        if counters["total"] <= 0:
            counters["total"] = final_size
        counters["downloaded"] = counters["total"] or final_size
        await _emit()


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
