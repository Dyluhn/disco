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


# ---- W-08: ensure_model download + real byte/percent progress --------------


async def test_ensure_model_noop_when_present(monkeypatch, tmp_path):
    """When both weight files already exist, ensure_model downloads nothing and
    emits ZERO progress events (the UI must not show a phantom download bar)."""
    monkeypatch.setenv("DISCO_TTS_DIR", str(tmp_path))
    monkeypatch.setattr(tts_local, "_data_dir", lambda: tmp_path)
    (tmp_path / "kokoro-v1.0.onnx").write_bytes(b"x")
    (tmp_path / "voices-v1.0.bin").write_bytes(b"y")

    def _fail_fetch(*a, **k):  # must never be called
        raise AssertionError("ensure_model fetched despite present weights")

    monkeypatch.setattr(tts_local, "_fetch", _fail_fetch)
    events: list[dict] = []
    await tts_local.ensure_model(on_progress=lambda e: events.append(e))
    assert events == []


async def test_ensure_model_downloads_with_real_progress(monkeypatch, tmp_path):
    """A missing weight file triggers a download whose REAL urllib byte counters
    are surfaced as `downloading_model` events with monotonically rising pct that
    reaches 100, plus correct file_index / file_total."""
    monkeypatch.setattr(tts_local, "_data_dir", lambda: tmp_path)

    def _fake_fetch(url, dest, sha, reporthook=None):
        # Simulate a 100-byte download reported in two 50-byte blocks, then the
        # file lands on disk — exactly the urlretrieve contract.
        if reporthook is not None:
            reporthook(1, 50, 100)
            reporthook(2, 50, 100)
        dest.write_bytes(b"z" * 100)

    monkeypatch.setattr(tts_local, "_fetch", _fake_fetch)
    events: list[dict] = []

    async def _on_progress(e: dict) -> None:
        events.append(e)

    await tts_local.ensure_model(on_progress=_on_progress)

    # Both files were "downloaded".
    assert (tmp_path / "kokoro-v1.0.onnx").exists()
    assert (tmp_path / "voices-v1.0.bin").exists()
    # Every event is a real downloading_model frame with the wire shape the FE
    # parses; pcts are within range and the final frame for each file hits 100.
    assert events, "expected at least one progress event"
    assert all(e["stage"] == "downloading_model" for e in events)
    assert all(0 <= e["pct"] <= 100 for e in events)
    assert {e["file_index"] for e in events} == {1, 2}
    assert all(e["file_total"] == 2 for e in events)
    # The last frame emitted for file 1 reports 100 % (downloaded == total).
    file1 = [e for e in events if e["file_index"] == 1]
    assert file1[-1]["pct"] == 100
    assert file1[-1]["downloaded"] == file1[-1]["total"] == 100


async def test_ensure_model_propagates_download_error(monkeypatch, tmp_path):
    """A checksum / network failure inside the threaded fetch propagates out of
    ensure_model so the route maps it to an honest error — never a silent skip."""
    monkeypatch.setattr(tts_local, "_data_dir", lambda: tmp_path)

    def _boom(url, dest, sha, reporthook=None):
        raise RuntimeError("checksum mismatch")

    monkeypatch.setattr(tts_local, "_fetch", _boom)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        await tts_local.ensure_model(on_progress=None)
