from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from disco.agent_server import report_audio
from disco.agent_server.report_export import _build_pdf_html, serialize_markdown
from disco.agent_server.routes.deck_editor import make_deck_editor_router
from disco.agent_server.routes.report import make_report_router
from disco.core import EventSource, ObservationEvent, ReportEvent, ReportSection
from disco.core.brand import resolve_theme
from disco.core.events import ToolResult
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.builtin import audio_overview
from fastapi import FastAPI


def _report(
    query: str = "Raw user question that should not become the export title",
) -> ReportEvent:
    return ReportEvent(
        source=EventSource.AGENT,
        query=query,
        summary="Generated summary for the report.",
        sections=[
            ReportSection(
                id="s1",
                title="Findings",
                markdown="The grounded answer cites a source [[p1]].",
                cited_passage_ids=["p1"],
            )
        ],
        passages=[
            {
                "id": "p1",
                "source_title": "Source One",
                "source_url": "https://example.test/source",
            }
        ],
        all_hits=[],
        unsupported_count=0,
        depth_tier="standard_deep",
    )


async def test_w08_w09_audio_progress_is_staged_and_download_only_when_needed(
    monkeypatch, tmp_path: Path
) -> None:
    async def fake_turn_script(
        _overview_text: str,
        _mode: str,
        *,
        conversation_id: str,
    ):
        assert conversation_id == "conv_progress"
        return audio_overview.ScriptResult(
            turns=[
                audio_overview.Turn(speaker="A", text="Opening finding."),
                audio_overview.Turn(speaker="B", text="Second finding."),
            ]
        )

    async def fake_synthesize(_text: str, _voice: str):
        return b"pcm"

    monkeypatch.setattr(report_audio, "_generate_turn_script", fake_turn_script)
    monkeypatch.setattr(report_audio.audio_overview, "_synthesize_local", fake_synthesize)
    monkeypatch.setattr(report_audio, "mix_pcm", lambda *_args, **_kwargs: b"mixed-pcm")
    monkeypatch.setattr(report_audio, "encode_mp3", lambda *_args, **_kwargs: b"mp3-bytes")

    from disco.agent_server import tts_local

    settings = SimpleNamespace(
        enabled=True,
        provider="bundled",
        voice_a="af_heart",
        voice_b="af_bella",
        base_url="",
        api_key_env="",
        model="",
    )

    cold_events: list[dict] = []
    monkeypatch.setattr(tts_local, "model_files_present", lambda: False)

    async def fake_ensure_model(on_progress=None) -> None:
        if on_progress is not None:
            on_progress(
                {
                    "stage": "downloading_model",
                    "file": "kokoro-v1.0.onnx",
                    "downloaded": 10,
                    "total": 10,
                    "pct": 100,
                    "file_index": 1,
                    "file_total": 1,
                }
            )

    monkeypatch.setattr(tts_local, "ensure_model", fake_ensure_model)
    await report_audio.generate_report_audio(
        _report(),
        conversation_id="conv_progress",
        tts_settings=settings,
        out_dir=tmp_path / "cold",
        on_progress=cold_events.append,
    )
    assert [e["stage"] for e in cold_events] == [
        "preparing",
        "downloading_model",
        "synthesizing",
        "synthesizing",
        "mixing",
    ]
    assert cold_events[2] == {"stage": "synthesizing", "current": 1, "total": 2}
    assert cold_events[3] == {"stage": "synthesizing", "current": 2, "total": 2}
    assert cold_events[1]["pct"] == 100

    warm_events: list[dict] = []
    monkeypatch.setattr(tts_local, "model_files_present", lambda: True)
    await report_audio.generate_report_audio(
        _report(),
        conversation_id="conv_progress",
        tts_settings=settings,
        out_dir=tmp_path / "warm",
        on_progress=warm_events.append,
    )
    assert "downloading_model" not in [e["stage"] for e in warm_events]
    assert [e["stage"] for e in warm_events] == [
        "preparing",
        "synthesizing",
        "synthesizing",
        "mixing",
    ]

    cache_events: list[dict] = []
    await report_audio.generate_report_audio(
        _report(),
        conversation_id="conv_progress",
        tts_settings=settings,
        out_dir=tmp_path / "warm",
        on_progress=cache_events.append,
    )
    assert cache_events == [{"stage": "cache_hit"}]


async def test_w10_markdown_export_route_uses_generated_title_not_raw_question() -> None:
    store = SqliteEventStore(":memory:")
    cid = "conv_export_title"
    raw_question = "What are all the messy details about the thing I typed?"
    generated_title = "Market Structure Overview"
    store.create_conversation(cid, title=generated_title, surface="deep_research")
    await store.append(cid, _report(raw_question))

    app = FastAPI()
    app.include_router(make_report_router(store, runtime=SimpleNamespace()))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/conversations/{cid}/report/export?fmt=md", json={})
    assert response.status_code == 200, response.text
    first_line = response.text.splitlines()[0]
    assert first_line == f"# {generated_title}"
    assert raw_question not in first_line


def test_w10_serializers_use_generated_title_for_markdown_and_pdf_title_page() -> None:
    raw_question = "Raw question should not appear as the document title"
    generated_title = "Generated Report Title"
    report = _report(raw_question)

    markdown = serialize_markdown(report, title=generated_title)
    assert markdown.splitlines()[0] == f"# {generated_title}"
    assert raw_question not in markdown.splitlines()[0]

    html = _build_pdf_html(report, None, resolve_theme("disco", "light"), title=generated_title)
    assert f"<title>{generated_title}</title>" in html
    assert "cover-title" in html
    assert generated_title in html
    cover_title = html.split('<div class="cover-title">', 1)[1].split("</div>", 1)[0]
    assert raw_question not in cover_title
    assert f"Question: {raw_question}" in html


_AUTHORED_DECK = {
    "title": "Reliability Review",
    "theme": "disco-light",
    "slides": [
        {
            "type": "bullets",
            "title": "What Changed",
            "body": ["Bounded preflights", "Visible export gates"],
            "layout_hint": None,
            "image_prompt": None,
            "chart": None,
            "table": None,
            "notes": None,
        }
    ],
}


class _DeckSession:
    def __init__(self, authored: dict) -> None:
        self.files = {"deck.authored.json": json.dumps(authored).encode()}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class _FakeExec:
    exit_code = 0
    stderr = ""
    timed_out = False


class _PdfSandbox:
    def __init__(self, pdf: bytes) -> None:
        self.pdf = pdf
        self.commands: list[str] = []
        self.writes: dict[str, bytes] = {}
        self.destroyed = False

    async def write_file(self, path: str, data: bytes) -> None:
        self.writes[path] = data

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> _FakeExec:
        self.commands.append(cmd)
        assert timeout_s <= 120
        return _FakeExec()

    async def read_file(self, path: str) -> bytes:
        assert path == "_deck.pdf"
        return self.pdf

    async def destroy(self) -> None:
        self.destroyed = True


class _PdfSandboxService:
    def __init__(self, instance: _PdfSandbox) -> None:
        self.instance = instance
        self.created = False

    async def create(self, _spec, *, owner_id: str, conversation_id: str) -> _PdfSandbox:
        self.created = True
        assert owner_id
        assert conversation_id.startswith("export-deck-pdf-")
        return self.instance


class _DeckRuntime:
    def __init__(
        self,
        session: _DeckSession,
        *,
        backend: str,
        sandbox_service: _PdfSandboxService | None = None,
    ) -> None:
        self._session = session
        self._backend = backend
        self._sandbox_service = sandbox_service
        self._sandbox_spec = object()
        self.live_sessions = SimpleNamespace(live_session=self.live_session)
        self.sandbox = SimpleNamespace(
            base_spec=lambda: self._sandbox_spec,
            backend_name=self._backend_name,
            _sandbox_service_now=self._sandbox_service_now,
        )

    def live_session(self, _cid: str) -> _DeckSession:
        return self._session

    def _current_project_store(self):
        return None

    def _backend_name(self) -> str:
        return self._backend

    def _sandbox_service_now(self) -> _PdfSandboxService:
        assert self._sandbox_service is not None
        return self._sandbox_service

    @property
    def projects(self) -> SimpleNamespace:
        return SimpleNamespace(current_project_store=self._current_project_store)


async def _declare_deck(store: SqliteEventStore, cid: str) -> None:
    store.create_conversation(cid, surface="agent")
    await store.append(
        cid,
        ObservationEvent(
            action_id="a1",
            tool_result=ToolResult(
                call_id="c1",
                tool_name="slides_generate",
                success=True,
                content="deck generated",
                structured={
                    "filename": "deck.html",
                    "base_name": "deck",
                    "editable_source": "deck.authored.json",
                },
            ),
        ),
    )


async def test_w22_deck_pdf_export_is_409_on_non_container_backend() -> None:
    store = SqliteEventStore(":memory:")
    cid = "conv_deck_no_container"
    await _declare_deck(store, cid)
    runtime = _DeckRuntime(_DeckSession(_AUTHORED_DECK), backend="process")

    app = FastAPI()
    app.include_router(make_deck_editor_router(store, runtime=runtime))  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/conversations/{cid}/deck/export?path=deck&fmt=pdf")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "no_container_backend"
    assert "process" in response.json()["detail"]["message"]


async def test_w22_deck_pdf_export_on_container_backend_returns_pdf_bytes() -> None:
    store = SqliteEventStore(":memory:")
    cid = "conv_deck_pdf"
    await _declare_deck(store, cid)
    sandbox = _PdfSandbox(b"%PDF-1.4\nstub pdf\n")
    service = _PdfSandboxService(sandbox)
    runtime = _DeckRuntime(
        _DeckSession(_AUTHORED_DECK),
        backend="local",
        sandbox_service=service,
    )

    app = FastAPI()
    app.include_router(make_deck_editor_router(store, runtime=runtime))  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/conversations/{cid}/deck/export?path=deck&fmt=pdf")

    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF")
    assert response.headers["content-type"].startswith("application/pdf")
    assert service.created is True
    assert sandbox.writes["_deck.pptx"].startswith(b"PK")
    assert any("soffice --headless --convert-to pdf" in cmd for cmd in sandbox.commands)
    assert sandbox.destroyed is True
