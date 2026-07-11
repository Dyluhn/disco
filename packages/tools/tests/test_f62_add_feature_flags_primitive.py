"""Epic 6.2 - `feature_flags` through the real `app_add_primitive` tool.

Exercises the user-facing add path on an in-memory sandbox: app_create a lead_gen
app, add flags, regenerate the tree, persist provenance, and refuse invalid or
wrong-host requests.
"""

from __future__ import annotations

import json

from disco.core.appkit import APPSPEC_RELPATH
from disco.tools.anatomy import Capability, ToolContext, ToolOutcome
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_FLAGS_SPEC: dict[str, object] = {
    "flags": [
        {
            "key": "new-checkout",
            "description": "Show the redesigned checkout flow",
            "enabled": True,
        },
        {"key": "beta-pricing", "description": "Expose beta pricing", "enabled": False},
    ]
}


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-f62-flags",
    )


async def _create_app(sbx: FakeSandboxInstance, primitive_id: str) -> ToolOutcome:
    return await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            primitive_id=primitive_id,
            brief="Feature Flag Shop",
        ),
        _ctx(sbx),
    )


async def _add_flags(sbx: FakeSandboxInstance, spec: dict[str, object]) -> ToolOutcome:
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="feature_flags", spec=dict(spec)), _ctx(sbx)
    )


def _text(sbx: FakeSandboxInstance, path: str) -> str:
    return sbx._fs[path].decode("utf-8")


async def test_add_feature_flags_writes_tree_and_provenance():
    sbx = FakeSandboxInstance()
    created = await _create_app(sbx, "lead_gen")
    assert created.success is True, created.content

    out = await _add_flags(sbx, _FLAGS_SPEC)
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["primitive_id"] == "feature_flags"
    assert out.structured["tier"] == "fillable"
    touched = out.structured["files_written"]
    assert "schema.sql" in touched
    assert "worker/index.ts" in touched
    assert "src/db/schema.ts" in touched
    assert "src/hooks/useFlag.ts" in touched
    assert any(path.endswith("FeatureFlagsSection.tsx") for path in touched)

    schema = _text(sbx, "schema.sql")
    assert 'CREATE TABLE IF NOT EXISTS "_flags"' in schema
    assert "new-checkout" in schema
    worker = _text(sbx, "worker/index.ts")
    assert 'url.pathname === "/api/_flags" && request.method === "GET"' in worker
    assert 'url.pathname === "/api/_flags/toggle" && request.method === "POST"' in worker
    assert "if (!isAuthorized(request, env))" in worker
    hook = _text(sbx, "src/hooks/useFlag.ts")
    assert "export function useFlag(key: string): boolean" in hook
    assert "isEnabled(key: string): boolean" in hook

    app_data = json.loads(_text(sbx, APPSPEC_RELPATH))
    home_sections = app_data["pages"][0]["sections"]
    section = next(s for s in home_sections if s["id"] == "feature_flags")
    assert section["kind"] == "custom"
    assert section["content_ref"] == "feature_flags"
    assert home_sections[-1]["kind"] == "footer"

    record = json.loads(_text(sbx, ".disco/primitives/feature_flags.json"))
    assert record["primitive_id"] == "feature_flags"
    assert record["tier"] == "fillable"
    assert record["spec"]["flags"][0]["key"] == "new-checkout"


async def test_identical_feature_flags_reapply_is_loud_noop():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "lead_gen")).success is True
    first = await _add_flags(sbx, _FLAGS_SPEC)
    assert first.success is True, first.content

    again = await _add_flags(sbx, _FLAGS_SPEC)
    assert again.success is False
    assert "no-op" in again.content


async def test_invalid_feature_flags_spec_refused_with_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "lead_gen")).success is True
    out = await _add_flags(sbx, {"flags": [{"key": "Not A Slug", "enabled": True}]})
    assert out.success is False
    assert "invalid 'feature_flags' spec" in out.content
    assert "Expected schema" in out.content
    assert "flags" in out.content


async def test_directory_host_refused_with_guidance():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "directory")).success is True
    out = await _add_flags(sbx, _FLAGS_SPEC)
    assert out.success is False
    assert "lead_gen-shaped" in out.content
