"""P7 tests: scaffold_starter — materialize the active contract's host-owned starter."""

from __future__ import annotations

import json

import pytest

from disco.tools.anatomy import ToolContext
from disco.tools.builtin.scaffold_starter import ScaffoldStarterArgs, ScaffoldStarterTool
from disco.tools.secrets import CapabilityBroker

from tool_fakes import FakeSandboxInstance


def _ctx(sbx, starter_kit):
    return ToolContext(
        sandbox=sbx, workspace_path=".", timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()), owner_id="t", conversation_id="c",
        starter_kit=starter_kit,
    )


@pytest.mark.asyncio
async def test_scaffolds_app_shell() -> None:
    sbx = FakeSandboxInstance()
    out = await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="Acme"), _ctx(sbx, "app_shell"))
    assert out.success and "index.html" in sbx._fs
    assert b"<title>Acme</title>" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_scaffolds_lead_form_files() -> None:
    sbx = FakeSandboxInstance()
    out = await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="Roof Co"), _ctx(sbx, "lead_form"))
    assert out.success
    assert ".disco/appspec.json" in sbx._fs and "index.html" in sbx._fs


def _text(sbx: FakeSandboxInstance, path: str) -> str:
    return sbx._fs[path].decode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("starter", "paths"),
    [
        ("game_loop_vanilla", {"index.html", "game.js", "README.md", "NOTES.md"}),
        (
            "pwa_shell",
            {
                "index.html",
                "styles.4fd8.css",
                "app.68ca.js",
                "sw.js",
                "manifest.webmanifest",
                "icons/icon-192-any.svg",
                "icons/icon-512-any.svg",
                "icons/icon-192-maskable.svg",
                "icons/icon-512-maskable.svg",
                "NOTES.md",
            },
        ),
        ("device_frames", {"index.html", "device-frames.css", "NOTES.md"}),
        ("ui_kit_dense", {"index.html", "styles.css", "app.js", "NOTES.md"}),
    ],
)
async def test_new_starters_materialize_files_and_return_notes(starter: str, paths: set[str]) -> None:
    sbx = FakeSandboxInstance()
    out = await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="Wave Three"), _ctx(sbx, starter))
    assert out.success
    assert paths <= set(sbx._fs)
    assert out.structured is not None
    assert out.structured["notes_path"] == "NOTES.md"
    assert out.structured["notes"] == _text(sbx, "NOTES.md")
    assert "NOTES.md" in out.content and starter in out.content


@pytest.mark.asyncio
async def test_game_loop_vanilla_contains_clean_room_juice_primitives() -> None:
    sbx = FakeSandboxInstance()
    await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="Game"), _ctx(sbx, "game_loop_vanilla"))
    game_js = _text(sbx, "game.js")
    assert "const FIXED_DT = 1 / 60" in game_js
    assert "const sceneStack = []" in game_js
    assert "function loadSpriteSheet" in game_js
    assert "Self-authored clean-room one-shot synth" in game_js
    assert "function oneShotSynth" in game_js
    assert "vendored ZzFX code" in _text(sbx, "NOTES.md")
    assert "function hitStop" in game_js and "function squash" in game_js
    assert "function burst" in game_js and "shake.kick" in game_js
    assert "zzfx(" not in game_js.lower()


@pytest.mark.asyncio
async def test_pwa_shell_manifest_safe_area_and_sw_strategies() -> None:
    sbx = FakeSandboxInstance()
    await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="Phone App"), _ctx(sbx, "pwa_shell"))
    manifest = json.loads(_text(sbx, "manifest.webmanifest"))
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == "#0f766e"
    icon_slots = {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]}
    assert {
        ("192x192", "any"),
        ("512x512", "any"),
        ("192x192", "maskable"),
        ("512x512", "maskable"),
    } <= icon_slots
    css = _text(sbx, "styles.4fd8.css")
    assert "env(safe-area-inset-top)" in css
    assert "env(safe-area-inset-bottom)" in css
    assert "touch-action:manipulation" in css
    assert ":focus-visible" in css
    assert ":active" in css
    app_js = _text(sbx, "app.68ca.js")
    assert "beforeinstallprompt" in app_js and "iosCoach" in app_js
    sw_js = _text(sbx, "sw.js")
    assert "HASHED_SHELL_ASSETS" in sw_js
    assert "cacheFirst" in sw_js and "networkFirst" in sw_js


@pytest.mark.asyncio
async def test_device_frames_contains_clean_room_phone_and_window_chrome() -> None:
    sbx = FakeSandboxInstance()
    await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="Frames"), _ctx(sbx, "device_frames"))
    css = _text(sbx, "device-frames.css")
    assert "Clean-room frame CSS" in css
    assert "--frame-screen-width" in css
    assert ".iphone-ish::before" in css
    assert ".android-ish::before" in css
    assert ".side-button" in css
    assert ".macos-window" in css and ".browser-window" in css
    assert ".traffic-lights" in css and ".address-bar" in css
    assert ":focus-visible" in css
    assert "devices.css" not in css.replace("not copied from devices.css", "")


@pytest.mark.asyncio
async def test_ui_kit_dense_tokens_focus_and_font_defaults() -> None:
    sbx = FakeSandboxInstance()
    await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="Ops"), _ctx(sbx, "ui_kit_dense"))
    css = _text(sbx, "styles.css")
    assert "--font-base:13px" in css
    assert "--row-height:32px" in css
    assert "--topbar-height:56px" in css
    assert "--rail-collapsed:64px" in css
    assert "--rail-expanded:256px" in css
    assert "font-variant-numeric:tabular-nums" in css
    assert "position:sticky" in css
    assert ":focus-visible" in css
    assert "Inter" not in css
    assert "Roboto" not in css


@pytest.mark.asyncio
async def test_never_clobbers_existing_files() -> None:
    sbx = FakeSandboxInstance()
    sbx._fs["index.html"] = b"<h1>my work</h1>"  # pre-existing
    out = await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="X"), _ctx(sbx, "app_shell"))
    assert out.success
    assert sbx._fs["index.html"] == b"<h1>my work</h1>"  # untouched
    assert out.structured is not None and "index.html" in out.structured["skipped"]


@pytest.mark.asyncio
async def test_no_starter_is_structured_error() -> None:
    out = await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="X"), _ctx(FakeSandboxInstance(), None))
    assert not out.success and out.error == "no_starter"


@pytest.mark.asyncio
async def test_unknown_starter_is_structured_error() -> None:
    out = await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="X"), _ctx(FakeSandboxInstance(), "ghost"))
    assert not out.success and out.error == "unknown_starter"


@pytest.mark.asyncio
async def test_uses_file_exists_not_read_so_a_read_error_never_clobbers() -> None:
    # gpt-5.5 fix: a transient read_file failure must NOT be read as "missing" and clobber.
    class ReadFailsButExists(FakeSandboxInstance):
        async def read_file(self, path):  # type: ignore[override]
            raise RuntimeError("transient read failure")

    sbx = ReadFailsButExists()
    sbx._fs["index.html"] = b"<h1>real work</h1>"  # exists; file_exists() will report True
    out = await ScaffoldStarterTool().run(ScaffoldStarterArgs(title="X"), _ctx(sbx, "app_shell"))
    assert out.success
    assert sbx._fs["index.html"] == b"<h1>real work</h1>"  # NOT clobbered despite read error
    assert out.structured is not None and "index.html" in out.structured["skipped"]


@pytest.mark.asyncio
async def test_executor_threads_active_contract_starter_to_ctx() -> None:
    # PROVE the active-contract binding end-to-end: the executor stamps its starter_kit
    # onto every ToolContext, so scaffold_starter materializes THIS build's starter.
    from disco.core.llm import ModelExecutionPolicy
    from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
    from tool_fakes import call

    sbx = FakeSandboxInstance()
    ex = DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=sbx,
        starter_kit="app_shell",  # the runtime threads the contract's starter here
    )
    res = await ex.execute(call("scaffold_starter", title="Acme"))
    assert res.success and "index.html" in sbx._fs  # ctx.starter_kit reached the tool
    # with no starter bound, the tool reports no_starter (not a silent success)
    ex2 = DefaultToolExecutor(build_default_registry(), agent_scope(model_policy=ModelExecutionPolicy.standard()), sandbox=FakeSandboxInstance())
    res2 = await ex2.execute(call("scaffold_starter", title="Acme"))
    assert not res2.success and res2.error == "no_starter"


def test_registered_and_in_scopes() -> None:
    from disco.core.llm import ModelExecutionPolicy
    from disco.tools import agent_scope, build_default_registry
    from disco.tools.registry import artifact_scope

    reg = build_default_registry()
    assert "scaffold_starter" in reg.names()
    agent = {t.definition.name for t in reg.in_scope(agent_scope(model_policy=ModelExecutionPolicy.standard()))}
    artifact = {t.definition.name for t in reg.in_scope(artifact_scope())}
    assert "scaffold_starter" in agent and "scaffold_starter" in artifact
