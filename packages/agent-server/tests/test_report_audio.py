"""Tests for the server-side report audio overview endpoint (D1 un-stub).

The pipeline reuses packages/tools/src/disco/tools/builtin/audio_overview.py
helpers.  We don't want to load the bundled Kokoro model (~0.3 GB) in tests,
so we monkeypatch `disco.tools.builtin.audio_overview._synthesize_local` and
`_synthesize_remote` to return tiny fake PCM.  The mixer + encoder + the
LLM turn-script step ARE exercised — that's the part we want to keep honest.

Acceptance covered:
  1. 200 + a real, playable mp3 path for a seeded ReportEvent
  2. 404 — no ReportEvent for the conversation
  3. 503 — TTS disabled in Settings → Audio
  4. 502 — TTS backend failure (synth returns empty)
  5. GET — serves the mp3 / transcript with the right content-type
  6. GET — JAIL: traversal / unknown name / wrong cid all 404
  7. Idempotency: a second POST returns 200 without re-running the synth
     (the cid-scoped cache is the source of truth after the first run)
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server import report_audio as report_audio_mod
from disco.core import (
    EventSource,
    ReportEvent,
    ReportSection,
    SqliteEventStore,
)
from disco.core.llm import TtsSettings
from fastapi.testclient import TestClient


# ---- helpers --------------------------------------------------------------


def _make_report() -> ReportEvent:
    return ReportEvent(
        source=EventSource.AGENT,
        query="What is the airspeed velocity of an unladen swallow?",
        summary="African and European swallows differ. Airspeed ~11 m/s and ~8 m/s.",
        sections=[
            ReportSection(
                id="s0",
                title="African Swallow",
                markdown="The African swallow cruises at **11 m/s** with 5-7 flaps per second.",
                cited_passage_ids=["p0"],
                confidence="high",
                disputed_notes=[],
            ),
            ReportSection(
                id="s1",
                title="European Swallow",
                markdown="The European swallow is smaller and slower: **8 m/s**.",
                cited_passage_ids=["p1"],
                confidence="mixed",
                disputed_notes=[],
            ),
        ],
        passages=[
            {"id": "p0", "source_title": "Avian Speed DB", "source_url": "https://example.com/p0"},
            {"id": "p1", "source_title": "Ornithology Journal", "source_url": "https://example.com/p1"},
        ],
        all_hits=[],
        unsupported_count=1,
        bounded_by="sources",
        depth_tier="standard_deep",
    )


def _fake_pcm(seconds: float = 0.1, sample_rate: int = 24000) -> np.ndarray:
    """Tiny float32 PCM — enough to mix and encode but tiny enough not to be slow."""
    n = int(seconds * sample_rate)
    return (np.ones(n, dtype=np.float32) * 0.1)


# A canned, well-formed turn-script — the LLM call is monkeypatched to return
# this verbatim.  Validation should pass first try (no retry path).
_GOOD_TURN_SCRIPT = """[
  {"speaker": "A", "text": "Welcome to today's deep research overview."},
  {"speaker": "B", "text": "Today we're looking at swallow airspeed — fun topic."},
  {"speaker": "A", "text": "African swallows cruise at eleven meters per second."},
  {"speaker": "B", "text": "And the European swallow is slower — about eight."}
]"""


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the LLM call and per-turn synth with fast, in-memory stubs.

    The mixer / encoder / pipeline orchestration in `generate_report_audio` are
    the parts we want to keep real; the network calls and the heavy model
    load are exactly what we want to avoid in unit tests.
    """
    from disco.tools.builtin import audio_overview

    async def _fake_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        return _GOOD_TURN_SCRIPT

    async def _fake_local(text: str, voice: str) -> np.ndarray:  # noqa: ARG001
        return _fake_pcm()

    async def _fake_remote(
        text: str, voice: str, base_url: str, *, api_key: str = "", model: str = ""  # noqa: ARG001
    ) -> np.ndarray:
        return _fake_pcm()

    monkeypatch.setattr(audio_overview, "_call_llm", _fake_llm)
    monkeypatch.setattr(audio_overview, "_synthesize_local", _fake_local)
    monkeypatch.setattr(audio_overview, "_synthesize_remote", _fake_remote)


# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


@pytest.fixture
def client(store: SqliteEventStore) -> TestClient:
    runtime = ConversationRuntime(store)
    return TestClient(create_app(store, runtime=runtime))


@pytest.fixture
def tts_enabled(monkeypatch: pytest.MonkeyPatch) -> TtsSettings:
    """A TTS settings object with TTS enabled (bundled Kokoro by default)."""
    s = TtsSettings(enabled=True, provider="bundled")
    _install_fakes(monkeypatch)
    return s


@pytest.fixture
def tts_disabled(monkeypatch: pytest.MonkeyPatch) -> TtsSettings:
    """A TTS settings object with TTS explicitly disabled."""
    s = TtsSettings(enabled=False, provider="bundled")
    _install_fakes(monkeypatch)
    return s


def _seed_report(store: SqliteEventStore, cid: str, report: ReportEvent | None = None) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(store.append(cid, report or _make_report()))
    finally:
        loop.close()


def _create_conv(client: TestClient) -> str:
    r = client.post("/conversations", json={"owner_id": "local"})
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


# Override the ConfigStore's tts settings for the duration of a test. The route
# reads `ConfigStore().load().tts`, so we monkeypatch that to return our
# configured TtsSettings instance.
@pytest.fixture
def configure_tts(monkeypatch: pytest.MonkeyPatch):
    from disco.core.llm import ConfigStore

    def _set(tts: TtsSettings) -> None:
        def _loader(self):  # noqa: ANN001
            class _Cfg:
                pass

            cfg = _Cfg()
            cfg.tts = tts
            return cfg

        monkeypatch.setattr(ConfigStore, "load", _loader)

    return _set


# ---- tests ----------------------------------------------------------------


def test_generate_report_audio_in_process(
    store: SqliteEventStore, configure_tts, tts_enabled, tmp_path, monkeypatch
) -> None:
    """Direct (non-HTTP) test of the pipeline — exercises every step in-process
    with a tiny synth, and writes the artifacts to `tmp_path`."""
    configure_tts(tts_enabled)
    out_dir = tmp_path / "conv_abc" / "audio"
    mp3_path, transcript_path = asyncio.run(
        report_audio_mod.generate_report_audio(
            _make_report(), tts_settings=tts_enabled, out_dir=out_dir
        )
    )
    assert mp3_path.exists() and mp3_path.stat().st_size > 0
    assert transcript_path.exists() and transcript_path.stat().st_size > 0
    # The mp3 starts with the LAME/MP3 header bytes (ID3 or MPEG sync).
    head = mp3_path.read_bytes()[:4]
    assert head[:3] == b"ID3" or head[0] == 0xFF, f"unexpected mp3 head: {head!r}"
    # Transcript mentions both voices and the query.
    tr = transcript_path.read_text(encoding="utf-8")
    assert "Audio Overview Transcript" in tr
    assert "Host A" in tr and "Host B" in tr
    assert "swallow" in tr.lower()


def test_generate_report_audio_disabled(
    configure_tts, tts_disabled, tmp_path
) -> None:
    """`tts.enabled = False` → :class:`TtsDisabled` (NOT a fake success)."""
    configure_tts(tts_disabled)
    out_dir = tmp_path / "conv_disabled" / "audio"
    with pytest.raises(report_audio_mod.TtsDisabled):
        asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(), tts_settings=tts_disabled, out_dir=out_dir
            )
        )
    # No files should be written.
    assert not (out_dir / "audio_overview.mp3").exists()


def test_generate_report_audio_synth_failure(
    configure_tts, tmp_path, monkeypatch
) -> None:
    """A synth that returns None → :class:`TtsBackendError`."""
    tts = TtsSettings(enabled=True, provider="bundled")
    configure_tts(tts)
    _install_fakes(monkeypatch)

    from disco.tools.builtin import audio_overview

    async def _bad_synth(text: str, voice: str) -> np.ndarray:  # noqa: ARG001
        return None  # simulates "empty audio from TTS"

    monkeypatch.setattr(audio_overview, "_synthesize_local", _bad_synth)

    out_dir = tmp_path / "conv_synth_fail" / "audio"
    with pytest.raises(report_audio_mod.TtsBackendError):
        asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(), tts_settings=tts, out_dir=out_dir
            )
        )


def test_endpoint_404_no_conversation(client: TestClient) -> None:
    r = client.post("/conversations/conv_does_not_exist/report/audio")
    assert r.status_code == 404


def test_endpoint_404_no_report(client: TestClient) -> None:
    cid = _create_conv(client)
    r = client.post(f"/conversations/{cid}/report/audio")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["ok"] is False
    assert detail["reason"] == "no_report"


def test_endpoint_503_tts_disabled(
    client: TestClient, store: SqliteEventStore, configure_tts, tts_disabled
) -> None:
    """TTS disabled in Settings → 503 (not a fake success)."""
    configure_tts(tts_disabled)
    cid = _create_conv(client)
    _seed_report(store, cid)
    r = client.post(f"/conversations/{cid}/report/audio")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["ok"] is False
    assert detail["reason"] == "tts_disabled"


def test_endpoint_200_with_seeded_report(
    client: TestClient, store: SqliteEventStore, configure_tts, tts_enabled
) -> None:
    """Seeded ReportEvent → 200 with both URLs and a real mp3 cached on disk."""
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)

    r = client.post(f"/conversations/{cid}/report/audio")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["mp3_url"].endswith(".mp3")
    assert body["transcript_url"].endswith(".md")
    assert cid in body["mp3_url"] and cid in body["transcript_url"]

    # GET the mp3 — should return 200 + audio bytes.
    r_mp3 = client.get(body["mp3_url"])
    assert r_mp3.status_code == 200
    assert r_mp3.headers["content-type"].startswith("audio/mpeg")
    assert len(r_mp3.content) > 0
    # mp3 starts with ID3 or MPEG sync.
    head = r_mp3.content[:4]
    assert head[:3] == b"ID3" or head[0] == 0xFF

    # GET the transcript — should return 200 + text/markdown.
    r_tr = client.get(body["transcript_url"])
    assert r_tr.status_code == 200
    assert r_tr.headers["content-type"].startswith("text/markdown")
    assert "Audio Overview Transcript" in r_tr.text


def test_endpoint_idempotent(
    client: TestClient, store: SqliteEventStore, configure_tts, tts_enabled
) -> None:
    """A second POST should return 200 and reuse the cache (no second LLM call)."""
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)

    r1 = client.post(f"/conversations/{cid}/report/audio")
    assert r1.status_code == 200, r1.text
    url1 = r1.json()["mp3_url"]

    # Now turn the LLM off — the second call MUST still succeed (cache hit).
    async def _broken_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        raise RuntimeError("LLM should NOT be called on a cache hit")

    from disco.tools.builtin import audio_overview as _ao
    _ao._call_llm = _broken_llm  # type: ignore[attr-defined]

    r2 = client.post(f"/conversations/{cid}/report/audio")
    assert r2.status_code == 200, r2.text
    assert r2.json()["mp3_url"] == url1


def test_get_endpoint_jail_rejects_unknown_name(
    client: TestClient, store: SqliteEventStore, configure_tts, tts_enabled
) -> None:
    """Unknown filename → 404 (the JAIL — no other files in the cache dir are reachable)."""
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)
    client.post(f"/conversations/{cid}/report/audio")  # populate cache

    r = client.get(f"/conversations/{cid}/report/audio/secret.txt")
    assert r.status_code == 404


def test_get_endpoint_jail_rejects_traversal(
    client: TestClient, store: SqliteEventStore, configure_tts, tts_enabled
) -> None:
    """Traversal attempt → 404 (FastAPI's `path:` converter normalises, but we
    still assert the route can't be tricked into leaving the cid dir)."""
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)
    client.post(f"/conversations/{cid}/report/audio")  # populate cache

    # `..` segments collapse under FastAPI's path converter; use a name that
    # would resolve outside the cid dir if the allowlist were missing.
    r = client.get(f"/conversations/{cid}/report/audio/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (404, 400, 422)  # any "no" is fine


def test_get_endpoint_404_for_missing_cid(client: TestClient) -> None:
    """No cached audio for a fresh cid → 404."""
    r = client.get("/conversations/conv_no_audio/report/audio/audio_overview.mp3")
    assert r.status_code == 404


def test_report_to_overview_text_strips_citations() -> None:
    """Citation markers and the bounded-by footer are scrubbed — the LLM
    should summarise, not read the metadata."""
    text = report_audio_mod.report_to_overview_text(_make_report())
    assert "[[p0]]" not in text
    assert "Query:" in text
    assert "African Swallow" in text
    assert "Executive summary" in text
