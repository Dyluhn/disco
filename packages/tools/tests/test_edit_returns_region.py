"""SPEC-edit-returns-region: successful mutators return and ground current regions."""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    ExactReplaceTool,
    FileAppendArgs,
    FileAppendTool,
    FileEditArgs,
    FileEditTool,
    FileInsertLinesArgs,
    FileInsertLinesTool,
    FileReadArgs,
    FileReadTool,
    FileReplaceLinesArgs,
    FileReplaceLinesTool,
    FileStrReplaceArgs,
    FileStrReplaceTool,
    FileWriteArgs,
    FileWriteTool,
    reset_read_tracker,
)


class _FakeSandbox:
    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        from disco.tools.sandbox.base import strip_redundant_workspace_prefix as _strip

        self._fs: dict[str, bytes] = {_strip(k): v for k, v in (existing or {}).items()}
        self.writes: list[tuple[str, bytes]] = []
        self._strip = _strip

    async def read_file(self, path: str) -> bytes:
        key = self._strip(path)
        if key not in self._fs:
            raise FileNotFoundError(path)
        return self._fs[key]

    async def write_file(self, path: str, data: bytes) -> None:
        key = self._strip(path)
        self._fs[key] = data
        self.writes.append((path, data))


def _ctx(
    sbx: _FakeSandbox,
    *,
    conv_id: str = "conv-edit-returns-region",
    read_char_budget: int | None = None,
) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv_id,
        assist=False,
        read_char_budget=read_char_budget,
    )


@pytest.fixture(autouse=True)
def _clear_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


def _numbered_lines(count: int, *, pad: int = 60) -> str:
    return "\n".join(f"line {i} {'x' * pad}" for i in range(1, count + 1)) + "\n"


@pytest.mark.asyncio
async def test_edit_success_returns_plus_minus_10_numbered_region_and_grounds_it():
    text = _numbered_lines(30)
    sbx = _FakeSandbox({"big.txt": text.encode("utf-8")})
    ctx = _ctx(sbx, read_char_budget=len(text) + 1024)
    assert (await FileReadTool().run(FileReadArgs(path="big.txt"), ctx)).success

    first = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="big.txt", start_line=15, end_line=15, new_text="line 15 edited"
        ),
        ctx,
    )

    assert first.success, first.content
    assert "applied — lines 5-25 now read:" in first.content
    assert "\tline 5 " in first.content
    assert "\tline 15 edited" in first.content
    assert "\tline 25 " in first.content
    assert "\tline 4 " not in first.content
    assert "\tline 26 " not in first.content

    second = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="big.txt", start_line=16, end_line=16, new_text="line 16 edited"
        ),
        ctx,
    )
    assert second.success, second.content
    assert b"line 16 edited" in sbx._fs["big.txt"]


@pytest.mark.asyncio
async def test_success_on_file_a_does_not_ground_unread_file_b():
    body_a = _numbered_lines(30)
    body_b = _numbered_lines(30)
    sbx = _FakeSandbox({"a.txt": body_a.encode("utf-8"), "b.txt": body_b.encode("utf-8")})
    ctx = _ctx(
        sbx,
        conv_id="conv-edit-returns-region-cross",
        read_char_budget=len(body_a) + 1024,
    )
    assert (await FileReadTool().run(FileReadArgs(path="a.txt"), ctx)).success
    changed_a = await FileEditTool().run(
        FileEditArgs(path="a.txt", old="line 15 " + "x" * 60, new="line 15 edited"),
        ctx,
    )
    assert changed_a.success, changed_a.content

    refused_b = await FileEditTool().run(
        FileEditArgs(path="b.txt", old="line 15 " + "x" * 60, new="line 15 edited"),
        ctx,
    )
    assert refused_b.success is False
    assert refused_b.error == "FRESH_READ_REQUIRED"
    assert sbx._fs["b.txt"] == body_b.encode("utf-8")


@pytest.mark.asyncio
async def test_file_write_returns_first_40_lines_total_count_and_grounds_path():
    content = _numbered_lines(45, pad=40)
    sbx = _FakeSandbox()
    ctx = _ctx(sbx, conv_id="conv-edit-returns-region-write")

    written = await FileWriteTool().run(FileWriteArgs(path="write.txt", content=content), ctx)

    assert written.success, written.content
    assert "applied — lines 1-40 now read:" in written.content
    assert "[total lines: 45]" in written.content
    assert "\tline 40 " in written.content
    assert "\tline 41 " not in written.content

    edited = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="write.txt", start_line=20, end_line=20, new_text="line 20 edited"
        ),
        ctx,
    )
    assert edited.success, edited.content
    assert b"line 20 edited" in sbx._fs["write.txt"]


@pytest.mark.asyncio
async def test_success_region_view_is_capped_at_64kb():
    text = _numbered_lines(30, pad=5000)
    sbx = _FakeSandbox({"huge.txt": text.encode("utf-8")})
    ctx = _ctx(
        sbx,
        conv_id="conv-edit-returns-region-cap",
        read_char_budget=len(text) + 1024,
    )
    assert (await FileReadTool().run(FileReadArgs(path="huge.txt"), ctx)).success

    out = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="huge.txt", start_line=15, end_line=15, new_text="line 15 edited"
        ),
        ctx,
    )

    assert out.success, out.content
    assert "[updated-region view truncated at 64KB]" in out.content
    assert len(out.content.encode("utf-8")) < 66 * 1024


@pytest.mark.asyncio
async def test_all_listed_mutators_include_applied_region_header_on_success():
    cases: list[tuple[str, object, object, str]] = [
        (
            "file_edit",
            FileEditTool(),
            FileEditArgs(path="file_edit.txt", old="alpha", new="bravo"),
            "file_edit.txt",
        ),
        (
            "file_str_replace",
            FileStrReplaceTool(),
            FileStrReplaceArgs(path="file_str_replace.txt", old_str="alpha", new_str="bravo"),
            "file_str_replace.txt",
        ),
        (
            "exact_replace",
            ExactReplaceTool(),
            ExactReplaceTool.definition.args_model(
                path="exact_replace.txt",
                edits=[{"old_string": "alpha", "new_string": "bravo"}],
            ),
            "exact_replace.txt",
        ),
        (
            "file_replace_lines",
            FileReplaceLinesTool(),
            FileReplaceLinesArgs(
                path="file_replace_lines.txt",
                start_line=1,
                end_line=1,
                new_text="bravo",
            ),
            "file_replace_lines.txt",
        ),
        (
            "file_insert_lines",
            FileInsertLinesTool(),
            FileInsertLinesArgs(path="file_insert_lines.txt", after_line=1, text="bravo"),
            "file_insert_lines.txt",
        ),
        (
            "file_append",
            FileAppendTool(),
            FileAppendArgs(path="file_append.txt", content="bravo\n"),
            "file_append.txt",
        ),
        (
            "file_write",
            FileWriteTool(),
            FileWriteArgs(path="file_write.txt", content="bravo\n"),
            "file_write.txt",
        ),
    ]

    for idx, (name, tool, args, path) in enumerate(cases):
        existing = {} if name == "file_write" else {path: b"alpha\n"}
        sbx = _FakeSandbox(existing)
        ctx = _ctx(sbx, conv_id=f"conv-edit-returns-region-mutator-{idx}")
        out = await tool.run(args, ctx)  # type: ignore[attr-defined]
        assert out.success, out.content
        assert "applied — lines " in out.content
        assert "now read:" in out.content
