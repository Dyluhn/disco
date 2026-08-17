"""Folded safe-write guards on file_write."""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import FileReadTool, FileWriteTool
from disco.tools.builtin.files import reset_read_tracker
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


async def _ctx():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    ctx = ToolContext(
        sandbox=inst,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="c",
    )
    return ctx, inst


def _a(**kw):
    return FileWriteTool.definition.args_model(**kw)


async def _ground(ctx: ToolContext, path: str) -> None:
    await FileReadTool().run(FileReadTool.definition.args_model(path=path), ctx)


async def test_file_write_shrink_over_50pct_rejected_with_recipe() -> None:
    ctx, inst = await _ctx()
    body = b"x" * 1000 + b"\n"
    await inst.write_file("big.txt", body)
    await _ground(ctx, "big.txt")

    res = await FileWriteTool().run(_a(path="big.txt", content="tiny\n"), ctx)

    assert not res.success
    assert res.error == "FILE_WRITE_SHRINK_REJECTED"
    assert "allow_shrink=true" in res.content
    assert (await inst.read_file("big.txt")) == body
    await inst.destroy()


async def test_file_write_allow_shrink_applies() -> None:
    ctx, inst = await _ctx()
    body = b"x" * 1000 + b"\n"
    await inst.write_file("big.txt", body)
    await _ground(ctx, "big.txt")

    res = await FileWriteTool().run(_a(path="big.txt", content="tiny\n", allow_shrink=True), ctx)

    assert res.success, res.content
    assert (await inst.read_file("big.txt")) == b"tiny\n"
    await inst.destroy()


async def test_file_write_new_file_unaffected_by_shrink_guard() -> None:
    ctx, inst = await _ctx()

    res = await FileWriteTool().run(_a(path="new.txt", content="tiny\n"), ctx)

    assert res.success, res.content
    assert (await inst.read_file("new.txt")) == b"tiny\n"
    await inst.destroy()


async def test_file_write_governed_disco_path_rejected() -> None:
    ctx, inst = await _ctx()

    res = await FileWriteTool().run(_a(path=".disco/appspec.json", content='{"x":1}'), ctx)

    assert not res.success
    assert res.error == "GOVERNED_ARTIFACT_REJECTED"
    assert not await inst.file_exists(".disco/appspec.json")
    await inst.destroy()


async def test_file_write_elision_marker_rejected_no_write() -> None:
    ctx, inst = await _ctx()

    res = await FileWriteTool().run(_a(path="e.txt", content="a <4500 chars elided> b"), ctx)

    assert not res.success
    assert res.error == "ELISION_MARKER_REJECTED"
    assert not await inst.file_exists("e.txt")
    await inst.destroy()


async def test_file_write_atomic_write_leaves_no_leftover_tmp() -> None:
    ctx, inst = await _ctx()

    res = await FileWriteTool().run(_a(path="sub/dir/a.txt", content="content\n"), ctx)

    assert res.success, res.content
    listing = await inst.list_dir("sub/dir")
    assert not any(name.startswith(".disco-tmp") for name in listing), listing
    await inst.destroy()
