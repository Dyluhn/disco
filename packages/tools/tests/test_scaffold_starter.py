"""P7 tests: scaffold_starter — materialize the active contract's host-owned starter."""

from __future__ import annotations

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
