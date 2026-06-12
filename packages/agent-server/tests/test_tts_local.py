"""Lifecycle tests for the bundled in-process Kokoro TTS (RP-09).

The real ONNX model is ~0.3 GB and slow to load, so these tests mock `_KokoroEngine`
with a tiny fake — we're verifying the *lifecycle* (lazy load, PCM return, idle-TTL
unload, the no-op-when-cold path that the sweep relies on), not Kokoro inference.
The module keeps process-global state (`_engine`, `_last_used`), so each test resets
it first to stay independent.
"""

from __future__ import annotations

import numpy as np
import pytest
from disco.agent_server import tts_local

pytestmark = pytest.mark.asyncio


class _FakeEngine:
    """Stands in for _KokoroEngine: no download, no ONNX — just returns short PCM."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, text: str, voice: str) -> np.ndarray:
        self.calls.append((text, voice))
        # 240 samples (~10 ms @ 24 kHz) of a constant tone — enough to be non-empty.
        return np.full(240, 0.1, dtype=np.float32)


@pytest.fixture(autouse=True)
async def _reset_module_state():
    """Drop any resident engine + reset the idle clock before and after each test."""
    tts_local._engine = None
    tts_local._last_used = 0.0
    yield
    await tts_local.unload()
    tts_local._last_used = 0.0


async def test_lazy_load_then_synthesize(monkeypatch):
    monkeypatch.setattr(tts_local, "_KokoroEngine", _FakeEngine)
    assert tts_local.is_loaded() is False  # cold to start

    pcm = await tts_local.synthesize("hello world", "af_heart")

    assert isinstance(pcm, np.ndarray)
    assert pcm.dtype == np.float32
    assert pcm.size == 240
    assert tts_local.is_loaded() is True  # loaded on first use
    assert tts_local._last_used > 0.0  # idle clock started


async def test_engine_is_warmed_once(monkeypatch):
    """Two synth calls reuse one engine (warm singleton) rather than reloading."""
    made: list[_FakeEngine] = []

    def _factory():
        e = _FakeEngine()
        made.append(e)
        return e

    monkeypatch.setattr(tts_local, "_KokoroEngine", _factory)

    await tts_local.synthesize("one", "af_heart")
    await tts_local.synthesize("two", "af_bella")

    assert len(made) == 1  # only one engine constructed
    assert made[0].calls == [("one", "af_heart"), ("two", "af_bella")]


async def test_maybe_unload_when_cold_is_noop(monkeypatch):
    """The periodic sweep calls this every minute; when nothing is loaded it must
    be a cheap no-op and never construct the engine."""
    monkeypatch.setattr(
        tts_local,
        "_KokoroEngine",
        lambda: pytest.fail("engine must not be constructed by the idle sweep"),
    )
    assert tts_local.is_loaded() is False
    await tts_local.maybe_unload_if_idle(ttl_s=0)
    assert tts_local.is_loaded() is False


async def test_idle_unload_frees_model(monkeypatch):
    monkeypatch.setattr(tts_local, "_KokoroEngine", _FakeEngine)
    await tts_local.synthesize("hello", "af_heart")
    assert tts_local.is_loaded() is True

    # ttl_s=0 → any elapsed idle time trips the unload.
    await tts_local.maybe_unload_if_idle(ttl_s=0)
    assert tts_local.is_loaded() is False


async def test_idle_unload_respects_ttl(monkeypatch):
    """A large TTL means a just-used model is NOT unloaded by the sweep."""
    monkeypatch.setattr(tts_local, "_KokoroEngine", _FakeEngine)
    await tts_local.synthesize("hello", "af_heart")
    assert tts_local.is_loaded() is True

    await tts_local.maybe_unload_if_idle(ttl_s=10_000)
    assert tts_local.is_loaded() is True  # still warm — recently used


async def test_load_without_synth_is_still_unloadable(monkeypatch):
    """Regression (Fable): the idle clock is stamped at LOAD, not only after a
    successful synth — so a model that loads but whose synth then raises (e.g. a
    typo'd voice) is still reclaimable by the sweep, not pinned resident forever."""

    class _RaisingEngine(_FakeEngine):
        def synthesize(self, text: str, voice: str):  # type: ignore[override]
            raise RuntimeError("unknown voice")

    monkeypatch.setattr(tts_local, "_KokoroEngine", _RaisingEngine)
    with pytest.raises(RuntimeError):
        await tts_local.synthesize("hi", "af_typo")

    assert tts_local.is_loaded() is True  # engine constructed before the synth raised
    assert tts_local._last_used > 0.0  # clock stamped at load, despite no good synth

    await tts_local.maybe_unload_if_idle(ttl_s=0)
    assert tts_local.is_loaded() is False  # the sweep CAN reclaim it


async def test_explicit_unload(monkeypatch):
    monkeypatch.setattr(tts_local, "_KokoroEngine", _FakeEngine)
    await tts_local.synthesize("hello", "af_heart")
    assert tts_local.is_loaded() is True

    await tts_local.unload()
    assert tts_local.is_loaded() is False
    # A subsequent synth re-loads cleanly.
    pcm = await tts_local.synthesize("again", "af_heart")
    assert pcm.size == 240
    assert tts_local.is_loaded() is True
