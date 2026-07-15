"""Cluster 4 — filesystem-as-memory: file_read offset/limit, file_append,
and lazy/scoped skill injection."""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import (
    FileAppendTool,
    FileReadTool,
    FileWriteTool,
)
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxService

pytestmark = pytest.mark.asyncio


async def _ctx():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    return ToolContext(
        sandbox=inst,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="c",
    ), inst


# ---- C-1: file_read offset/limit --------------------------------------------


async def test_file_read_whole_file_unchanged():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"line1\nline2\nline3")
    out = await FileReadTool().run(FileReadTool().definition.args_model(path="a.txt"), ctx)
    assert all(f"\t{ln}" in out.content for ln in ("line1", "line2", "line3"))  # numbered
    await inst.destroy()


async def test_file_read_line_range_slice():
    ctx, inst = await _ctx()
    body = "\n".join(f"line{i}" for i in range(1, 21))  # 20 lines
    await inst.write_file("big.txt", body.encode())
    out = await FileReadTool().run(
        FileReadTool().definition.args_model(path="big.txt", offset=5, limit=3), ctx
    )
    assert "line5" in out.content
    assert "line7" in out.content
    assert "line8" not in out.content
    assert "lines 5-7 of 20" in out.content
    assert "read more with offset=8" in out.content
    await inst.destroy()


async def test_file_read_offset_past_end_is_safe():
    ctx, inst = await _ctx()
    await inst.write_file("s.txt", b"only one line")
    out = await FileReadTool().run(
        FileReadTool().definition.args_model(path="s.txt", offset=50, limit=10), ctx
    )
    # No crash; reports the range honestly.
    assert "of 1" in out.content
    await inst.destroy()


# ---- file_append (the >> replacement) ---------------------------------------


async def test_file_append_creates_then_appends():
    ctx, inst = await _ctx()
    await FileAppendTool().run(
        FileAppendTool().definition.args_model(path="log.md", content="first\n"), ctx
    )
    await FileAppendTool().run(
        FileAppendTool().definition.args_model(path="log.md", content="second\n"), ctx
    )
    data = (await inst.read_file("log.md")).decode()
    assert data == "first\nsecond\n"
    await inst.destroy()


async def test_file_append_returns_artifact_path():
    ctx, inst = await _ctx()
    out = await FileWriteTool().run(
        FileWriteTool().definition.args_model(path="x.txt", content="seed"), ctx
    )
    assert out.artifacts == ["x.txt"]
    out2 = await FileAppendTool().run(
        FileAppendTool().definition.args_model(path="x.txt", content="more"), ctx
    )
    assert out2.artifacts == ["x.txt"]
    await inst.destroy()


# ---- ROOT-2 (slides spiral): binary-deliverable clobber guard ---------------


async def test_file_write_refuses_text_into_existing_binary_deliverable():
    """A file_write (text-only) targeting an EXISTING generated binary (.pptx, …)
    is refused with a clear terminal message — it would corrupt the artifact, and
    the confused post-generation agent kept trying exactly this."""
    ctx, inst = await _ctx()
    # Simulate a generated, delivered deck on disk.
    await inst.write_file("local_llms_2026.pptx", b"PK\x03\x04 binary pptx bytes")
    out = await FileWriteTool().run(
        FileWriteTool().definition.args_model(path="local_llms_2026.pptx", content="placeholder"),
        ctx,
    )
    assert out.success is False
    assert out.error == "binary_deliverable_clobber"
    assert "generated binary deliverable" in out.content
    # The on-disk bytes are untouched (not clobbered with the text).
    assert (await inst.read_file("local_llms_2026.pptx")).startswith(b"PK\x03\x04")
    await inst.destroy()


async def test_file_write_allows_new_text_file():
    """The guard must NOT block authoring a brand-new text file of any name."""
    ctx, inst = await _ctx()
    out = await FileWriteTool().run(
        FileWriteTool().definition.args_model(path="notes.txt", content="hello"),
        ctx,
    )
    assert out.success is True
    assert (await inst.read_file("notes.txt")).decode() == "hello"
    await inst.destroy()
