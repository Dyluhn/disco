from __future__ import annotations

import asyncio
from typing import Any

import pytest
from disco.agent_server.routes.suggestions import make_suggestions_router
from disco.agent_server.suggestion_service import (
    SuggestionService,
    parse_suggestions,
    sanitize_suggestion,
)
from disco.core.llm.types import ModelRole
from disco.core.store.sqlite import SqliteEventStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _Router:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.requests: list[Any] = []

    async def complete(self, req: Any) -> _Resp:
        self.calls += 1
        self.requests.append(req)
        return _Resp(self.text)


def test_parse_suggestions_strips_think_before_line_parsing() -> None:
    parsed = parse_suggestions(
        "\n".join(
            [
                "<think>ignore this numbered plan</think>",
                '1. "a budget dashboard from CSV uploads"',
                "- a tiny tower defense game with score states",
                "Build a weekly meal planner sheet",
                "`a deck about grid storage tradeoffs`",
            ]
        )
    )

    assert parsed == [
        "a budget dashboard from CSV uploads",
        "a tiny tower defense game with score states",
        "Build a weekly meal planner sheet",
        "a deck about grid storage tradeoffs",
    ]


def test_sanitize_suggestion_cleans_without_clipping() -> None:
    long = (
        '1. "Compare how cities changed heat, flood, and wildfire planning after '
        'three consecutive record disaster years using public budgets and policies"'
    )

    sanitized = sanitize_suggestion(long)

    assert sanitized.startswith("Compare how cities changed")
    assert sanitized.endswith("public budgets and policies")
    assert len(sanitized) > 90


def test_parse_suggestions_drops_overlong_lines_instead_of_clipping() -> None:
    overlong = (
        "Compare what policies have actually changed across every major coastal "
        "resilience program after repeated billion-dollar flood years, including "
        "budgets, enforcement, insurance, retreat, zoning, and construction"
    )

    parsed = parse_suggestions(
        "\n".join(
            [
                overlong,
                "Compare sodium-ion and LFP batteries for grid storage",
                "Trace how NASA's Artemis schedule shifted and why",
                "Which wildfire mitigation programs have measurable results?",
                "Map GLP-1 evidence and risks for adolescents",
            ]
        )
    )

    assert overlong not in parsed
    assert not any(item.startswith("Compare what policies have actually") for item in parsed)
    assert parsed == [
        "Compare sodium-ion and LFP batteries for grid storage",
        "Trace how NASA's Artemis schedule shifted and why",
        "Which wildfire mitigation programs have measurable results?",
        "Map GLP-1 evidence and risks for adolescents",
    ]


@pytest.mark.asyncio
async def test_service_generates_sanitizes_and_caches(tmp_path) -> None:
    router = _Router(
        "\n".join(
            [
                "<think>private reasoning</think>",
                "1. Compare clinic wait times across three states",
                '2. "Trace Artemis launch delays and budget causes"',
                "- Map PFAS removal evidence in municipal water",
                "Review four-day work week trials by industry",
            ]
        )
    )
    svc = SuggestionService(lambda: router, lambda: tmp_path)

    first = await svc.get_generated("research")
    second = await SuggestionService(lambda: _Router("bad"), lambda: tmp_path).get_generated(
        "research"
    )

    assert first == second
    assert router.calls == 1
    assert router.requests[0].profile.role is ModelRole.SUMMARIZER
    assert (tmp_path / ".disco" / "suggestions" / "research.json").exists()


@pytest.mark.asyncio
async def test_service_inflight_guard_dedupes_parallel_loads(tmp_path) -> None:
    class _SlowRouter(_Router):
        async def complete(self, req: Any) -> _Resp:
            self.calls += 1
            await asyncio.sleep(0.01)
            return _Resp(self.text)

    router = _SlowRouter(
        "\n".join(
            [
                "a kanban board with calendar sync",
                "a spreadsheet budget checker",
                "a canvas maze game with score states",
                "a support dashboard from CSV uploads",
            ]
        )
    )
    svc = SuggestionService(lambda: router, lambda: tmp_path)

    one, two = await asyncio.gather(svc.get_generated("build"), svc.get_generated("build"))

    assert one == two
    assert router.calls == 1


class _FailingSuggestionService:
    async def get_generated(self, surface: object) -> list[str]:
        raise RuntimeError("provider down")


class _Runtime:
    def __init__(self, service: object) -> None:
        self.suggestions = service


def test_suggestions_route_falls_back_to_curated() -> None:
    app = FastAPI()
    store = SqliteEventStore(":memory:")
    try:
        runtime = _Runtime(_FailingSuggestionService())
        app.include_router(make_suggestions_router(store, runtime))  # type: ignore[arg-type]
        resp = TestClient(app).get("/api/suggestions?surface=agent")
    finally:
        store.close()

    assert resp.status_code == 200
    body = resp.json()
    assert body["surface"] == "agent"
    assert body["source"] == "curated"
    assert len(body["suggestions"]) >= 4
