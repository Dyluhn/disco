"""Server-side hardening for the report audio overview.

Covers the failure paths that used to reach the operator as "unexpected error"
or an opaque 500, the artifact's survival across a restart (UI-42), what
"Regenerate" actually does (UI-43), and the two synthesis walls a real machine
hits: a first-use voice-model download and a voice the engine does not have.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server import report_audio as report_audio_mod
from disco.core import EventSource, ReportEvent, ReportSection, SqliteEventStore
from disco.core.llm import TtsSettings
from fastapi.testclient import TestClient

_GOOD_TURN_SCRIPT = (
    '[{"speaker": "A", "text": "Welcome to the overview."},'
    ' {"speaker": "B", "text": "Glad to be here."},'
    ' {"speaker": "A", "text": "Cells reached nine hundred cycles."}]'
)


def _make_report() -> ReportEvent:
    return ReportEvent(
        source=EventSource.AGENT,
        query="solid-state batteries",
        summary="Cells reached nine hundred cycles. Costs remain high.",
        sections=[
            ReportSection(
                id="s0",
                title="Cycle life",
                markdown="Prototypes held eighty percent capacity after nine hundred cycles.",
                cited_passage_ids=["p0"],
                confidence="high",
                disputed_notes=[],
            )
        ],
        passages=[{"id": "p0", "source_title": "Cell Reports", "source_url": "https://e.com/p0"}],
        all_hits=[],
        unsupported_count=0,
        bounded_by="sources",
        depth_tier="standard_deep",
    )


def _fake_pcm() -> np.ndarray:
    return np.ones(2400, dtype=np.float32) * 0.1


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


@pytest.fixture
def client(store: SqliteEventStore) -> TestClient:
    return TestClient(create_app(store, runtime=ConversationRuntime(store)))


@pytest.fixture
def configure_tts(monkeypatch: pytest.MonkeyPatch):
    from disco.core.llm import ConfigStore, default_config

    def _set(tts: TtsSettings) -> None:
        def _loader(self):  # noqa: ANN001
            return default_config().model_copy(update={"tts": tts})

        monkeypatch.setattr(ConfigStore, "load", _loader)

    return _set


@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Fast stubs for the network + model calls, and a cache dir per test."""
    from disco.tools.builtin import audio_overview

    async def _fake_llm(_payload: dict, _llm_url: str) -> str:
        return _GOOD_TURN_SCRIPT

    async def _fake_local(_text: str, _voice: str) -> np.ndarray:
        return _fake_pcm()

    monkeypatch.setattr(audio_overview, "_call_llm", _fake_llm)
    monkeypatch.setattr(audio_overview, "_synthesize_local", _fake_local)
    monkeypatch.setattr(report_audio_mod, "_default_cache_dir", lambda: tmp_path / "tts")
    return tmp_path / "tts"


def _create_conv(client: TestClient) -> str:
    r = client.post("/conversations", json={"owner_id": "local"})
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


def _seed_report(store: SqliteEventStore, cid: str) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(store.append(cid, _make_report()))
    finally:
        loop.close()


def _enabled() -> TtsSettings:
    return TtsSettings(enabled=True, provider="bundled")


# ── UI-42: audio already on disk is discoverable after a restart ─────────────


def test_existing_audio_is_listed_without_regenerating(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline
) -> None:
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)

    assert client.get(f"/conversations/{cid}/report/audio").json() == {"ok": True, "audio": []}

    generated = client.post(f"/conversations/{cid}/report/audio?mode=single")
    assert generated.status_code == 200, generated.text

    # A fresh page load — nothing in the browser remembers the run.
    listing = client.get(f"/conversations/{cid}/report/audio")
    assert listing.status_code == 200
    entries = listing.json()["audio"]
    assert len(entries) == 1
    assert entries[0]["mode"] == "single"
    assert entries[0]["mp3_url"] == generated.json()["mp3_url"]
    # And the artifact it points at is really servable.
    assert client.get(entries[0]["mp3_url"]).status_code == 200


def test_a_second_research_run_marks_the_first_reports_audio_as_earlier(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline
) -> None:
    """A conversation can hold more than one report. The previous run's audio
    must not be passed off as this report's — but it must still be reachable,
    so it is offered with the note saying where it came from."""
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)
    assert client.post(f"/conversations/{cid}/report/audio").status_code == 200
    assert len(client.get(f"/conversations/{cid}/report/audio").json()["audio"]) == 1

    # A second, different report lands on the same conversation.
    second = _make_report().model_copy(update={"query": "sodium-ion batteries"})
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(store.append(cid, second))
    finally:
        loop.close()

    # The old artifact is still offered — an MP3 on the volume the user cannot
    # reach is the bug we are fixing — but it is marked, not passed off as this
    # report's audio.
    entries = client.get(f"/conversations/{cid}/report/audio").json()["audio"]
    assert len(entries) == 1
    assert entries[0]["stale"] is True
    assert "earlier version of this report" in entries[0]["note"]


def test_listing_audio_without_a_report_is_a_404(
    client: TestClient, configure_tts, fake_pipeline
) -> None:
    configure_tts(_enabled())
    cid = _create_conv(client)
    assert client.get(f"/conversations/{cid}/report/audio").status_code == 404


def _write_legacy_artifacts(out_dir: Path, mode: str, digest: str) -> None:
    """An artifact pair exactly as builds before the sidecar wrote them: an MP3
    and its transcript, no `.json` beside them, and a hash from whatever cache
    scheme was current at the time."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"audio_overview_{mode}_{digest}.mp3").write_bytes(b"\xff\xfbID3 fake mp3")
    (out_dir / f"audio_overview_{mode}_{digest}.md").write_text("# Audio Overview Transcript\n")


def test_audio_written_before_the_sidecar_existed_is_still_restored(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline
) -> None:
    """A conversation whose ONLY audio predates this change (no sidecar, and a
    hash from the older cache scheme). The listing used to come back empty and
    the page showed just the "Audio Overview" button, even though the MP3 was
    sitting on the volume — UI-42, unfixed for every overview generated before
    the sidecar shipped."""
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)
    cid_dir = fake_pipeline / cid
    _write_legacy_artifacts(cid_dir, "podcast", "5cd813135d85")

    listing = client.get(f"/conversations/{cid}/report/audio")
    assert listing.status_code == 200
    entries = listing.json()["audio"]
    assert len(entries) == 1
    assert entries[0]["mode"] == "podcast"  # recovered from the filename
    assert entries[0]["stale"] is True
    assert "earlier version of this report" in entries[0]["note"]
    assert entries[0]["mp3_url"].endswith("audio_overview_podcast_5cd813135d85.mp3")
    # And it is really servable, so the restored player is not a dead link.
    assert client.get(entries[0]["mp3_url"]).status_code == 200


def test_a_legacy_transcript_with_no_mp3_is_not_listed(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline
) -> None:
    """The VM directory also held an orphan `_single_*.md` with no MP3 beside
    it. A player pointing at a file that is not there is worse than no player."""
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)
    cid_dir = fake_pipeline / cid
    cid_dir.mkdir(parents=True, exist_ok=True)
    (cid_dir / "audio_overview_single_5f7a69b8e629.md").write_text("orphan\n")

    assert client.get(f"/conversations/{cid}/report/audio").json()["audio"] == []


def test_a_current_artifact_wins_over_a_legacy_one(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline
) -> None:
    """Once this report has audio of its own, the unprovable older file must
    not be offered alongside it."""
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)
    _write_legacy_artifacts(fake_pipeline / cid, "podcast", "5cd813135d85")

    generated = client.post(f"/conversations/{cid}/report/audio?mode=single")
    assert generated.status_code == 200, generated.text

    entries = client.get(f"/conversations/{cid}/report/audio").json()["audio"]
    assert len(entries) == 1
    assert entries[0]["stale"] is False
    assert entries[0]["mp3_url"] == generated.json()["mp3_url"]


def test_listing_a_conversation_with_no_audio_is_empty_not_an_error(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline
) -> None:
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)
    assert client.get(f"/conversations/{cid}/report/audio").json()["audio"] == []


# ── UI-43: Regenerate re-runs instead of handing back the cache ──────────────


def test_force_reruns_the_pipeline_and_replaces_the_artifact(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline, monkeypatch
) -> None:
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)

    synth_calls = 0
    from disco.tools.builtin import audio_overview

    async def _counting_local(_text: str, _voice: str) -> np.ndarray:
        nonlocal synth_calls
        synth_calls += 1
        return _fake_pcm()

    monkeypatch.setattr(audio_overview, "_synthesize_local", _counting_local)

    first = client.post(f"/conversations/{cid}/report/audio")
    assert first.status_code == 200
    after_first = synth_calls
    assert after_first > 0

    # Without force the content hash wins: the same file, no work done.
    cached = client.post(f"/conversations/{cid}/report/audio")
    assert cached.json()["mp3_url"] == first.json()["mp3_url"]
    assert synth_calls == after_first

    # With force the pipeline runs again and rewrites the same path.
    forced = client.post(f"/conversations/{cid}/report/audio?force=true")
    assert forced.status_code == 200
    assert forced.json()["mp3_url"] == first.json()["mp3_url"]
    assert synth_calls > after_first


# ── the voice-model download is its own failure, not "unexpected error" ──────


def test_voice_model_download_failure_names_itself(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline, monkeypatch
) -> None:
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)

    from disco.agent_server import tts_local

    async def _boom(on_progress: Any = None) -> None:  # noqa: ARG001
        raise OSError("connection reset while fetching kokoro-v1.0.onnx")

    monkeypatch.setattr(tts_local, "model_files_present", lambda: False)
    monkeypatch.setattr(tts_local, "ensure_model", _boom)

    response = client.post(f"/conversations/{cid}/report/audio")

    assert response.status_code == 502
    detail = response.json()["detail"]
    # Was reason=internal ("unexpected error") on the stream and a bare 500 on
    # this blocking path — neither told the operator a download had failed.
    assert detail["reason"] == "voice_model"
    assert "0.5 GB" in detail["message"]
    assert "connection reset" in detail["detail"]


def test_voice_model_download_progress_reaches_the_stream(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline, monkeypatch
) -> None:
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)

    from disco.agent_server import tts_local

    async def _download(on_progress: Any = None) -> None:
        for pct in (0, 50, 100):
            await on_progress(
                {
                    "stage": "downloading_model",
                    "file": "kokoro-v1.0.onnx",
                    "downloaded": pct * 5_000_000,
                    "total": 500_000_000,
                    "pct": pct,
                    "file_index": 1,
                    "file_total": 1,
                }
            )

    monkeypatch.setattr(tts_local, "model_files_present", lambda: False)
    monkeypatch.setattr(tts_local, "ensure_model", _download)

    with client.stream("POST", f"/conversations/{cid}/report/audio/stream") as response:
        body = "".join(chunk for chunk in response.iter_text())

    assert '"stage": "downloading_model"' in body
    assert '"pct": 50' in body
    assert '"stage": "done"' in body  # a download is not a script failure


# ── a voice the engine does not have falls back, with a note ────────────────


def test_missing_voice_falls_back_to_the_default_with_a_note(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline, monkeypatch
) -> None:
    configure_tts(
        TtsSettings(enabled=True, provider="bundled", voice_a="af_heart", voice_b="xx_nope")
    )
    cid = _create_conv(client)
    _seed_report(store, cid)

    from disco.tools.builtin import audio_overview

    async def _picky_local(_text: str, voice: str) -> np.ndarray:
        if voice == "xx_nope":
            raise ValueError("voice xx_nope not found in voices-v1.0.bin")
        return _fake_pcm()

    monkeypatch.setattr(audio_overview, "_synthesize_local", _picky_local)

    response = client.post(f"/conversations/{cid}/report/audio")

    assert response.status_code == 200, response.text
    assert "xx_nope" in response.json()["note"]
    assert "default voice (af_heart)" in response.json()["note"]


def test_a_backend_that_fails_in_every_voice_still_fails(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline, monkeypatch
) -> None:
    configure_tts(
        TtsSettings(enabled=True, provider="bundled", voice_a="af_heart", voice_b="xx_nope")
    )
    cid = _create_conv(client)
    _seed_report(store, cid)

    from disco.tools.builtin import audio_overview

    async def _broken(_text: str, _voice: str) -> np.ndarray:
        raise RuntimeError("onnxruntime session is gone")

    monkeypatch.setattr(audio_overview, "_synthesize_local", _broken)

    response = client.post(f"/conversations/{cid}/report/audio")
    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "tts_backend"


@pytest.mark.asyncio
async def test_a_script_that_synthesises_to_nothing_says_so(monkeypatch) -> None:
    """When every turn is skipped there is no PCM to mix. The mixer's own
    exception used to surface as reason=internal ("unexpected error"); the
    empty result is now named where it happens."""

    async def _skip_everything(*_args: Any, **_kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(report_audio_mod, "_synthesize_turn_pcm", _skip_everything)

    class _Turn:
        speaker = "A"
        text = "anything"

    with pytest.raises(report_audio_mod.TtsBackendError) as exc:
        await report_audio_mod._synthesize_turns(
            [_Turn(), _Turn()],
            "af_heart",
            "af_bella",
            is_remote=False,
            remote_base="",
            remote_key="",
            remote_model="",
            backend="bundled Kokoro",
            on_progress=None,
        )
    assert "produced no audio" in str(exc.value)
    assert "all 2 turn(s)" in str(exc.value)


def test_an_unexpected_crash_on_the_blocking_path_is_json_not_a_bare_500(
    client: TestClient, store: SqliteEventStore, configure_tts, fake_pipeline, monkeypatch
) -> None:
    configure_tts(_enabled())
    cid = _create_conv(client)
    _seed_report(store, cid)

    async def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise ZeroDivisionError("mixer arithmetic")

    monkeypatch.setattr(report_audio_mod, "generate_report_audio", _explode)
    # The route imported the name at module load; patch it where it is used.
    from disco.agent_server.routes import report as report_routes

    monkeypatch.setattr(report_routes, "generate_report_audio", _explode)

    response = client.post(f"/conversations/{cid}/report/audio")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["reason"] == "internal"
    assert "mixer arithmetic" in detail["detail"]
