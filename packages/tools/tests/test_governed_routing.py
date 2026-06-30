"""CD-TOOLS-4 — artifact-aware governed routing. NO generic mutator may write the host-reserved
.disco/ namespace; each refuses + ROUTES to the owning semantic tool (no false tool name for an
unmapped governed path). The semantic tools themselves write .disco/ via the sandbox directly, so
they are unaffected."""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import (
    ExactReplaceTool,
    FileAppendTool,
    FileEditTool,
    FileInsertLinesTool,
    FileReplaceLinesTool,
    FileStrReplaceTool,
    FileWriteTool,
    SafeWriteFileTool,
)
from disco.tools.builtin.files import _route_for_governed, reset_read_tracker
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean():
    reset_read_tracker()
    yield
    reset_read_tracker()


async def _ctx():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    ctx = ToolContext(
        sandbox=inst, workspace_path=".", timeout_s=30,
        capabilities={Capability.FILESYSTEM}, owner_id="local", conversation_id="c",
    )
    return ctx, inst


# (tool, kwargs) for a write to the governed appspec — args beyond path are irrelevant (the guard
# fires first), but must be schema-valid.
GOV = ".disco/appspec.json"
_MUTATORS = [
    (FileWriteTool, {"path": GOV, "content": "x"}),
    (FileAppendTool, {"path": GOV, "content": "x"}),
    (FileEditTool, {"path": GOV, "old": "a", "new": "b"}),
    (FileReplaceLinesTool, {"path": GOV, "start_line": 1, "end_line": 1, "new_text": "x"}),
    (FileInsertLinesTool, {"path": GOV, "after_line": 0, "text": "x"}),
    (FileStrReplaceTool, {"path": GOV, "old_str": "a", "new_str": "b"}),
    (ExactReplaceTool, {"path": GOV, "edits": [{"old_string": "a", "new_string": "b"}]}),
    (SafeWriteFileTool, {"path": GOV, "content": "x"}),
]


@pytest.mark.parametrize("tool_cls,kwargs", _MUTATORS)
async def test_every_mutator_refuses_governed_and_routes(tool_cls, kwargs):
    ctx, inst = await _ctx()
    await inst.write_file(GOV, b'{"real":1}')  # the governed file exists
    tool = tool_cls()
    res = await tool.run(tool.definition.args_model(**kwargs), ctx)
    assert not res.success
    assert res.error in ("GOVERNED_ARTIFACT_REJECTED", "SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED")
    assert (res.structured or {}).get("route_to") == "app_set_tweak"  # routed to the owning tool
    assert (await inst.read_file(GOV)) == b'{"real":1}'  # never mutated
    await inst.destroy()


async def test_unmapped_governed_names_no_tool():
    # a governed .disco/ path with no semantic owner must NOT invent a tool (no false affordance).
    ctx, inst = await _ctx()
    res = await SafeWriteFileTool().run(
        SafeWriteFileTool.definition.args_model(path=".disco/random_thing.txt", content="x"), ctx
    )
    assert not res.success
    assert (res.structured or {}).get("route_to") is None
    assert "host-managed" in res.content.lower() and "generic write tool" in res.content.lower()
    await inst.destroy()


async def test_routing_table_maps():
    assert _route_for_governed(".disco/appspec.json")[0] == "app_set_tweak"
    assert _route_for_governed(".disco/tweaks.json")[0] == "app_set_tweak"
    assert _route_for_governed(".disco/versions/v1.json")[0] == "app_snapshot_version"
    assert _route_for_governed(".disco/context/todo")[0] == "context_memory"
    assert _route_for_governed(".disco/unknown")[0] is None


async def test_non_governed_write_still_succeeds():
    # a path that merely CONTAINS '.disco' as a substring (not the namespace) is NOT governed.
    ctx, inst = await _ctx()
    res = await FileWriteTool().run(
        FileWriteTool.definition.args_model(path="my.disconnect.config.txt", content="ok\n"), ctx
    )
    assert res.success, res.content
    await inst.destroy()


async def test_governed_guard_works_through_sandbox_session():
    # the RUNTIME passes a SandboxSession wrapper as ctx.sandbox (not a raw instance). Prove the
    # session DELEGATES resolve_relpath (governed symlink-proof) + atomic_write — without the
    # delegation these silently fell back to lexical / non-atomic write (Codex CD-TOOLS-4 round-2).
    from disco.tools.sandbox.session import SandboxSession

    session = SandboxSession(ProcessSandboxService(), owner_id="o", conversation_id="c")
    ctx = ToolContext(
        sandbox=session, workspace_path=".", timeout_s=30,
        capabilities={Capability.FILESYSTEM}, owner_id="o", conversation_id="c",
    )
    await session.write_file(".disco/appspec.json", b'{"real":1}')
    # a symlink reaching INTO .disco must be caught via the session's delegated resolve_relpath.
    import os
    ws = (await session._ensure())._workspace  # type: ignore[attr-defined]
    os.symlink(".disco/appspec.json", ws / "link.json")
    res = await SafeWriteFileTool().run(
        SafeWriteFileTool.definition.args_model(path="link.json", content="x"), ctx
    )
    assert not res.success and res.error == "SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED"
    assert (await session.read_file(".disco/appspec.json")) == b'{"real":1}'
    # atomic_write delegates: a normal write succeeds + leaves no tmp.
    ok = await SafeWriteFileTool().run(
        SafeWriteFileTool.definition.args_model(path="out/page.txt", content="hi\n"), ctx
    )
    assert ok.success, ok.content
    assert not any(n.startswith(".disco-tmp") for n in await session.list_dir("out"))
    await session.destroy()


async def test_semantic_tools_write_disco_directly_unaffected():
    # the semantic tools write .disco/ via the sandbox directly (not these generic mutators), so a
    # direct sandbox write is ungated — proving the guard is tool-level, not a sandbox-jail change.
    ctx, inst = await _ctx()
    await inst.write_file(".disco/appspec.json", b'{"spec":1}')  # what app_create does internally
    assert (await inst.read_file(".disco/appspec.json")) == b'{"spec":1}'
    await inst.destroy()
