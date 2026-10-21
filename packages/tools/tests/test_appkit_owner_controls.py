"""Canonical creation, six owner editors, and persisted regeneration agree."""

import json

import pytest
from disco.core.appkit import generate, load_app_spec_from_bytes, load_design_spec_from_bytes
from disco.core.appkit.owner_controls import owner_tweak_spec, owner_tweak_values
from disco.core.design import DIRECTION_BY_ID, render_design_direction
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.app_kit import AppCreateArgs, AppCreateTool
from disco.tools.builtin.appkit import AppSetKVArgs, AppSetTweakTool
from disco.tools.secrets import CapabilityBroker
from tool_fakes import FakeSandboxInstance


def _context(sandbox: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="app-owner",
    )


@pytest.mark.asyncio
async def test_six_controls_change_real_generated_output_and_survive_reload() -> None:
    sandbox = FakeSandboxInstance()
    ctx = _context(sandbox)
    created = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", brief="Original Studio"),
        ctx,
    )
    assert created.success, created.content
    schema = json.loads(await sandbox.read_file(".disco/tweaks.json"))
    assert {field["editor"] for field in schema["fields"]} == {
        "boolean",
        "enum",
        "int",
        "palette",
        "color",
        "text",
    }
    changes = {
        "brand.name": "Orchard Studio",
        "design.accent": "#246b55",
        "design.text": "#263238",
        "layout.density": "airy",
        "layout.width": "1280",
        "motion.reduced": "true",
    }
    for key, value in changes.items():
        outcome = await AppSetTweakTool().run(AppSetKVArgs(key=key, value=value), ctx)
        assert outcome.success, outcome.content
        assert outcome.effect_receipts
    app = load_app_spec_from_bytes(await sandbox.read_file(".disco/appspec.json"))
    design = load_design_spec_from_bytes(await sandbox.read_file(".disco/designspec.json"))
    values = owner_tweak_values(app, design)
    assert values == {**changes, "layout.width": 1280, "motion.reduced": True}
    assert owner_tweak_spec(app, design).model_dump(mode="json") == json.loads(
        await sandbox.read_file(".disco/tweaks.json")
    )
    regenerated = generate(app, design)
    for path, content in regenerated.items():
        assert (await sandbox.read_file(path)).decode() == content
    css = regenerated["src/styles.css"]
    assert "max-width: 1280px" in css and "--space: 1.5rem" in css
    assert "#246b55" in css and "#263238" in css and "animation: none !important" in css
    assert "Original Studio" not in regenerated["src/generated/content.ts"]
    assert "Orchard Studio" in regenerated["src/generated/content.ts"]
    assert '<script type="module"' in regenerated["index.html"]  # no legacy HTML overwrite


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("brand.name", ""),
        ("layout.width", "1281"),
        ("motion.reduced", "yes"),
        ("design.accent", "#123456"),
        ("layout.density", "unknown"),
        ("design.text", "red; background:url(https://bad.invalid)"),
        ("lead.include_phone", "true"),
    ],
)
async def test_invalid_owner_control_preserves_all_files(key: str, value: str) -> None:
    sandbox = FakeSandboxInstance()
    ctx = _context(sandbox)
    assert (await AppCreateTool().run(AppCreateArgs(recipe_id="editorial-ledger"), ctx)).success
    before = dict(sandbox._fs)
    result = await AppSetTweakTool().run(AppSetKVArgs(key=key, value=value), ctx)
    assert not result.success
    assert sandbox._fs == before


@pytest.mark.asyncio
async def test_shared_tweak_tool_preserves_v2_kill_switch(monkeypatch) -> None:
    sandbox = FakeSandboxInstance()
    ctx = _context(sandbox)
    assert (await AppCreateTool().run(AppCreateArgs(recipe_id="editorial-ledger"), ctx)).success
    before = dict(sandbox._fs)
    monkeypatch.setenv("DISCO_APPKIT_ENABLED", "0")
    result = await AppSetTweakTool().run(AppSetKVArgs(key="layout.width", value="1280"), ctx)
    assert not result.success and result.error == "appkit_disabled"
    assert sandbox._fs == before


@pytest.mark.asyncio
@pytest.mark.parametrize("direction_id", sorted(DIRECTION_BY_ID))
async def test_advertised_palette_choices_work_with_committed_direction(direction_id: str) -> None:
    sandbox = FakeSandboxInstance()
    ctx = _context(sandbox)
    sandbox._fs[".disco/context/design_direction.md"] = render_design_direction(
        DIRECTION_BY_ID[direction_id]
    ).encode()
    created = await AppCreateTool().run(AppCreateArgs(recipe_id="editorial-ledger"), ctx)
    assert created.success, created.content
    schema = json.loads(await sandbox.read_file(".disco/tweaks.json"))
    palette = next(field for field in schema["fields"] if field["key"] == "design.accent")
    for color in palette["colors"]:
        outcome = await AppSetTweakTool().run(AppSetKVArgs(key="design.accent", value=color), ctx)
        assert outcome.success, outcome.content
    for key, value in {
        "brand.name": "Orchard Studio",
        "design.text": "#333333",
        "layout.density": "airy",
        "layout.width": "1280",
        "motion.reduced": "true",
    }.items():
        outcome = await AppSetTweakTool().run(AppSetKVArgs(key=key, value=value), ctx)
        assert outcome.success, outcome.content
