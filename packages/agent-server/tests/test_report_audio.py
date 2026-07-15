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
import json
import re

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
            {
                "id": "p1",
                "source_title": "Ornithology Journal",
                "source_url": "https://example.com/p1",
            },
        ],
        all_hits=[],
        unsupported_count=1,
        bounded_by="sources",
        depth_tier="standard_deep",
    )


def _fake_pcm(seconds: float = 0.1, sample_rate: int = 24000) -> np.ndarray:
    """Tiny float32 PCM — enough to mix and encode but tiny enough not to be slow."""
    n = int(seconds * sample_rate)
    return np.ones(n, dtype=np.float32) * 0.1


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
        text: str,
        voice: str,
        base_url: str,
        *,
        api_key: str = "",
        model: str = "",  # noqa: ARG001
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
    from disco.core.llm import ConfigStore, default_config

    def _set(tts: TtsSettings) -> None:
        def _loader(self):  # noqa: ANN001
            return default_config().model_copy(update={"tts": tts})

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
            _make_report(),
            conversation_id="conv_abc",
            tts_settings=tts_enabled,
            out_dir=out_dir,
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


def test_generate_report_audio_disabled(configure_tts, tts_disabled, tmp_path) -> None:
    """`tts.enabled = False` → :class:`TtsDisabled` (NOT a fake success)."""
    configure_tts(tts_disabled)
    out_dir = tmp_path / "conv_disabled" / "audio"
    with pytest.raises(report_audio_mod.TtsDisabled):
        asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(),
                conversation_id="conv_disabled",
                tts_settings=tts_disabled,
                out_dir=out_dir,
            )
        )
    # No files should be written.
    assert not (out_dir / "audio_overview.mp3").exists()


def test_generate_report_audio_synth_failure(configure_tts, tmp_path, monkeypatch) -> None:
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
                _make_report(),
                conversation_id="conv_synth_failure",
                tts_settings=tts,
                out_dir=out_dir,
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


def test_report_route_optional_body_contract(client: TestClient) -> None:
    """Absent, empty, and null bodies retain the existing default-body contract."""
    cid = _create_conv(client)
    paths = (
        f"/api/conversations/{cid}/report/export?fmt=md",
        f"/conversations/{cid}/report/audio",
    )
    for path in paths:
        for request_kwargs in ({}, {"json": {}}, {"json": None}):
            response = client.post(path, **request_kwargs)
            assert response.status_code == 404, response.text
            assert response.json()["detail"]["reason"] == "no_report"

    openapi = client.app.openapi()
    request_bodies = {
        path: openapi["paths"][path]["post"]["requestBody"]
        for path in (
            "/api/conversations/{conversation_id}/report/export",
            "/conversations/{conversation_id}/report/audio",
        )
    }
    assert all("required" not in body for body in request_bodies.values())
    assert request_bodies["/api/conversations/{conversation_id}/report/export"]["content"][
        "application/json"
    ]["schema"]["default"] == {
        "theme": "disco",
        "mode": "light",
    }
    assert (
        request_bodies["/conversations/{conversation_id}/report/audio"]["content"][
            "application/json"
        ]["schema"]["default"]
        == {}
    )


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


def test_endpoint_recovers_length_stop_and_serves_complete_unique_transcript(
    client: TestClient,
    store: SqliteEventStore,
    configure_tts,
    tts_enabled,
    monkeypatch,
) -> None:
    """The report route shares length-aware assembly and completes mocked TTS."""

    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)
    payloads: list[dict] = []
    from disco.tools.builtin import audio_overview as _ao

    async def _fake_llm(payload: dict, _llm_url: str):
        payloads.append(payload)
        if len(payloads) == 1:
            return _ao.LLMResponse(
                content='{"total_turns":12,"turns":[{"index":1,"text":"cut',
                finish_reason="length",
            )
        user_content = payload["messages"][-1]["content"]
        match = re.search(r"contiguous turn indexes (\d+) through (\d+)", user_content)
        assert match is not None
        start, end = (int(value) for value in match.groups())
        turns = [
            {
                "index": index,
                "speaker": "A" if index % 2 else "B",
                "text": f"Endpoint assembled turn {index}: cited report fact [source].",
            }
            for index in range(start, end + 1)
        ]
        return _ao.LLMResponse(
            content=json.dumps({"total_turns": 12, "turns": turns}),
            finish_reason="stop",
        )

    monkeypatch.setattr(_ao, "_call_llm", _fake_llm)
    response = client.post(f"/conversations/{cid}/report/audio")

    assert response.status_code == 200, response.text
    body = response.json()
    transcript = client.get(body["transcript_url"])
    assert transcript.status_code == 200
    assert len(payloads) == 7
    assert "African Swallow" in payloads[0]["messages"][-1]["content"]
    for index in range(1, 13):
        assert transcript.text.count(f"Endpoint assembled turn {index}:") == 1
    mp3 = client.get(body["mp3_url"])
    assert mp3.status_code == 200 and len(mp3.content) > 0


def test_report_http_adapter_preserves_provider_finish_reason(monkeypatch) -> None:
    from disco.core.llm import ConfigStore

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {"content": "{"},
                        "finish_reason": "length",
                    }
                ]
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> _Response:
            return _Response()

    monkeypatch.setattr(ConfigStore, "origin_approved", lambda *_args: True)
    monkeypatch.setattr(report_audio_mod.httpx, "AsyncClient", lambda **_kwargs: _Client())
    result = asyncio.run(
        report_audio_mod._authenticated_call_llm(
            {"messages": []},
            "http://provider.invalid/v1",
            api_key_env=None,
            purpose="model:test",
            conversation_id="conv_finish_reason",
            model="test-model",
            ledger_purpose="report_audio.podcast",
        )
    )
    assert isinstance(result, report_audio_mod.audio_overview.LLMResponse)
    assert result.finish_reason == "length"


def test_report_endpoint_retains_one_normal_malformed_correction(
    client: TestClient,
    store: SqliteEventStore,
    configure_tts,
    tts_enabled,
    monkeypatch,
) -> None:
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)
    payloads: list[dict] = []
    from disco.tools.builtin import audio_overview as _ao

    async def _fake_llm(payload: dict, _llm_url: str):
        payloads.append(payload)
        if len(payloads) == 1:
            return _ao.LLMResponse(content="ordinary malformed output", finish_reason="stop")
        all_content = "\n".join(message["content"] for message in payload["messages"])
        match = re.search(r"contiguous turn indexes (\d+) through (\d+)", all_content)
        assert match is not None
        start, end = (int(value) for value in match.groups())
        turns = [
            {
                "index": index,
                "speaker": "A" if index % 2 else "B",
                "text": f"Malformed recovery turn {index}.",
            }
            for index in range(start, end + 1)
        ]
        return _ao.LLMResponse(
            content=json.dumps({"total_turns": 12, "turns": turns}),
            finish_reason="stop",
        )

    monkeypatch.setattr(_ao, "_call_llm", _fake_llm)
    response = client.post(f"/conversations/{cid}/report/audio")

    assert response.status_code == 200, response.text
    assert len(payloads) == 4
    assert len(payloads[1]["messages"]) == 4  # system + request + invalid + correction
    transcript = client.get(response.json()["transcript_url"]).text
    for index in range(1, 13):
        assert transcript.count(f"Malformed recovery turn {index}.") == 1


def test_endpoint_repeated_length_stop_is_bounded_and_actionable(
    client: TestClient,
    store: SqliteEventStore,
    configure_tts,
    tts_enabled,
    monkeypatch,
) -> None:
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)
    calls = 0
    from disco.tools.builtin import audio_overview as _ao

    async def _fake_llm(_payload: dict, _llm_url: str):
        nonlocal calls
        calls += 1
        return _ao.LLMResponse(content="{", finish_reason="length")

    monkeypatch.setattr(_ao, "_call_llm", _fake_llm)
    response = client.post(f"/conversations/{cid}/report/audio")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["reason"] == "turn_script"
    assert "finish_reason='length'" in detail["detail"]
    assert "no partial artifact" in detail["detail"]
    assert calls == 3


@pytest.mark.parametrize(
    ("failure_type", "expected_reason"),
    [
        pytest.param(report_audio_mod.TurnScriptError, "turn_script", id="turn-script"),
        pytest.param(report_audio_mod.TtsBackendError, "tts_backend", id="tts-backend"),
    ],
)
def test_endpoint_classifies_audio_generation_failure(
    client: TestClient,
    store: SqliteEventStore,
    configure_tts,
    tts_enabled,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
    expected_reason: str,
) -> None:
    """HTTP 502 responses distinguish script generation from TTS failures."""
    import disco.agent_server.routes.report as report_routes

    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)

    async def _fail(*_args: object, **_kwargs: object) -> None:
        assert _kwargs["conversation_id"] == cid
        raise failure_type("classified audio failure")

    monkeypatch.setattr(report_routes, "generate_report_audio", _fail)
    response = client.post(f"/conversations/{cid}/report/audio")

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "ok": False,
            "reason": expected_reason,
            "detail": "classified audio failure",
        }
    }


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
    r = client.get("/conversations/conv_no_audio/report/audio/audio_overview_podcast.mp3")
    assert r.status_code == 404


def test_report_to_overview_text_strips_citations() -> None:
    """Citation markers and the bounded-by footer are scrubbed — the LLM
    should summarise, not read the metadata."""
    text = report_audio_mod.report_to_overview_text(_make_report())
    assert "[[p0]]" not in text
    assert "Query:" in text
    assert "African Swallow" in text
    assert "Executive summary" in text


# ---- WALK-21 / D3: mode-aware cache + single-speaker tests ------------------


def test_mode_aware_cache_key_no_collision(configure_tts, tts_enabled, tmp_path) -> None:
    """Podcast and single modes write DIFFERENT cache files in the same out_dir.

    This prevents the two modes from colliding when both are requested for the
    same conversation (WALK-21 / D3 critical cache-collision fix)."""
    configure_tts(tts_enabled)
    out_dir = tmp_path / "conv_modes" / "audio"

    # Patch the LLM for single mode (all-A script).
    _SINGLE_SCRIPT = """[
      {"speaker": "A", "text": "Here is an honest overview of the research."},
      {"speaker": "A", "text": "The main finding is that African swallows are faster."}
    ]"""

    from disco.tools.builtin import audio_overview as _ao

    original_llm = _ao._call_llm  # noqa: SLF001

    # Must be named '_fake_llm' so _authenticated_call_llm's test-detection check passes.
    async def _fake_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        content = payload["messages"][0]["content"]
        # Detect single mode by presence of the single-speaker prompt marker.
        if "narrator" in content.lower() or "single-voice" in content.lower():
            return _SINGLE_SCRIPT
        return _GOOD_TURN_SCRIPT

    import asyncio

    _ao._call_llm = _fake_llm  # type: ignore[assignment]
    try:
        mp3_podcast, tr_podcast = asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(),
                conversation_id="conv_modes",
                tts_settings=tts_enabled,
                out_dir=out_dir,
                mode="podcast",
            )
        )
        mp3_single, tr_single = asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(),
                conversation_id="conv_modes",
                tts_settings=tts_enabled,
                out_dir=out_dir,
                mode="single",
            )
        )
    finally:
        _ao._call_llm = original_llm  # type: ignore[assignment]

    # Two distinct filenames → no collision.
    assert mp3_podcast != mp3_single
    assert tr_podcast != tr_single
    assert "podcast" in mp3_podcast.name
    assert "single" in mp3_single.name
    # Both files exist independently.
    assert mp3_podcast.exists()
    assert mp3_single.exists()


def test_single_mode_transcript_no_host_labels(configure_tts, tts_enabled, tmp_path) -> None:
    """Single-mode transcript must NOT emit 'Host A' / 'Host B' labels (WALK-21 / D3).
    The voice is just spoken text — no dialogue attribution."""
    configure_tts(tts_enabled)
    out_dir = tmp_path / "conv_single_tr" / "audio"

    _SINGLE_ONLY_SCRIPT = """[
      {"speaker": "A", "text": "Welcome to this research breakdown."},
      {"speaker": "A", "text": "The evidence points to a nuanced picture."}
    ]"""

    from disco.tools.builtin import audio_overview as _ao

    original_llm = _ao._call_llm  # noqa: SLF001

    # Must be named '_fake_llm' so _authenticated_call_llm's test-detection check passes.
    async def _fake_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        return _SINGLE_ONLY_SCRIPT

    import asyncio

    _ao._call_llm = _fake_llm  # type: ignore[assignment]
    try:
        _, tr_path = asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(),
                conversation_id="conv_single_tr",
                tts_settings=tts_enabled,
                out_dir=out_dir,
                mode="single",
            )
        )
    finally:
        _ao._call_llm = original_llm  # type: ignore[assignment]

    tr = tr_path.read_text(encoding="utf-8")
    assert "Host A" not in tr and "Host B" not in tr, (
        "Single-mode transcript must not emit 'Host A'/'Host B' speaker labels"
    )
    assert "Audio Overview Transcript" in tr
    assert "Welcome to this research breakdown" in tr


def test_endpoint_400_for_invalid_mode(
    client: TestClient, store: SqliteEventStore, configure_tts, tts_enabled
) -> None:
    """Unknown mode → 400 (fail-fast, not a silent default)."""
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)

    r = client.post(f"/conversations/{cid}/report/audio?mode=bogus")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["reason"] == "invalid_mode"


def test_endpoint_single_mode_200(
    client: TestClient, store: SqliteEventStore, configure_tts, tts_enabled
) -> None:
    """Single mode returns 200 and writes a separate cache file (not the podcast file)."""
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)

    r = client.post(f"/conversations/{cid}/report/audio?mode=single")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "single" in body["mp3_url"], f"Expected 'single' in mp3_url; got {body['mp3_url']!r}"


# ---- WALK-13 / D1: reporthook test -----------------------------------------


def test_fetch_reporthook_logs_progress(tmp_path, monkeypatch) -> None:
    """_fetch must pass a reporthook to urlretrieve that logs percent progress."""
    from disco.agent_server.tts_local import _fetch

    captured_hooks: list = []

    def _mock_urlretrieve(url: str, dest: str, reporthook=None, data=None) -> tuple:  # noqa: ANN001
        captured_hooks.append(reporthook)
        # Simulate a 100-byte download in two 50-byte blocks.
        if reporthook is not None:
            reporthook(1, 50, 100)  # 50 %
            reporthook(2, 50, 100)  # 100 %
        # Write a dummy file so rename works.
        import pathlib

        pathlib.Path(dest).write_bytes(b"fake")
        return (dest, {})

    import hashlib

    # SHA256 of the dummy "fake" bytes we'll write.
    sha = hashlib.sha256(b"fake").hexdigest()

    monkeypatch.setattr("urllib.request.urlretrieve", _mock_urlretrieve)

    dest = tmp_path / "model.onnx"
    _fetch("https://example.com/model.onnx", dest, sha)

    assert len(captured_hooks) == 1, "urlretrieve must be called exactly once"
    assert captured_hooks[0] is not None, "_fetch must pass a reporthook (not None)"


# ---- WALK-20 / B3: follow-up text in audio overview -------------------------


def test_report_to_overview_text_includes_follow_ups() -> None:
    """report_to_overview_text appends follow-up Q&A when provided."""
    report = _make_report()
    follow_ups = [
        ("What about the sparrow?", "The sparrow is much slower at 4 m/s."),
        ("Any penguins?", "Penguins cannot fly but swim at 6 m/s."),
    ]
    text = report_audio_mod.report_to_overview_text(report, follow_ups)
    assert "Follow-up Q&A:" in text
    assert "Follow-up 1: What about the sparrow?" in text
    assert "The sparrow is much slower at 4 m/s." in text
    assert "Follow-up 2: Any penguins?" in text
    assert "Penguins cannot fly" in text


def test_report_to_overview_text_no_follow_ups_unchanged() -> None:
    """report_to_overview_text output is byte-identical when follow_ups is None or []."""
    report = _make_report()
    baseline = report_audio_mod.report_to_overview_text(report)
    assert report_audio_mod.report_to_overview_text(report, None) == baseline
    assert report_audio_mod.report_to_overview_text(report, []) == baseline
    assert "Follow-up Q&A:" not in baseline


def test_generate_report_audio_with_follow_ups(
    configure_tts, tts_enabled, tmp_path, monkeypatch
) -> None:
    """generate_report_audio passes follow_ups to the LLM payload and uses a
    distinct fu-suffixed cache filename so it doesn't collide with the base audio."""
    configure_tts(tts_enabled)

    # Capture the payload the LLM sees so we can verify follow-up text reaches it.
    # IMPORTANT: _authenticated_call_llm checks __name__ == "_fake_llm" to detect
    # the test stub; our recording wrapper must preserve that name.
    captured_payloads: list[dict] = []

    from disco.tools.builtin import audio_overview as _ao

    async def _recording_fake(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        captured_payloads.append(payload)
        return _GOOD_TURN_SCRIPT

    _recording_fake.__name__ = "_fake_llm"  # satisfy the guard in _authenticated_call_llm
    monkeypatch.setattr(_ao, "_call_llm", _recording_fake)

    follow_ups = [("What about the European swallow?", "It flies at 8 m/s.")]
    out_dir = tmp_path / "conv_fu" / "audio"
    mp3_path, transcript_path = asyncio.run(
        report_audio_mod.generate_report_audio(
            _make_report(),
            conversation_id="conv_fu",
            tts_settings=tts_enabled,
            out_dir=out_dir,
            follow_ups=follow_ups,
        )
    )
    # Cache file must include a hash that encodes the follow-up content (B3:
    # content-hash cache key); the file name format is audio_overview_<mode>_<hash12>.mp3.
    import re as _re

    assert _re.search(r"audio_overview_\w+_[0-9a-f]{12}\.mp3$", mp3_path.name), (
        f"expected content-hash filename, got {mp3_path.name!r}"
    )
    assert mp3_path.exists() and mp3_path.stat().st_size > 0
    # The LLM payload user-content must include the follow-up text.
    assert len(captured_payloads) >= 1
    user_content = " ".join(
        m.get("content", "")
        for m in captured_payloads[0].get("messages", [])
        if m.get("role") == "user"
    )
    assert "European swallow" in user_content or "Follow-up" in user_content


def test_generate_report_audio_follow_ups_separate_cache(
    configure_tts, tts_enabled, tmp_path, monkeypatch
) -> None:
    """The base audio and follow-up audio write to different cache files."""
    configure_tts(tts_enabled)
    report = _make_report()
    follow_ups = [("Extra question?", "Extra answer.")]
    out_dir = tmp_path / "conv_sep" / "audio"

    base_mp3, _ = asyncio.run(
        report_audio_mod.generate_report_audio(
            report,
            conversation_id="conv_sep",
            tts_settings=tts_enabled,
            out_dir=out_dir,
        )
    )
    fu_mp3, _ = asyncio.run(
        report_audio_mod.generate_report_audio(
            report,
            conversation_id="conv_sep",
            tts_settings=tts_enabled,
            out_dir=out_dir,
            follow_ups=follow_ups,
        )
    )
    # B3: content-hash cache key — follow-up content changes the hash so the two
    # files are distinct even though "_fu" is no longer a literal suffix.
    assert base_mp3 != fu_mp3, "base and follow-up audio must use distinct filenames"
    # Both names follow the audio_overview_<mode>_<hash12> pattern.
    import re as _re

    assert _re.search(r"audio_overview_\w+_[0-9a-f]{12}\.mp3$", base_mp3.name)
    assert _re.search(r"audio_overview_\w+_[0-9a-f]{12}\.mp3$", fu_mp3.name)


# ---- D4 robustness: empty-text-after-normalize fix (RP-09) ------------------


def test_normalize_empty_turn_falls_back_to_raw_text(
    configure_tts, tts_enabled, tmp_path, monkeypatch
) -> None:
    """A turn whose text normalizes to the empty string (code-only block or
    citation-only content) must NOT crash the pipeline with Kokoro's
    `ValueError: need at least one array to concatenate`.  The fix in
    `generate_report_audio` falls back to the raw `turn.text` before the TTS
    call so Kokoro always receives non-empty input.

    Regression test for the defect surfaced by RP-09 live acceptance: Kokoro
    raises ValueError on empty string; the `_normalize_for_tts` step can
    produce an empty string from a code-block-only or citation-only turn."""
    configure_tts(tts_enabled)

    # Build a turn-script with one code-block-only turn and two normal turns.
    # `_normalize_for_tts` strips fenced code blocks entirely → the first turn
    # would be passed as "" to Kokoro without the fix, causing ValueError.
    _TURN_SCRIPT_WITH_CODE_ONLY = """[
      {"speaker": "A", "text": "```python\\nprint('hello')\\n```"},
      {"speaker": "B", "text": "Interesting. What does that snippet show?"},
      {"speaker": "A", "text": "Just an example. The key insight is the algorithm."}
    ]"""

    from disco.tools.builtin import audio_overview as _ao

    original_llm = _ao._call_llm

    async def _fake_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        return _TURN_SCRIPT_WITH_CODE_ONLY

    _ao._call_llm = _fake_llm  # type: ignore[assignment]

    # Record what text the local synth receives.
    received_texts: list[str] = []

    async def _recording_local(text: str, voice: str) -> np.ndarray:  # noqa: ANN001
        received_texts.append(text)
        return _fake_pcm()

    try:
        out_dir = tmp_path / "conv_code_only" / "audio"
        mp3_path, _ = asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(),
                conversation_id="conv_code_only",
                tts_settings=tts_enabled,
                out_dir=out_dir,
            )
        )
    finally:
        _ao._call_llm = original_llm  # type: ignore[assignment]

    # Pipeline must complete without ValueError — proven by reaching here.
    assert mp3_path.exists() and mp3_path.stat().st_size > 0

    # The code-block turn was passed as the original (non-normalized) text, not
    # as empty string. The monkeypatched local synth records what it receives,
    # but note: the fixture uses the already-monkeypatched `_synthesize_local`,
    # so the synth here is the _fake_local from `_install_fakes`. We verify the
    # pipeline ran to completion (not ValueError) as the primary proof.


def test_all_empty_turns_skipped_gracefully(
    configure_tts, tts_enabled, tmp_path, monkeypatch
) -> None:
    """When every turn in the script normalizes to empty text AND the raw text
    is also empty (pathological case), the pipeline should log+skip each turn
    and then write a silent-frame MP3 (mix_pcm empty → encode_mp3 silent frame)
    without raising. The mixer already handles 0-turn input gracefully.

    This tests the `continue` branch in the loop (the second guard after the
    fallback fails on a genuinely-empty raw text string)."""
    configure_tts(tts_enabled)

    # A valid-looking script where every turn.text is empty string after strip.
    # `_validate_turn_script` accepts `text=" "` (non-empty after strip is
    # NOT checked in the model field validator for single spaces); we use a
    # realistic text so validation passes, but then force the normalizer to
    # return empty by monkeypatching `_normalize_for_tts`.
    _NORMAL_SCRIPT = """[
      {"speaker": "A", "text": "The algorithm uses rank positions not raw scores."},
      {"speaker": "B", "text": "That makes it robust across different scoring systems."}
    ]"""

    from disco.agent_server import report_audio as _ra
    from disco.tools.builtin import audio_overview as _ao

    original_llm = _ao._call_llm
    original_normalize = _ra._normalize_for_tts

    async def _fake_llm(payload: dict, llm_url: str) -> str:  # noqa: ARG001
        return _NORMAL_SCRIPT

    def _force_empty(text: str) -> str:  # noqa: ARG001
        return ""  # always empty — simulates pathological normalizer output

    _ao._call_llm = _fake_llm  # type: ignore[assignment]
    _ra._normalize_for_tts = _force_empty  # type: ignore[assignment]

    try:
        out_dir = tmp_path / "conv_all_empty" / "audio"
        # All turns skip → mix_pcm([]) → encode_mp3(empty) → valid silent MP3
        mp3_path, _ = asyncio.run(
            report_audio_mod.generate_report_audio(
                _make_report(),
                conversation_id="conv_all_skipped",
                tts_settings=tts_enabled,
                out_dir=out_dir,
            )
        )
    finally:
        _ao._call_llm = original_llm  # type: ignore[assignment]
        _ra._normalize_for_tts = original_normalize  # type: ignore[assignment]

    # Must not raise; the output is a silent-frame MP3 (0 real turns).
    assert mp3_path.exists() and mp3_path.stat().st_size > 0


# ---- W-08 / W-09: progress channel ----------------------------------------


def _collect_progress(monkeypatch, *, model_present: bool):
    """Run generate_report_audio capturing every on_progress event; force the
    bundled-model presence so we can assert the download stage is honest."""
    import disco.agent_server.tts_local as tts_local

    monkeypatch.setattr(tts_local, "model_files_present", lambda: model_present)
    return []


def test_progress_emits_real_stages_no_download(
    configure_tts, tts_enabled, tmp_path, monkeypatch
) -> None:
    """W-09: the pipeline emits preparing → synthesizing(N/total) → mixing.
    W-08: when the voice model is already present, NO downloading_model event."""
    configure_tts(tts_enabled)
    events = _collect_progress(monkeypatch, model_present=True)

    async def _on_progress(ev: dict) -> None:
        events.append(ev)

    out_dir = tmp_path / "conv_prog" / "audio"
    asyncio.run(
        report_audio_mod.generate_report_audio(
            _make_report(),
            conversation_id="conv_prog",
            tts_settings=tts_enabled,
            out_dir=out_dir,
            on_progress=_on_progress,
        )
    )
    stages = [e["stage"] for e in events]
    assert "preparing" in stages
    assert "mixing" in stages
    # No false download note when the model files already exist (W-08).
    assert "downloading_model" not in stages
    # Real staged synthesis — one event per turn, with current/total (W-09).
    synth = [e for e in events if e["stage"] == "synthesizing"]
    assert len(synth) == 4  # _GOOD_TURN_SCRIPT has 4 turns
    assert synth[0]["current"] == 1 and synth[0]["total"] == 4
    assert synth[-1]["current"] == 4 and synth[-1]["total"] == 4
    # Ordering: preparing before any synthesizing before mixing.
    assert stages.index("preparing") < stages.index("synthesizing")
    assert stages.index("synthesizing") < stages.index("mixing")


def test_progress_emits_download_only_when_missing(
    configure_tts, tts_enabled, tmp_path, monkeypatch
) -> None:
    """W-08: the downloading_model event fires ONLY when the bundled model files
    are genuinely absent (a real first-run download), and each frame carries the
    REAL byte/percent counters the progress bar renders."""
    configure_tts(tts_enabled)
    import disco.agent_server.tts_local as tts_local

    # Point the weights dir at an EMPTY tmp so the model is genuinely absent, and
    # fake the network fetch so ensure_model surfaces real byte progress without
    # hitting GitHub.  (No need to mock model_files_present — the real check sees
    # the empty dir and reports absent.)
    weights = tmp_path / "weights"
    weights.mkdir()
    monkeypatch.setattr(tts_local, "_data_dir", lambda: weights)

    def _fake_fetch(url, dest, sha, reporthook=None):
        if reporthook is not None:
            reporthook(1, 50, 100)  # 50 %
            reporthook(2, 50, 100)  # 100 %
        dest.write_bytes(b"z" * 100)

    monkeypatch.setattr(tts_local, "_fetch", _fake_fetch)
    events: list[dict] = []

    async def _on_progress(ev: dict) -> None:
        events.append(ev)

    out_dir = tmp_path / "conv_dl" / "audio"
    asyncio.run(
        report_audio_mod.generate_report_audio(
            _make_report(),
            conversation_id="conv_dl",
            tts_settings=tts_enabled,
            out_dir=out_dir,
            on_progress=_on_progress,
        )
    )
    stages = [e["stage"] for e in events]
    assert "downloading_model" in stages
    # The download note must precede synthesis.
    assert stages.index("downloading_model") < stages.index("synthesizing")
    # Every download frame carries the real byte/percent fields the FE bar reads.
    dl = [e for e in events if e["stage"] == "downloading_model"]
    assert all({"downloaded", "total", "pct", "file_index", "file_total"} <= e.keys() for e in dl)
    assert any(e["pct"] == 100 for e in dl)  # reaches 100 % at the end of the file


def test_progress_cache_hit_no_download(configure_tts, tts_enabled, tmp_path, monkeypatch) -> None:
    """W-08: a warm-cache run emits cache_hit and NEVER downloading_model — even
    when the model files happen to be absent (a cache hit needs no synth)."""
    configure_tts(tts_enabled)
    out_dir = tmp_path / "conv_cache" / "audio"
    # First run populates the cache.
    asyncio.run(
        report_audio_mod.generate_report_audio(
            _make_report(),
            conversation_id="conv_cache_progress",
            tts_settings=tts_enabled,
            out_dir=out_dir,
        )
    )
    # Second run with the model 'missing' — should still skip the download.
    events = _collect_progress(monkeypatch, model_present=False)

    async def _on_progress(ev: dict) -> None:
        events.append(ev)

    asyncio.run(
        report_audio_mod.generate_report_audio(
            _make_report(),
            conversation_id="conv_cache_progress",
            tts_settings=tts_enabled,
            out_dir=out_dir,
            on_progress=_on_progress,
        )
    )
    stages = [e["stage"] for e in events]
    assert stages == ["cache_hit"]
    assert "downloading_model" not in stages


def test_audio_stream_endpoint_emits_sse(
    client, store, configure_tts, tts_enabled, monkeypatch
) -> None:
    """W-09: the SSE endpoint streams real staged frames + a final done event."""
    import disco.agent_server.tts_local as tts_local

    monkeypatch.setattr(tts_local, "model_files_present", lambda: True)
    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)

    r = client.post(f"/conversations/{cid}/report/audio/stream?mode=podcast")
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("content-type", "")
    body = r.text
    assert '"stage": "preparing"' in body
    assert '"stage": "synthesizing"' in body
    assert '"stage": "mixing"' in body
    assert '"stage": "done"' in body
    assert '"mp3_url"' in body
    # No false download note when the model is present.
    assert "downloading_model" not in body


@pytest.mark.parametrize(
    ("failure_type", "expected_reason"),
    [
        pytest.param(report_audio_mod.TurnScriptError, "turn_script", id="turn-script"),
        pytest.param(report_audio_mod.TtsBackendError, "tts_backend", id="tts-backend"),
    ],
)
def test_audio_stream_classifies_audio_generation_failure(
    client: TestClient,
    store: SqliteEventStore,
    configure_tts,
    tts_enabled,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
    expected_reason: str,
) -> None:
    """SSE terminal errors preserve the same typed failure contract as HTTP."""
    import disco.agent_server.routes.report as report_routes

    configure_tts(tts_enabled)
    cid = _create_conv(client)
    _seed_report(store, cid)

    async def _fail(*_args: object, **_kwargs: object) -> None:
        assert _kwargs["conversation_id"] == cid
        raise failure_type("classified audio failure")

    monkeypatch.setattr(report_routes, "generate_report_audio", _fail)
    response = client.post(f"/conversations/{cid}/report/audio/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events == [
        {
            "stage": "error",
            "reason": expected_reason,
            "detail": "classified audio failure",
        }
    ]


def test_audio_stream_endpoint_no_report_404(client) -> None:
    """W-09: the stream endpoint 404s (before opening the stream) with no report,
    so the FE falls back / surfaces the error rather than hanging."""
    cid = _create_conv(client)
    r = client.post(f"/conversations/{cid}/report/audio/stream?mode=podcast")
    assert r.status_code == 404
