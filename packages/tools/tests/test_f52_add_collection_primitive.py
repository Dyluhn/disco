"""Epic F5.2-lite — the `collection` primitive through `app_add_primitive`.

Against a REAL lead_gen app (AppCreateTool, recipe editorial-ledger) on the
in-memory sandbox:
  * happy path — the folded collection regenerates the tree through the app's
    own base primitive: the labels land in src/generated/content.ts, a
    kind-list component is emitted for the derived section, and the AppSpec
    carries the new section on the home page;
  * provenance — the validated spec persists at .disco/primitives/collection.json;
  * no-op refusal — an identical re-apply refuses loudly (RC-M), never a hollow
    success;
  * replace-on-changed-items — re-applying the same collection_id with new
    items UPDATES the one section in place (the edit path for this slice: no
    owner-facing edit UI yet) and the regenerated content reflects it;
  * refusal wiring — an invalid spec carries the expected schema; a fold-time
    refusal (unknown page) surfaces as an app_add_primitive refusal.
"""

from __future__ import annotations

import json
from typing import Any

from disco.core.appkit import APPSPEC_RELPATH
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_CONTENT_TS = "src/generated/content.ts"
_COMPONENT_TSX = "src/components/HomeCollectionTeamSection.tsx"
_RECORD = ".disco/primitives/collection.json"

_TEAM_SPEC: dict[str, Any] = {
    "collection_id": "team",
    "title": "Meet the team",
    "items": [
        {"label": "Ana Ferreira", "detail": "Founder"},
        {"label": "Ben Okafor", "detail": "Design lead"},
        {"label": "Cleo Marsh"},
    ],
}


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-f52",
    )


async def _create_lead_gen_app(sbx: FakeSandboxInstance):
    return await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", brief="Acme Studio"),
        _ctx(sbx),
    )


async def _add_collection(sbx: FakeSandboxInstance, spec: dict[str, Any]):
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="collection", spec=spec), _ctx(sbx)
    )


def _text(sbx: FakeSandboxInstance, path: str) -> str:
    return sbx._fs[path].decode("utf-8")


# ---- the happy path ---------------------------------------------------------------


async def test_add_collection_renders_items_and_persists_provenance():
    sbx = FakeSandboxInstance()
    created = await _create_lead_gen_app(sbx)
    assert created.success is True, created.content
    assert _COMPONENT_TSX not in sbx._fs

    out = await _add_collection(sbx, _TEAM_SPEC)
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["primitive_id"] == "collection"
    assert out.structured["tier"] == "fillable"
    assert "Disco-owned" not in out.content  # fillable, not template_only

    # the labels are RENDERED into the regenerated tree (content.ts feeds the
    # section component at runtime)
    content_ts = _text(sbx, _CONTENT_TS)
    assert "Meet the team" in content_ts
    assert "Ana Ferreira — Founder" in content_ts
    assert "Ben Okafor — Design lead" in content_ts
    assert "Cleo Marsh" in content_ts

    # a real list-kind component was emitted for the derived section and is
    # mounted in the app shell
    component = _text(sbx, _COMPONENT_TSX)
    assert "kind-list" in component
    assert 'CONTENT["HomeCollectionTeamSection"]' in component
    assert "HomeCollectionTeamSection" in _text(sbx, "src/App.tsx")

    # the AppSpec was re-saved with the folded section on the home page
    app_data = json.loads(_text(sbx, APPSPEC_RELPATH))
    home = app_data["pages"][0]
    assert home["id"] == "home"
    section = next(s for s in home["sections"] if s["id"] == "collection-team")
    assert section["kind"] == "list"
    assert section["content"]["heading"] == "Meet the team"
    assert section["content"]["items"] == [
        "Ana Ferreira — Founder",
        "Ben Okafor — Design lead",
        "Cleo Marsh",
    ]

    # provenance record
    record = json.loads(_text(sbx, _RECORD))
    assert record["primitive_id"] == "collection"
    assert record["tier"] == "fillable"
    assert record["spec"]["collection_id"] == "team"
    assert record["spec"]["items"][0] == {"label": "Ana Ferreira", "detail": "Founder"}


# ---- re-apply semantics -------------------------------------------------------------


async def test_identical_reapply_is_loud_noop():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    assert (await _add_collection(sbx, _TEAM_SPEC)).success is True

    again = await _add_collection(sbx, _TEAM_SPEC)
    assert again.success is False
    assert "no-op" in again.content


async def test_reapply_with_changed_items_replaces_in_place():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    assert (await _add_collection(sbx, _TEAM_SPEC)).success is True

    changed = {
        **_TEAM_SPEC,
        "items": [
            {"label": "Ana Ferreira", "detail": "CEO"},
            {"label": "Dana Wu", "detail": "Engineering"},
        ],
    }
    out = await _add_collection(sbx, changed)
    assert out.success is True, out.content

    content_ts = _text(sbx, _CONTENT_TS)
    assert "Ana Ferreira — CEO" in content_ts
    assert "Dana Wu — Engineering" in content_ts
    assert "Cleo Marsh" not in content_ts  # the old item set is gone

    # still exactly ONE collection-team section (replaced, not duplicated)
    app_data = json.loads(_text(sbx, APPSPEC_RELPATH))
    home = app_data["pages"][0]
    matches = [s for s in home["sections"] if s["id"] == "collection-team"]
    assert len(matches) == 1
    assert matches[0]["content"]["items"] == [
        "Ana Ferreira — CEO",
        "Dana Wu — Engineering",
    ]

    # provenance reflects the LATEST applied spec
    record = json.loads(_text(sbx, _RECORD))
    assert record["spec"]["items"][1] == {"label": "Dana Wu", "detail": "Engineering"}


# ---- refusal wiring -----------------------------------------------------------------


async def test_invalid_spec_refused_with_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    out = await _add_collection(sbx, {"collection_id": "team", "title": "T", "items": []})
    assert out.success is False
    assert "invalid 'collection' spec" in out.content
    assert "Expected schema" in out.content


async def test_unknown_page_fold_refusal_surfaces():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    out = await _add_collection(sbx, {**_TEAM_SPEC, "page_id": "nope"})
    assert out.success is False
    assert "applying the 'collection' spec failed" in out.content
    assert "unknown page_id 'nope'" in out.content
    assert "'home'" in out.content  # the known page ids are carried
