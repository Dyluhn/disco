"""Edit-safety guards (issue D) — lock the behavior that prevents the silent
content-loss seen live on gpt-oss-120b:
  - file_replace_lines refuses an EMPTY new_text over a real range (the
    miscounted-range → wipe path),
  - file_edit refuses a no-op (old == new after ws-norm),
  - the line tools stay AVAILABLE (a weak-model affordance for large files).
"""

from __future__ import annotations

import pytest

from disco.tools.builtin.files import (
    FileEditArgs,
    FileEditTool,
    FileReplaceLinesArgs,
    FileReplaceLinesTool,
)


class _FakeSandbox:
    def __init__(self, text: str):
        self._text = text.encode("utf-8")
        self.writes: list[bytes] = []

    async def read_file(self, path: str) -> bytes:
        return self._text

    async def write_file(self, path: str, data: bytes) -> None:
        self.writes.append(data)


class _Ctx:
    def __init__(self, sandbox):
        self.sandbox = sandbox


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
