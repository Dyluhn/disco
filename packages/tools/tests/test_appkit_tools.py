"""P4/TOOL-1 tests: AppKit mutation tools — create/edit/section/design/tweak/snapshot,
registry+scope membership, and the no-app / corrupt-spec error paths."""

from __future__ import annotations

import json

import pytest

from disco.core.appkit import AppSpec
from disco.core.llm import ModelExecutionPolicy
from disco.tools import agent_scope, build_default_registry
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.appkit import (
    AppAddSectionArgs,
    AppCreateArgs,
    AppCreateTool,
    AppAddSectionTool,
    AppReorderSectionArgs,
    AppReorderSectionTool,
    AppSetKVArgs,
    AppSetDesignTool,
    AppSetTweakTool,
    AppSnapshotArgs,
    AppSnapshotVersionTool,
    AppSectionRefArgs,
    AppRemoveSectionTool,
    AppUpdateContentArgs,
    AppUpdateContentTool,
)
from disco.tools.registry import artifact_scope
from disco.tools.secrets import CapabilityBroker

from tool_fakes import FakeSandboxInstance

_RETIRED_LEGACY_APP_NAMES = (
    "app_create", "app_update_content", "app_add_section", "app_remove_section",
    "app_reorder_section", "app_set_design",
)
_KEPT_LEGACY_APP_NAMES = (
    "app_set_tweak", "app_snapshot_version",
)


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx, workspace_path=".", timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()), owner_id="t", conversation_id="c",
    )


def _spec_of(sbx: FakeSandboxInstance) -> AppSpec:
    return AppSpec.model_validate_json(sbx._fs[".disco/appspec.json"])


# --- registry + scope ---------------------------------------------------------
def test_legacy_app_tools_registration_retirement() -> None:
    reg = build_default_registry()
    names = reg.names()
    agent = {t.definition.name for t in reg.in_scope(agent_scope(model_policy=ModelExecutionPolicy.standard()))}
    artifact = {t.definition.name for t in reg.in_scope(artifact_scope())}
    for n in _KEPT_LEGACY_APP_NAMES:
        assert n in names, n
        assert n in agent, n
        assert n in artifact, n
    # Hard-replace endpoint (fix-2): the retired legacy names are now OWNED by
    # the v2 AppKit tools in the default registry (the appkit.leadgen contract
    # references them); they stay OUT of the generic agent/artifact scopes —
    # only appkit contract scopes / the strict executor advertise them.
    from disco.tools.builtin.app_kit import APPKIT_V2_TOOLS

    v2_classes = {cls().definition.name: cls for cls in APPKIT_V2_TOOLS}
    for n in _RETIRED_LEGACY_APP_NAMES:
        if n in names:
            got = reg._tools.get(n) if hasattr(reg, "_tools") else None
            assert got is not None and isinstance(got, v2_classes[n]), n
        assert n not in agent, n
        assert n not in artifact, n


# --- create + render ----------------------------------------------------------
@pytest.mark.asyncio
async def test_app_create_writes_spec_and_html() -> None:
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(AppCreateArgs(title="Acme Roofing"), _ctx(sbx))
    assert out.success
    assert ".disco/appspec.json" in sbx._fs and "index.html" in sbx._fs
    assert b"Acme Roofing" in sbx._fs["index.html"]
    assert b"<form" in sbx._fs["index.html"]  # default lead form scaffolded


# --- semantic edits -----------------------------------------------------------
@pytest.mark.asyncio
async def test_update_content_touches_one_field_and_rerenders() -> None:
    sbx = FakeSandboxInstance()
    await AppCreateTool().run(AppCreateArgs(title="X"), _ctx(sbx))
    out = await AppUpdateContentTool().run(
        AppUpdateContentArgs(section_id="hero", field="headline", value="Brand new headline"), _ctx(sbx)
    )
    assert out.success
    assert _spec_of(sbx).section("hero").fields["headline"] == "Brand new headline"
    assert b"Brand new headline" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_add_reorder_remove_sections() -> None:
    sbx = FakeSandboxInstance()
    await AppCreateTool().run(AppCreateArgs(title="X"), _ctx(sbx))
    await AppAddSectionTool().run(AppAddSectionArgs(id="about", kind="about", fields={"body": "we roof"}), _ctx(sbx))
    assert [s.id for s in _spec_of(sbx).sections] == ["hero", "lead", "about"]
    await AppReorderSectionTool().run(AppReorderSectionArgs(section_id="about", to_index=0), _ctx(sbx))
    assert [s.id for s in _spec_of(sbx).sections][0] == "about"
    await AppRemoveSectionTool().run(AppSectionRefArgs(section_id="lead"), _ctx(sbx))
    assert "lead" not in [s.id for s in _spec_of(sbx).sections]


@pytest.mark.asyncio
async def test_set_design_and_tweak() -> None:
    sbx = FakeSandboxInstance()
    await AppCreateTool().run(AppCreateArgs(title="X"), _ctx(sbx))
    await AppSetDesignTool().run(AppSetKVArgs(key="primary", value="#0b5e2a"), _ctx(sbx))
    assert b"--primary:#0b5e2a" in sbx._fs["index.html"]
    out = await AppSetTweakTool().run(AppSetKVArgs(key="lead.include_phone", value="true"), _ctx(sbx))
    assert out.success and _spec_of(sbx).tweaks["lead.include_phone"] is True  # coerced to bool
    assert out.structured is not None and out.structured["affects"] == ["lead.phone"]  # P9C echoes affects


@pytest.mark.asyncio
async def test_snapshot_version() -> None:
    sbx = FakeSandboxInstance()
    await AppCreateTool().run(AppCreateArgs(title="X"), _ctx(sbx))
    out = await AppSnapshotVersionTool().run(AppSnapshotArgs(label="v1"), _ctx(sbx))
    assert out.success and sbx._fs.get(".disco/versions/v1.json")


@pytest.mark.asyncio
async def test_snapshot_default_label_is_deterministic() -> None:
    # same spec → same content-addressed label across two separate runs (no hash() randomness)
    paths: list[str] = []
    for _ in range(2):
        sbx = FakeSandboxInstance()
        await AppCreateTool().run(AppCreateArgs(title="Stable"), _ctx(sbx))
        out = await AppSnapshotVersionTool().run(AppSnapshotArgs(), _ctx(sbx))
        assert out.success and out.structured is not None
        paths.append(out.structured["rel_path"])
    assert paths[0] == paths[1]  # deterministic
    assert paths[0].startswith(".disco/versions/v-")


@pytest.mark.asyncio
async def test_app_create_seeds_a_tweakspec() -> None:
    sbx = FakeSandboxInstance()
    await AppCreateTool().run(AppCreateArgs(title="X"), _ctx(sbx))
    assert ".disco/tweaks.json" in sbx._fs  # default app → grounded tweaks seeded


@pytest.mark.asyncio
async def test_typed_tweak_validation_and_appspec_untouched_on_error() -> None:
    sbx = FakeSandboxInstance()
    await AppCreateTool().run(AppCreateArgs(title="X"), _ctx(sbx))
    # defined boolean (mixed case) + defined palette (a real swatch) both succeed + coerce
    assert (await AppSetTweakTool().run(AppSetKVArgs(key="lead.include_phone", value="TRUE"), _ctx(sbx))).success
    assert _spec_of(sbx).tweaks["lead.include_phone"] is True
    ok = await AppSetTweakTool().run(AppSetKVArgs(key="brand.accent", value="#4077A3"), _ctx(sbx))
    assert ok.success and _spec_of(sbx).tweaks["brand.accent"] == "#4077a3"  # canonicalized swatch
    # from here the appspec must NOT change on any validation failure
    before = sbx._fs[".disco/appspec.json"]
    unk = await AppSetTweakTool().run(AppSetKVArgs(key="nope.key", value="x"), _ctx(sbx))
    assert unk.error == "unknown_tweak" and sbx._fs[".disco/appspec.json"] == before
    bad = await AppSetTweakTool().run(AppSetKVArgs(key="brand.accent", value="#000000"), _ctx(sbx))  # not a swatch
    assert bad.error == "invalid_tweak_value" and sbx._fs[".disco/appspec.json"] == before
    rng = await AppSetTweakTool().run(AppSetKVArgs(key="lead.include_phone", value="maybe"), _ctx(sbx))
    assert rng.error == "invalid_tweak_value" and sbx._fs[".disco/appspec.json"] == before


@pytest.mark.asyncio
async def test_set_tweak_no_app_takes_precedence() -> None:
    sbx = FakeSandboxInstance()  # no app_create
    out = await AppSetTweakTool().run(AppSetKVArgs(key="lead.include_phone", value="true"), _ctx(sbx))
    assert not out.success and out.error == "no_app"


@pytest.mark.asyncio
async def test_create_with_duplicate_section_ids_persists_nothing() -> None:
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(
        AppCreateArgs(title="X", sections=[{"id": "dup", "kind": "hero"}, {"id": "dup", "kind": "about"}]),  # type: ignore[list-item]
        _ctx(sbx),
    )
    assert not out.success and out.error == "invalid_app_edit"
    assert ".disco/appspec.json" not in sbx._fs


# --- error paths --------------------------------------------------------------
@pytest.mark.asyncio
async def test_edit_without_app_is_no_app_error() -> None:
    out = await AppUpdateContentTool().run(
        AppUpdateContentArgs(section_id="hero", field="x", value="y"), _ctx(FakeSandboxInstance())
    )
    assert not out.success and out.error == "no_app"


@pytest.mark.asyncio
async def test_bad_section_is_invalid_edit() -> None:
    sbx = FakeSandboxInstance()
    await AppCreateTool().run(AppCreateArgs(title="X"), _ctx(sbx))
    out = await AppUpdateContentTool().run(
        AppUpdateContentArgs(section_id="ghost", field="x", value="y"), _ctx(sbx)
    )
    assert not out.success and out.error == "invalid_app_edit"


@pytest.mark.asyncio
async def test_corrupt_spec_is_structured_error() -> None:
    sbx = FakeSandboxInstance()
    sbx._fs[".disco/appspec.json"] = b"{ not json"
    out = await AppUpdateContentTool().run(
        AppUpdateContentArgs(section_id="hero", field="x", value="y"), _ctx(sbx)
    )
    assert not out.success and out.error == "corrupt_appspec"


@pytest.mark.asyncio
async def test_create_with_bad_section_kind_errors() -> None:
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(
        AppCreateArgs(title="X", sections=[{"id": "h", "kind": "nonsense"}]), _ctx(sbx)  # type: ignore[list-item]
    )
    assert not out.success and out.error == "invalid_app_edit"
    assert ".disco/appspec.json" not in sbx._fs  # nothing persisted
