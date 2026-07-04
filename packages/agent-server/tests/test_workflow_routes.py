"""WF-8 workflow draft/review/approval routes."""

from __future__ import annotations

from pathlib import Path

import httpx
from disco.agent_server.routes.workflows import make_workflows_router
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings
from disco.tools.builtin.workflow_tools import JsonDirWorkflowStore
from fastapi import FastAPI


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
    cfg_store.save_projects(ProjectStorageSettings(projects_root=str(project_root)))
    runtime = ConversationRuntime(store, config_store=cfg_store)
    app = FastAPI()
    app.include_router(make_workflows_router(store, runtime))
    return app


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
            finding["code"] == "unknown_builtin_tool"
            for finding in workflow["validation_findings"]
        )

        approved = await client.post(
            "/api/workflows/wf_route_bad/approve",
            json={"approved_by": "tester", "surface_shown_digest": workflow["surface_shown_digest"]},
        )

    assert approved.status_code == 409
    assert approved.json()["detail"]["reason"] == "workflow_validation_failed"
    stored = JsonDirWorkflowStore(tmp_path).get_instance("wf_route_bad")
    assert stored is not None
    assert stored.enabled is False
