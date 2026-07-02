"""Edit-safety guards (issue D) — lock the behavior that prevents the silent
content-loss seen live on gpt-oss-120b:
  - file_replace_lines refuses an EMPTY new_text over a real range (the
    miscounted-range → wipe path),
  - file_edit refuses a no-op (old == new after ws-norm),
  - the line tools stay AVAILABLE (a weak-model affordance for large files).
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    FileAppendArgs,
    FileAppendTool,
    FileEditArgs,
    FileEditTool,
    FileReadArgs,
    FileReadTool,
    FileReplaceLinesArgs,
    FileReplaceLinesTool,
    FileWriteArgs,
    FileWriteTool,
    reset_read_tracker,
)


class _FakeSandbox:
    def __init__(self, text: str):
        self._text = text.encode("utf-8")
        self.writes: list[bytes] = []

    async def read_file(self, path: str) -> bytes:
        return self._text

    async def write_file(self, path: str, data: bytes) -> None:
        self._text = data  # update so subsequent reads see the change
        self.writes.append(data)


def _Ctx(sandbox) -> ToolContext:  # noqa: N802 — keeps old call sites unchanged
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-edit-guards",
    )


@pytest.fixture(autouse=True)
def _clear_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


_FILE = "config.py\n".join(["A = 1", "B = 2", "C = 3", "D = 4", ""])


@pytest.mark.asyncio
async def test_replace_lines_refuses_empty_new_text():
    sbx = _FakeSandbox("A = 1\nB = 2\nC = 3\nD = 4\n")
    out = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(path="config.py", start_line=2, end_line=3, new_text=""),
        _Ctx(sbx),
    )
    assert out.success is False
    assert out.error == "empty_replacement_refused"
    assert sbx.writes == []  # nothing written — the wipe is prevented


@pytest.mark.asyncio
async def test_replace_lines_still_works_with_real_text():
    sbx = _FakeSandbox("A = 1\nB = 2\nC = 3\nD = 4\n")
    out = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="config.py", start_line=2, end_line=2, new_text="B = 20"
        ),
        _Ctx(sbx),
    )
    assert out.success is True
    assert sbx.writes and b"B = 20" in sbx.writes[-1]
    assert b"A = 1" in sbx.writes[-1] and b"C = 3" in sbx.writes[-1]  # rest intact


@pytest.mark.asyncio
async def test_file_edit_refuses_noop():
    sbx = _FakeSandbox("x = 1\n")
    out = await FileEditTool().run(
        FileEditArgs(path="config.py", old="x = 1", new="x = 1"),
        _Ctx(sbx),
    )
    assert out.success is False
    assert out.error == "no_op_edit"
    assert "Fresh full current file for config.py" in out.content
    assert "\tx = 1" in out.content
    assert "this change may ALREADY be applied" not in out.content
    assert (out.structured or {})["no_op_edit_count"] == 1
    assert (out.structured or {})["delivered_read"]["full"] is True
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_edit_consecutive_noop_escalates_and_carries_content():
    sbx = _FakeSandbox("x = 1\n")
    ctx = _Ctx(sbx)

    first = await FileEditTool().run(
        FileEditArgs(path="config.py", old="x = 1", new="x = 1"),
        ctx,
    )
    second = await FileEditTool().run(
        FileEditArgs(path="config.py", old="x = 1", new="x = 1"),
        ctx,
    )

    assert first.error == "no_op_edit"
    assert second.error == "no_op_edit"
    assert "Fresh full current file for config.py" in second.content
    assert "\tx = 1" in second.content
    assert "this change may ALREADY be applied" in second.content
    assert second.content.index("Fresh full current file") < second.content.index(
        "this change may ALREADY be applied"
    )
    assert (second.structured or {})["no_op_edit_count"] == 2
    assert (second.structured or {})["delivered_read"]["full"] is True
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_edit_real_change_applies():
    sbx = _FakeSandbox("x = 1\n")
    out = await FileEditTool().run(
        FileEditArgs(path="config.py", old="x = 1", new="x = 2"),
        _Ctx(sbx),
    )
    assert out.success is True
    assert sbx.writes and b"x = 2" in sbx.writes[-1]


@pytest.mark.asyncio
async def test_file_edit_real_change_after_noop_succeeds_and_resets_noop_counter():
    sbx = _FakeSandbox("x = 1\n")
    ctx = _Ctx(sbx)

    refused = await FileEditTool().run(
        FileEditArgs(path="config.py", old="x = 1", new="x = 1"),
        ctx,
    )
    changed = await FileEditTool().run(
        FileEditArgs(path="config.py", old="x = 1", new="x = 2"),
        ctx,
    )
    refused_after_success = await FileEditTool().run(
        FileEditArgs(path="config.py", old="x = 2", new="x = 2"),
        ctx,
    )

    assert refused.error == "no_op_edit"
    assert changed.success is True, changed.content
    assert sbx.writes and b"x = 2" in sbx.writes[-1]
    assert refused_after_success.error == "no_op_edit"
    assert "this change may ALREADY be applied" not in refused_after_success.content
    assert (refused_after_success.structured or {})["no_op_edit_count"] == 1


@pytest.mark.asyncio
async def test_file_edit_whitespace_only_change_applies():
    """A pure re-indent must NOT be refused as a no-op. The old guard compared
    ws-normalized old==new and wrongly rejected it; the ground-truth guard checks
    the APPLIED result, which differs from disk (new is written verbatim)."""
    sbx = _FakeSandbox("def f():\n        return 1\n")  # over-indented body
    out = await FileEditTool().run(
        # match ws-tolerantly on the body line, rewrite it at 4-space indent
        FileEditArgs(path="config.py", old="        return 1", new="    return 1"),
        _Ctx(sbx),
    )
    assert out.success is True, out.content
    assert sbx.writes and b"\n    return 1\n" in sbx.writes[-1]
    assert b"        return 1" not in sbx.writes[-1]  # old indentation gone


@pytest.mark.asyncio
async def test_file_edit_noop_caught_on_ground_truth():
    """Even when old != new textually, if applying the edit leaves the file
    byte-identical (the new content already matches disk), it's still refused."""
    sbx = _FakeSandbox("x = 1\n")
    out = await FileEditTool().run(
        # `old` carries a stray line-number prefix the model copied; stripped it's
        # "x = 1" → matches disk → replacement equals disk → genuine no-op.
        FileEditArgs(path="config.py", old="3\tx = 1", new="x = 1"),
        _Ctx(sbx),
    )
    assert out.success is False
    assert out.error == "no_op_edit"
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_write_identical_bytes_refused_with_content_and_escalates():
    sbx = _FakeSandbox("x = 1\n")
    ctx = _Ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="config.py"), ctx)

    first = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="x = 1\n"),
        ctx,
    )
    second = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="x = 1\n"),
        ctx,
    )

    assert first.success is False
    assert first.error == "no_op_write"
    assert (
        "file_write refused: config.py already contains exactly this content"
        in first.content
    )
    assert "Fresh full current file for config.py" in first.content
    assert "\tx = 1" in first.content
    assert "Repeated no-op write" not in first.content
    assert (first.structured or {})["kind"] == "no_op_write"
    assert (first.structured or {})["no_op_write_count"] == 1
    assert (first.structured or {})["delivered_read"]["full"] is True

    assert second.success is False
    assert second.error == "no_op_write"
    assert "Repeated no-op write" in second.content
    assert "ALREADY contain the intended change" in second.content
    assert "\tx = 1" in second.content
    assert (second.structured or {})["no_op_write_count"] == 2
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_write_different_after_noop_succeeds_and_resets_noop_counter():
    sbx = _FakeSandbox("x = 1\n")
    ctx = _Ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="config.py"), ctx)

    refused = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="x = 1\n"),
        ctx,
    )
    changed = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="x = 2\n"),
        ctx,
    )
    await FileReadTool().run(FileReadArgs(path="config.py"), ctx)
    refused_after_success = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="x = 2\n"),
        ctx,
    )

    assert refused.error == "no_op_write"
    assert changed.success is True, changed.content
    assert sbx.writes and sbx.writes[-1] == b"x = 2\n"
    assert refused_after_success.error == "no_op_write"
    assert "Repeated no-op write" not in refused_after_success.content
    assert (refused_after_success.structured or {})["no_op_write_count"] == 1


@pytest.mark.asyncio
async def test_file_append_empty_effect_refused_as_no_op_write():
    sbx = _FakeSandbox("line\n")
    out = await FileAppendTool().run(
        FileAppendArgs(path="log.txt", content=""),
        _Ctx(sbx),
    )

    assert out.success is False
    assert out.error == "no_op_write"
    assert "file_append refused: log.txt already contains exactly this content" in out.content
    assert "Fresh full current file for log.txt" in out.content
    assert "\tline" in out.content
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_write_identical_unread_existing_file_hits_read_before_write_first():
    sbx = _FakeSandbox("x = 1\n")
    out = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="x = 1\n"),
        _Ctx(sbx),
    )

    assert out.success is False
    assert out.error == "read_before_write"
    assert "file_read" in out.content
    assert "already contains exactly this content" not in out.content
    assert sbx.writes == []
