"""Epic 6.1 - `analytics` through the real app_add_primitive tool."""

from __future__ import annotations

import json

from disco.core.appkit import APPSPEC_RELPATH
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_ANALYTICS_SPEC = {"dashboard_page_title": "Traffic"}
_RECORD = ".disco/primitives/analytics.json"


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-e61-analytics",
    )


async def _create_lead_gen_app(sbx: FakeSandboxInstance):
    return await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            primitive_id="lead_gen",
            brief="Acme Studio",
        ),
        _ctx(sbx),
    )


async def _add_analytics(sbx: FakeSandboxInstance, spec: dict[str, object]):
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="analytics", spec=spec), _ctx(sbx)
    )


def _text(sbx: FakeSandboxInstance, path: str) -> str:
    return sbx._fs[path].decode("utf-8")


async def test_add_analytics_happy_path_writes_surface_and_provenance():
    sbx = FakeSandboxInstance()
    created = await _create_lead_gen_app(sbx)
    assert created.success is True, created.content
    assert "/api/_hits" not in _text(sbx, "worker/index.ts")

    out = await _add_analytics(sbx, _ANALYTICS_SPEC)
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["primitive_id"] == "analytics"
    assert out.structured["tier"] == "fillable"
    touched = set(out.structured["files_written"])
    assert {"schema.sql", "worker/index.ts", "src/main.tsx", "src/App.tsx"} <= touched
    assert any(path.startswith("src/components/") for path in touched)

    assert 'CREATE TABLE IF NOT EXISTS "_hits"' in _text(sbx, "schema.sql")
    worker = _text(sbx, "worker/index.ts")
    assert 'url.pathname === "/api/_hits" && request.method === "POST"' in worker
    assert 'url.pathname === "/api/_hits/summary" && request.method === "GET"' in worker
    assert "isAuthorized(request, env)" in worker
    assert "installAnalyticsBeacon" in _text(sbx, "src/main.tsx")
    assert '"/analytics": Page' in _text(sbx, "src/App.tsx")
    assert any(
        'id="analytics-dashboard"' in _text(sbx, path)
        for path in sbx._fs
        if path.startswith("src/components/") and path.endswith(".tsx")
    )

    app_data = json.loads(_text(sbx, APPSPEC_RELPATH))
    assert app_data["analytics"]["dashboard_page_title"] == "Traffic"
    analytics_page = next(page for page in app_data["pages"] if page["id"] == "analytics")
    assert analytics_page["route"] == "/analytics"
    assert analytics_page["sections"][0]["content_ref"] == "_hits"

    record = json.loads(_text(sbx, _RECORD))
    assert record["primitive_id"] == "analytics"
    assert record["tier"] == "fillable"
    assert record["spec"] == _ANALYTICS_SPEC


async def test_add_analytics_invalid_and_identical_reapply_refusals():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True

    invalid = await _add_analytics(sbx, {"provider": "external"})
    assert invalid.success is False
    assert "invalid 'analytics' spec" in invalid.content
    assert "Expected schema" in invalid.content
    assert "dashboard_page_title" in invalid.content
    assert _RECORD not in sbx._fs

    first = await _add_analytics(sbx, _ANALYTICS_SPEC)
    assert first.success is True, first.content
    again = await _add_analytics(sbx, _ANALYTICS_SPEC)
    assert again.success is False
    assert "no-op" in again.content

    changed = await _add_analytics(sbx, {"dashboard_page_title": "Page analytics"})
    assert changed.success is True, changed.content
    app_data = json.loads(_text(sbx, APPSPEC_RELPATH))
    assert app_data["analytics"]["dashboard_page_title"] == "Page analytics"
