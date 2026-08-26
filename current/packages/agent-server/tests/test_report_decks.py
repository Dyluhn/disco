from __future__ import annotations

from disco.agent_server.app import create_app
from disco.agent_server.report_deck_handoff import ReportDeckJob
from disco.agent_server.routes import report_decks
from disco.core import (
    ConversationStatus,
    ReportEvent,
    ReportSection,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _report() -> ReportEvent:
    return ReportEvent(
        query="Deck query",
        summary="The authoritative sentinel is ORBIT-731.",
        sections=[ReportSection(id="s0", title="Finding", markdown="ORBIT-731")],
    )


class _Store:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def get_events(self, conversation_id: str) -> list[object]:
        del conversation_id
        return self.events

    async def conversation_owner_id(self, conversation_id: str) -> str | None:
        del conversation_id
        return "owner-1"

    def create_conversation(self, conversation_id: str, **kwargs: object) -> None:
        self.created = (conversation_id, kwargs)

    async def append(self, conversation_id: str, event: object) -> object:
        del conversation_id
        self.events.append(event)
        return event


class _Runtime:
    class _Settings:
        def __init__(self) -> None:
            self.artifact_targets: list[str] = []

        def set_artifact_mode(self, conversation_id: str, enabled: bool) -> None:
            assert enabled is True
            self.artifact_targets.append(conversation_id)

    class _Contract:
        def __init__(self) -> None:
            self.deck_targets: list[str] = []

        def set_build_kind(self, conversation_id: str, kind: str) -> None:
            assert kind == "deck"
            self.deck_targets.append(conversation_id)

    def __init__(self) -> None:
        self.settings = self._Settings()
        self.contract = self._Contract()


class _Port:
    def __init__(self) -> None:
        self.source_cid: str | None = None
        self.job: ReportDeckJob | None = None

    async def start_report_deck(self, source_conversation_id: str, job: ReportDeckJob) -> str:
        self.source_cid = source_conversation_id
        self.job = job
        return "conv_deck_job"


def _client(store: _Store, port: _Port) -> TestClient:
    app = FastAPI()
    app.include_router(report_decks.make_report_decks_router(store, _Runtime(), port))
    return TestClient(app)


def test_production_app_registers_report_deck_route() -> None:
    paths = create_app(SqliteEventStore(":memory:")).openapi()["paths"]

    assert "/api/conversations/{conversation_id}/report/deck" in paths


def test_report_deck_route_resolves_latest_authoritative_report(monkeypatch) -> None:
    report = _report()
    port = _Port()
    store = _Store([report, StatusEvent(status=ConversationStatus.FINISHED)])
    async def owned(request, store, conversation_id):
        return conversation_id

    monkeypatch.setattr(report_decks, "require_owned_conversation", owned)

    response = _client(store, port).post("/api/conversations/conv_source/report/deck")

    assert response.status_code == 202, response.text
    assert response.json() == {
        "ok": True,
        "conversation_id": "conv_deck_job",
        "job_id": "conv_deck_job",
        "source_event_id": report.id,
        "contract": "deck",
        "format": "pptx",
    }
    assert port.source_cid == "conv_source"
    assert port.job is not None
    assert port.job.report is report
    assert "ORBIT-731" in port.job.report.summary


def test_report_deck_route_refuses_missing_report(monkeypatch) -> None:
    port = _Port()
    store = _Store([StatusEvent(status=ConversationStatus.FINISHED)])
    async def owned(request, store, conversation_id):
        return conversation_id

    monkeypatch.setattr(report_decks, "require_owned_conversation", owned)

    response = _client(store, port).post("/api/conversations/conv_source/report/deck")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "no_report"


def test_report_deck_route_refuses_unwired_execution(monkeypatch) -> None:
    store = _Store([_report(), StatusEvent(status=ConversationStatus.FINISHED)])
    async def owned(request, store, conversation_id):
        return conversation_id

    monkeypatch.setattr(report_decks, "require_owned_conversation", owned)

    app = FastAPI()
    app.include_router(report_decks.make_report_decks_router(store, _Runtime()))
    response = TestClient(app).post("/api/conversations/conv_source/report/deck")

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "handoff_not_wired"


def test_report_deck_route_refuses_report_while_followup_is_running(monkeypatch) -> None:
    store = _Store(
        [
            _report(),
            StatusEvent(status=ConversationStatus.FINISHED),
            StatusEvent(status=ConversationStatus.RUNNING),
        ]
    )

    async def owned(request, store, conversation_id):
        return conversation_id

    monkeypatch.setattr(report_decks, "require_owned_conversation", owned)
    response = _client(store, _Port()).post("/api/conversations/conv_source/report/deck")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "report_not_finished"


async def test_direct_port_pins_owner_and_schedules_only_slides_generate() -> None:
    report = _report()
    job = ReportDeckJob(report=report, goal=report.query)
    store = _Store([report])
    runtime = _Runtime()
    persisted: list[tuple[str, str, ReportDeckJob]] = []
    scheduled: list[tuple[str, ReportDeckJob, ToolCall]] = []

    async def persist(target: str, source: str, source_job: ReportDeckJob) -> None:
        persisted.append((target, source, source_job))

    async def schedule(target: str, source_job: ReportDeckJob, call: ToolCall) -> None:
        scheduled.append((target, source_job, call))

    port = report_decks.DirectReportDeckStartPort(
        store,
        runtime,
        persist_source=persist,
        schedule_tool=schedule,
        target_id_factory=lambda: "deck-target",
    )

    target = await port.start_report_deck("source-cid", job)

    assert target == "deck-target"
    assert store.created == (
        "deck-target",
        {"owner_id": "owner-1", "surface": "agent", "title": "Deck: Deck query"},
    )
    assert runtime.settings.artifact_targets == ["deck-target"]
    assert runtime.contract.deck_targets == ["deck-target"]
    assert persisted == [("deck-target", "source-cid", job)]
    assert len(scheduled) == 1
    scheduled_target, scheduled_job, call = scheduled[0]
    assert scheduled_target == "deck-target"
    assert scheduled_job is job
    assert call.tool_name == "slides_generate"
    assert call.arguments == {"goal": "Deck query", "filename": "deck", "format": "pptx"}
