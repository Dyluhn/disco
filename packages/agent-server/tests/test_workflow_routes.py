"""WF-8 workflow draft/review/approval routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast
from unittest import mock

import httpx
from disco.agent_server.app import create_app
from disco.agent_server.routes.workflows import make_workflows_router
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SecurityRisk, SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings
from disco.core.workflow import ScheduleSpec
from disco.tools import ToolDef
from disco.tools.builtin.workflow_tools import JsonDirWorkflowStore
from disco.tools.workflow_seed import SCRIPTED_WORKSPACE_TASK_INSTANCE_ID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict


class _EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FakeCompletionResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeDraftRouter:
    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.requests: list[Any] = []

    async def complete(self, req: Any) -> _FakeCompletionResponse:
        self.requests.append(req)
        text = self._texts.pop(0) if self._texts else "{}"
        return _FakeCompletionResponse(text)


def _definition(*, tools: list[str] | None = None) -> dict:
    return {
        "name": "route_review",
        "card": "Review route workflow that writes one bounded markdown output.",
        "params_model_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "tools": tools if tools is not None else ["file_read"],
        "mcp_mounts": [],
        "skills": [],
        "policies": {"untrusted_content": True, "allows_writes": False},
        "output_contract": {
            "path_template": "outputs/{query}.md",
            "format": "markdown",
        },
        "verify": {"checks": ["output_exists"], "finalizer": "ready_for_workflow_output"},
    }


def _app(project_root: Path) -> FastAPI:
    store = SqliteEventStore(":memory:")
    cfg_store = ConfigStore(project_root / "config.json")
    cfg_store.sections.save_projects(ProjectStorageSettings(projects_root=str(project_root)))
    runtime = ConversationRuntime(store, config_store=cfg_store)
    app = FastAPI()
    app.include_router(make_workflows_router(store, runtime))
    return app


def _draft_model_json() -> str:
    return json.dumps(
        {
            "summary": "Reads a query and writes a markdown workflow brief.",
            "name": "Description Draft",
            "card": "Workflow drafted from a user description into a bounded markdown output.",
            "params": [
                {
                    "name": "query",
                    "type": "string",
                    "required": True,
                    "description": "Topic or request to process.",
                }
            ],
            "tools": ["file_read"],
            "mcp_mounts": [],
            "skills": [],
            "allows_writes": False,
            "untrusted_content": True,
            "output_path_template": "outputs/{query}.md",
            "output_format": "markdown",
            "verify_checks": ["output_exists"],
            "finalizer": "ready_for_workflow_output",
        }
    )


def _quiet_startup(runtime: ConversationRuntime) -> None:
    rt = cast(Any, runtime)
    rt._mcp._start_mcp_pool = mock.AsyncMock()
    rt.reconcile_orphaned_runs = mock.AsyncMock()
    rt.prewarm_model_probe = mock.AsyncMock()
    rt.prewarm_vision_probe = mock.AsyncMock()
    rt._idle_sweeper.run = mock.AsyncMock()
    rt._schedule._schedule_manager_loop = mock.AsyncMock()
    rt._mcp._close_mcp_pool = mock.AsyncMock()


def _runtime_for_project_root(project_root: Path | str, config_path: Path) -> ConversationRuntime:
    store = SqliteEventStore(":memory:")
    cfg_store = ConfigStore(config_path)
    cfg_store.sections.save_projects(ProjectStorageSettings(projects_root=str(project_root)))
    runtime = ConversationRuntime(store, config_store=cfg_store)
    _quiet_startup(runtime)
    return runtime


def test_create_app_lifespan_seeds_builtin_workflows(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    runtime = _runtime_for_project_root(tmp_path, tmp_path / "config.json")
    workflow_store = JsonDirWorkflowStore(tmp_path)
    assert workflow_store.get_instance(SCRIPTED_WORKSPACE_TASK_INSTANCE_ID) is None

    with TestClient(create_app(store, runtime=runtime)) as client:
        listed = client.get("/api/workflows")

    assert listed.status_code == 200
    instance_ids = {workflow["instance_id"] for workflow in listed.json()["workflows"]}
    assert SCRIPTED_WORKSPACE_TASK_INSTANCE_ID in instance_ids
    assert workflow_store.get_instance(SCRIPTED_WORKSPACE_TASK_INSTANCE_ID) is not None


def test_create_app_lifespan_skips_builtin_seed_when_projects_root_unavailable(
    tmp_path: Path,
    caplog: Any,
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    unavailable_root = blocker / "projects"
    store = SqliteEventStore(":memory:")
    runtime = _runtime_for_project_root(unavailable_root, tmp_path / "config.json")

    with caplog.at_level(logging.WARNING):
        with TestClient(create_app(store, runtime=runtime)) as client:
            health = client.get("/health")

    assert health.status_code == 200
    assert "Builtin workflow seed skipped" in caplog.text


async def test_workflow_draft_list_and_approve_records_surface_digest(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        draft = await client.post(
            "/api/workflows/draft",
            json={
                "instance_id": "wf_route_ok",
                "definition": _definition(),
                "params": {"query": "smoke"},
            },
        )
        assert draft.status_code == 200
        workflow = draft.json()["workflow"]
        assert workflow["enabled"] is False
        assert workflow["approved"] is False
        assert workflow["validation_findings"] == []
        assert workflow["compiled_surface"]["compiled"] is True
        assert "file_read" in workflow["compiled_surface"]["allowed_tools"]
        assert "finish" in workflow["compiled_surface"]["allowed_tools"]
        surface_digest = workflow["surface_shown_digest"]

        listed = await client.get("/api/workflows")
        assert listed.status_code == 200
        listed_workflow = listed.json()["workflows"][0]
        rendered_tools = {
            tool["name"] for tool in listed_workflow["compiled_surface"]["tool_definitions"]
        }
        assert {"file_read", "finish", "skip", "needs_input"} <= rendered_tools
        finish_surface = next(
            tool
            for tool in listed_workflow["compiled_surface"]["tool_definitions"]
            if tool["name"] == "finish"
        )
        assert "verify" not in finish_surface["parameters_schema"]["properties"]

        approved = await client.post(
            "/api/workflows/wf_route_ok/approve",
            json={"approved_by": "tester", "surface_shown_digest": surface_digest},
        )

    assert approved.status_code == 200
    approved_workflow = approved.json()["workflow"]
    assert approved_workflow["enabled"] is True
    assert approved_workflow["approval"]["approved_by"] == "tester"
    assert approved_workflow["approval"]["surface_shown_digest"] == surface_digest
    stored = JsonDirWorkflowStore(tmp_path).get_instance("wf_route_ok")
    assert stored is not None
    assert stored.enabled is True
    assert stored.approval is not None
    assert stored.approval.surface_shown_digest == surface_digest


async def test_workflow_review_advertises_verify_only_with_explicit_shell(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        draft = await client.post(
            "/api/workflows/draft",
            json={
                "instance_id": "wf_route_shell",
                "definition": _definition(tools=["file_read", "shell"]),
                "params": {"query": "smoke"},
            },
        )

    assert draft.status_code == 200
    finish_surface = next(
        tool
        for tool in draft.json()["workflow"]["compiled_surface"]["tool_definitions"]
        if tool["name"] == "finish"
    )
    assert "verify" in finish_surface["parameters_schema"]["properties"]


async def test_workflow_authoring_context_lists_pickable_inventory(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    cfg_store = ConfigStore(tmp_path / "config.json")
    cfg_store.sections.save_projects(ProjectStorageSettings(projects_root=str(tmp_path)))
    runtime = ConversationRuntime(store, config_store=cfg_store)
    rt = cast(Any, runtime)
    rt._mcp._http_tools = {
        "mcp__github__search_issues": ToolDef(
            name="mcp__github__search_issues",
            description="Search GitHub issues.",
            args_model=_EmptyArgs,
            base_risk=SecurityRisk.LOW,
            read_only=True,
        )
    }
    skill_store = SkillStore(tmp_path / "skills")
    skill_store.create("Release Notes", description="Draft release notes.")
    rt._skill_store = skill_store
    app = FastAPI()
    app.include_router(make_workflows_router(store, runtime))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/workflows/authoring-context")

    assert response.status_code == 200
    body = response.json()
    assert {"name": "file_read", "description": mock.ANY, "read_only": True} in body[
        "builtin_tools"
    ]
    assert body["mcp_servers"] == [{"server": "github", "tools": ["search_issues"]}]
    assert "Release Notes" in body["skills"]
    assert body["param_types"] == [
        "string",
        "integer",
        "number",
        "boolean",
        "string_array",
        "integer_array",
        "number_array",
    ]
    assert body["output_formats"] == ["markdown", "html", "json", "csv", "pptx", "pdf", "text"]


async def test_workflow_author_happy_path_persists_unapproved_review(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/workflows/author",
            json={
                "name": "Author Route",
                "card": "Workflow authored from the simplified route shape.",
                "params": [
                    {
                        "name": "query",
                        "type": "string",
                        "required": True,
                        "description": "Search query.",
                    }
                ],
                "tools": ["file_read"],
                "mcp_mounts": [],
                "skills": [],
                "allows_writes": False,
                "untrusted_content": True,
                "output_path_template": "outputs/{query}.md",
                "output_format": "markdown",
                "verify_checks": ["output_exists"],
                "finalizer": "ready_for_workflow_output",
            },
        )

    assert response.status_code == 200
    body = response.json()
    workflow = body["workflow"]
    assert workflow["name"] == "Author Route"
    assert workflow["enabled"] is False
    assert workflow["approved"] is False
    assert workflow["validation_findings"] == []
    assert body["simulation"]["ok"] is True
    assert body["simulation"]["output_path"] == "outputs/sample.md"
    stored = JsonDirWorkflowStore(tmp_path).get_instance(workflow["instance_id"])
    assert stored is not None
    assert stored.params == {"query": "sample"}


async def test_workflow_draft_from_description_persists_unapproved_review(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(":memory:")
    cfg_store = ConfigStore(tmp_path / "config.json")
    cfg_store.sections.save_projects(ProjectStorageSettings(projects_root=str(tmp_path)))
    router = _FakeDraftRouter(_draft_model_json())
    runtime = ConversationRuntime(
        store,
        config_store=cfg_store,
        router=cast(Any, router),
    )
    app = FastAPI()
    app.include_router(make_workflows_router(store, runtime))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/workflows/draft-from-description",
            json={"description": "Read a topic and write a markdown brief."},
        )

    assert response.status_code == 200
    body = response.json()
    workflow = body["workflow"]
    assert body["summary"] == "Reads a query and writes a markdown workflow brief."
    assert body["description"] == "Read a topic and write a markdown brief."
    assert workflow["name"] == "Description Draft"
    assert workflow["enabled"] is False
    assert workflow["approved"] is False
    assert workflow["validation_findings"] == []
    assert body["simulation"]["ok"] is True
    assert router.requests
    request = router.requests[0]
    assert request.response_format == "json"
    assert request.max_tokens == 1400
    assert request.temperature == 0.3
    stored = JsonDirWorkflowStore(tmp_path).get_instance(workflow["instance_id"])
    assert stored is not None
    assert stored.params == {"query": "sample"}


async def test_workflow_draft_from_description_strips_think_spans(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(":memory:")
    cfg_store = ConfigStore(tmp_path / "config.json")
    cfg_store.sections.save_projects(ProjectStorageSettings(projects_root=str(tmp_path)))
    router = _FakeDraftRouter(f"<think>plan the workflow</think>{_draft_model_json()}")
    runtime = ConversationRuntime(
        store,
        config_store=cfg_store,
        router=cast(Any, router),
    )
    app = FastAPI()
    app.include_router(make_workflows_router(store, runtime))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/workflows/draft-from-description",
            json={"description": "Draft a workflow even when the model thinks first."},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "Reads a query and writes a markdown workflow brief."
    assert JsonDirWorkflowStore(tmp_path).get_instance(body["workflow"]["instance_id"]) is not None


async def test_workflow_draft_from_description_rejects_blank_description(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/workflows/draft-from-description",
            json={"description": "   "},
        )

    assert response.status_code == 422


async def test_workflow_author_invalid_definition_returns_422(tmp_path: Path) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/workflows/author",
            json={
                "name": "Duplicate Params",
                "card": "Workflow with duplicate simplified parameter names.",
                "params": [
                    {"name": "query", "type": "string", "required": True},
                    {"name": "query", "type": "string", "required": False},
                ],
                "tools": ["file_read"],
                "mcp_mounts": [],
                "skills": [],
                "allows_writes": False,
                "untrusted_content": True,
                "output_path_template": "outputs/{query}.md",
                "output_format": "markdown",
                "verify_checks": [],
                "finalizer": None,
            },
        )

    assert response.status_code == 422
    assert response.json()["reason"] == "workflow_definition_invalid"
    assert "duplicate parameter name" in response.json()["detail"]


async def test_workflow_author_returns_error_findings_without_approving(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/workflows/author",
            json={
                "name": "Author Bad Tool",
                "card": "Workflow authored with an unavailable builtin tool.",
                "params": [{"name": "query", "type": "string", "required": True}],
                "tools": ["missing_tool"],
                "mcp_mounts": [],
                "skills": [],
                "allows_writes": False,
                "untrusted_content": True,
                "output_path_template": "outputs/{query}.md",
                "output_format": "markdown",
                "verify_checks": [],
                "finalizer": None,
            },
        )

    assert response.status_code == 200
    workflow = response.json()["workflow"]
    assert workflow["enabled"] is False
    assert any(
        finding["code"] == "unknown_builtin_tool" for finding in workflow["validation_findings"]
    )
    assert any(
        finding["code"] == "simulation_scope_compile_failed"
        for finding in response.json()["simulation"]["findings"]
    )


async def test_workflow_approve_refuses_error_findings(tmp_path: Path) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        draft = await client.post(
            "/api/workflows/draft",
            json={
                "instance_id": "wf_route_bad",
                "definition": _definition(tools=["unknown_builtin"]),
                "params": {"query": "smoke"},
            },
        )
        assert draft.status_code == 200
        workflow = draft.json()["workflow"]
        assert any(
            finding["code"] == "unknown_builtin_tool" for finding in workflow["validation_findings"]
        )

        approved = await client.post(
            "/api/workflows/wf_route_bad/approve",
            json={
                "approved_by": "tester",
                "surface_shown_digest": workflow["surface_shown_digest"],
            },
        )

    assert approved.status_code == 409
    assert approved.json()["detail"]["reason"] == "workflow_validation_failed"
    stored = JsonDirWorkflowStore(tmp_path).get_instance("wf_route_bad")
    assert stored is not None
    assert stored.enabled is False


async def test_workflow_run_rejects_unapproved_instance(tmp_path: Path) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        draft = await client.post(
            "/api/workflows/draft",
            json={
                "instance_id": "wf_route_pending",
                "definition": _definition(),
                "params": {"query": "smoke"},
            },
        )
        assert draft.status_code == 200

        response = await client.post("/api/workflows/wf_route_pending/run")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "workflow_not_approved"


async def test_workflow_run_fires_approved_instance(tmp_path: Path) -> None:
    store = SqliteEventStore(":memory:")
    cfg_store = ConfigStore(tmp_path / "config.json")
    cfg_store.sections.save_projects(ProjectStorageSettings(projects_root=str(tmp_path)))

    class RecordingRuntime(ConversationRuntime):
        created_spec: ScheduleSpec | None = None
        fired_schedule_id: str | None = None

        def create_workflow_schedule(
            self,
            spec: ScheduleSpec,
            *,
            owner_id: str,  # noqa: ARG002
        ) -> dict:
            self.created_spec = spec
            root = self.project_store().root
            assert root is not None
            schedules = root / "workflow_schedules" / "schedules"
            schedules.mkdir(parents=True, exist_ok=True)
            (schedules / "wfsched_route.json").write_text("{}", encoding="utf-8")
            return {"schedule_id": "wfsched_route"}

        async def fire_workflow_schedule_now(
            self,
            schedule_id: str,
            *,
            owner_id: str,  # noqa: ARG002
        ) -> dict | None:
            self.fired_schedule_id = schedule_id
            return {"run_cid": "conv_started"}

    runtime = RecordingRuntime(store, config_store=cfg_store)
    app = FastAPI()
    app.include_router(make_workflows_router(store, runtime))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            draft = await client.post(
                "/api/workflows/draft",
                json={
                    "instance_id": "wf_route_run",
                    "definition": _definition(),
                    "params": {"query": "smoke"},
                },
            )
            assert draft.status_code == 200
            workflow = draft.json()["workflow"]
            approved = await client.post(
                "/api/workflows/wf_route_run/approve",
                json={
                    "approved_by": "tester",
                    "surface_shown_digest": workflow["surface_shown_digest"],
                },
            )
            assert approved.status_code == 200

            response = await client.post("/api/workflows/wf_route_run/run")

        assert response.status_code == 200
        assert response.json() == {"conversation_id": "conv_started", "status": "started"}
        assert runtime.fired_schedule_id == "wfsched_route"
        assert runtime.created_spec is not None
        assert runtime.created_spec.instance_id == "wf_route_run"
        assert runtime.created_spec.enabled is False
        assert not (tmp_path / "workflow_schedules" / "schedules" / "wfsched_route.json").exists()
    finally:
        await runtime.aclose()
