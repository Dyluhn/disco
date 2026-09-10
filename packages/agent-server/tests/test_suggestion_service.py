from __future__ import annotations

import asyncio
import json
import time
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
    cache = tmp_path / ".disco" / "suggestions" / "research.json"
    assert cache.exists()
    assert json.loads(cache.read_text())["schema_version"] == 2


@pytest.mark.asyncio
async def test_service_inflight_guard_dedupes_parallel_loads(tmp_path) -> None:
    class _SlowRouter(_Router):
        async def complete(self, req: Any) -> _Resp:
            self.calls += 1
            self.requests.append(req)
            await asyncio.sleep(0.01)
            return _Resp(self.text)

    router = _SlowRouter(
        "\n".join(
            [
                "a kanban board with calendar sync",
                "an invoice tracker with searchable payment status",
                "a canvas maze game with score states",
                "a support dashboard from CSV uploads",
            ]
        )
    )
    svc = SuggestionService(lambda: router, lambda: tmp_path)

    one, two = await asyncio.gather(svc.get_generated("build"), svc.get_generated("build"))

    assert one == two
    assert router.calls == 1
    prompt = router.requests[0].messages[-1].content
    assert "interactive software" in prompt
    assert "Never suggest a report" in prompt


@pytest.mark.asyncio
async def test_build_generation_drops_document_only_prompts_before_caching(tmp_path) -> None:
    router = _Router(
        "\n".join(
            [
                "write a market research report",
                "create an investor slide deck",
                "prepare a budget spreadsheet",
                "draft a product requirements document",
                "a collaborative kanban board",
                "a browser-based trivia game",
                "an inventory dashboard with CSV import",
                "a report generator with editable templates",
            ]
        )
    )
    svc = SuggestionService(lambda: router, lambda: tmp_path)

    suggestions = await svc.get_generated("build")

    assert suggestions == [
        "a collaborative kanban board",
        "a browser-based trivia game",
        "an inventory dashboard with CSV import",
        "a report generator with editable templates",
    ]
    cached = json.loads((tmp_path / ".disco" / "suggestions" / "build.json").read_text())
    assert cached["suggestions"] == suggestions


@pytest.mark.asyncio
async def test_old_build_cache_is_invalidated_after_surface_semantics_change(tmp_path) -> None:
    cache = tmp_path / ".disco" / "suggestions" / "build.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "surface": "build",
                "schema_version": 1,
                "created_at": time.time(),
                "suggestions": [
                    "Create a 10-slide investor deck",
                    "Make a budget sheet",
                    "Draft a project brief",
                    "Write a research report",
                ],
            }
        )
    )
    router = _Router(
        "\n".join(
            [
                "a collaborative kanban board",
                "a browser-based trivia game",
                "an inventory dashboard with CSV import",
                "a habit tracker with weekly charts",
            ]
        )
    )

    suggestions = await SuggestionService(lambda: router, lambda: tmp_path).get_generated("build")

    assert router.calls == 1
    assert suggestions[0] == "a collaborative kanban board"
    assert not any("deck" in item or "budget sheet" in item for item in suggestions)


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
