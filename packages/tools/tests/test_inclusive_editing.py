"""Inclusive editing — the framework lets ANY model edit a LARGE file without
reproducing its exact bytes: line-number-targeted replace/insert + a forgiving
file_edit. This is what unblocks small models (and helps large ones) iterating on
big files (found live: the 27B couldn't edit a 28KB index.html via exact-match)."""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import (
    FileEditTool,
    FileInsertLinesTool,
    FileReplaceLinesTool,
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


def _args(tool, **kw):
    return tool.definition.args_model(**kw)


async def test_replace_lines_edits_a_large_file_by_line_number():
    ctx, inst = await _ctx()
    body = "\n".join(f"line {i}" for i in range(1, 501))  # 500-line file
    await inst.write_file("big.txt", body.encode())
    t = FileReplaceLinesTool()
    res = await t.run(_args(t, path="big.txt", start_line=250, end_line=252, new_text="X\nY"), ctx)
    assert res.success, res.content
    out = (await inst.read_file("big.txt")).decode().splitlines()
    assert out[248] == "line 249" and out[249] == "X" and out[250] == "Y" and out[251] == "line 253"
    assert len(out) == 499  # replaced 3 lines with 2
    await inst.destroy()


async def test_insert_lines_adds_a_block_without_replacing():
    ctx, inst = await _ctx()
    await inst.write_file("a.txt", b"one\ntwo\nthree")
    t = FileInsertLinesTool()
    res = await t.run(_args(t, path="a.txt", after_line=2, text="INSERTED"), ctx)
    assert res.success
    got = (await inst.read_file("a.txt")).decode().splitlines()
    assert got == ["one", "two", "INSERTED", "three"]
    # after_line=0 inserts at the very top
    await t.run(_args(t, path="a.txt", after_line=0, text="TOP"), ctx)
    assert (await inst.read_file("a.txt")).decode().splitlines()[0] == "TOP"
    await inst.destroy()


async def test_file_edit_is_forgiving_about_whitespace_and_pasted_line_numbers():
    ctx, inst = await _ctx()
    # original has specific indentation
    await inst.write_file("c.js", b"function f() {\n    return 1;\n}\n")
    t = FileEditTool()
    # the model supplies `old` with DRIFTED indentation + a trailing space + a pasted
    # line-number prefix — exact match would fail; forgiving match succeeds.
    res = await t.run(_args(t, path="c.js", old="2\treturn 1; ", new="    return 42;"), ctx)
    assert res.success, res.content
    assert "return 42;" in (await inst.read_file("c.js")).decode()
    await inst.destroy()


async def test_file_edit_failure_points_at_the_nearest_line():
    ctx, inst = await _ctx()
    await inst.write_file("d.txt", b"alpha\nbeta\ngamma\n")
    t = FileEditTool()
    res = await t.run(_args(t, path="d.txt", old="bета no-match xyz", new="z"), ctx)
    assert res.success is False
    assert "file_replace_lines" in res.content  # steers to the robust tool
    await inst.destroy()
