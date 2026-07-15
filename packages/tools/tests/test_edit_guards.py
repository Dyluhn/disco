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
    def __init__(self, text: str):
        self._text = text.encode("utf-8")
        self.writes: list[bytes] = []

    async def read_file(self, path: str) -> bytes:
        return self._text

    async def write_file(self, path: str, data: bytes) -> None:
        self._text = data  # update so subsequent reads see the change
        self.writes.append(data)


def _Ctx(  # noqa: N802 — keeps old call sites unchanged
    sandbox,
    *,
    conv_id: str = "conv-edit-guards",
    read_char_budget: int | None = None,
) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv_id,
        read_char_budget=read_char_budget,
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
        FileReplaceLinesArgs(path="config.py", start_line=2, end_line=2, new_text="B = 20"),
        _Ctx(sbx),
    )
    assert out.success is True
    assert sbx.writes and b"B = 20" in sbx.writes[-1]
    assert b"A = 1" in sbx.writes[-1] and b"C = 3" in sbx.writes[-1]  # rest intact


@pytest.mark.asyncio
async def test_replace_lines_refuses_byte_identical_replacement():
    sbx = _FakeSandbox("A = 1\nB = 2\n")
    out = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(path="config.py", start_line=2, end_line=2, new_text="B = 2"),
        _Ctx(sbx),
    )
    assert out.success is False
    assert out.error == "no_op_edit"
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_insert_lines_refuses_empty_byte_identical_insertion():
    sbx = _FakeSandbox("")
    out = await FileInsertLinesTool().run(
        FileInsertLinesArgs(path="empty.txt", after_line=0, text=""),
        _Ctx(sbx),
    )
    assert out.success is False
    assert out.error == "no_op_edit"
    assert sbx.writes == []


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
async def test_file_edit_old_text_not_found_carries_content_and_delivered_read_for_small_file():
    sbx = _FakeSandbox("alpha = 1\nbeta = 2\n")

    out = await FileEditTool().run(
        FileEditArgs(path="config.py", old="gamma = 3", new="gamma = 4"),
        _Ctx(sbx),
    )

    assert out.success is False
    assert out.error == "old_text_not_found"
    assert "file_edit refused: your `old` text was not found in config.py" in out.content
    assert "Fresh full current file for config.py" in out.content
    assert "\talpha = 1" in out.content
    assert "\tbeta = 2" in out.content
    assert "Anchor your next edit on the CURRENT text shown above" in out.content
    assert (out.structured or {})["kind"] == "old_text_not_found"
    assert (out.structured or {})["old_text_not_found_count"] == 1
    assert (out.structured or {})["delivered_read"]["full"] is True
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_edit_large_old_text_not_found_carries_best_match_window():
    lines = [f"line {i} {'x' * 100}" for i in range(1, 701)]
    lines[359] = "line 360 target_value = 'current live value'"
    large_text = "\n".join(lines) + "\n"
    assert len(large_text.encode("utf-8")) > 64 * 1024
    sbx = _FakeSandbox(large_text)
    ctx = _Ctx(
        sbx,
        conv_id="conv-edit-guards-large-miss",
        read_char_budget=len(large_text) + 1024,
    )
    assert (await FileReadTool().run(FileReadArgs(path="large.txt"), ctx)).success

    first = await FileEditTool().run(
        FileEditArgs(
            path="large.txt",
            old="line 360 target_value = 'stale composed value'",
            new="replacement",
        ),
        ctx,
    )
    second = await FileEditTool().run(
        FileEditArgs(
            path="large.txt",
            old="line 360 target_value = 'stale composed value'",
            new="replacement",
        ),
        ctx,
    )

    assert first.success is False
    assert first.error == "old_text_not_found"
    assert "Fresh full current file" not in first.content
    assert "Best fuzzy match region in the current file" in first.content
    assert "Fresh current window for large.txt" in first.content
    assert "\tline 360 target_value = 'current live value'" in first.content
    assert "\tline 1 " not in first.content
    assert "\tline 700 " not in first.content
    assert (first.structured or {})["delivered_read"]["full"] is False
    assert (first.structured or {})["old_text_not_found_count"] == 1

    assert second.success is False
    assert second.error == "old_text_not_found"
    assert "Fresh full current file" not in second.content
    assert "Fresh current window for large.txt" in second.content
    assert "do NOT re-send it" in second.content
    assert (second.structured or {})["delivered_read"]["full"] is False
    assert (second.structured or {})["old_text_not_found_count"] == 2
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_edit_old_text_not_found_reports_replacement_already_present():
    sbx = _FakeSandbox("alpha = 1\nbeta = 20\n")

    out = await FileEditTool().run(
        FileEditArgs(path="config.py", old="beta = 02", new="beta = 20"),
        _Ctx(sbx),
    )

    assert out.success is False
    assert out.error == "old_text_not_found"
    assert (
        "the replacement text is already present at lines 2-2 — the edit already "
        "applied; do not re-issue it."
    ) in out.content
    assert "Anchor your next edit" not in out.content
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_edit_old_text_not_found_successful_edit_resets_counter():
    sbx = _FakeSandbox("alpha = 1\nbeta = 2\n")
    ctx = _Ctx(sbx, conv_id="conv-edit-guards-miss-reset")

    first = await FileEditTool().run(
        FileEditArgs(path="config.py", old="missing one", new="replacement"),
        ctx,
    )
    second = await FileEditTool().run(
        FileEditArgs(path="config.py", old="missing two", new="replacement"),
        ctx,
    )
    changed = await FileEditTool().run(
        FileEditArgs(path="config.py", old="alpha = 1", new="alpha = 10"),
        ctx,
    )
    after_success = await FileEditTool().run(
        FileEditArgs(path="config.py", old="missing three", new="replacement"),
        ctx,
    )

    assert first.error == "old_text_not_found"
    assert second.error == "old_text_not_found"
    assert "exact current content is above" in second.content
    assert changed.success is True, changed.content
    assert after_success.error == "old_text_not_found"
    assert "do NOT re-send it" not in after_success.content
    assert (after_success.structured or {})["old_text_not_found_count"] == 1


@pytest.mark.asyncio
async def test_file_edit_corrected_retry_after_old_text_not_found_delivered_content_succeeds():
    sbx = _FakeSandbox("alpha = 1\nbeta = 2\n")
    ctx = _Ctx(sbx, conv_id="conv-edit-guards-miss-retry")

    refused = await FileEditTool().run(
        FileEditArgs(path="config.py", old="beta = 3", new="beta = 20"),
        ctx,
    )
    retry = await FileEditTool().run(
        FileEditArgs(path="config.py", old="beta = 2", new="beta = 20"),
        ctx,
    )

    assert refused.error == "old_text_not_found"
    assert "Fresh full current file for config.py" in refused.content
    assert (refused.structured or {})["delivered_read"]["full"] is True
    assert retry.success is True, retry.content
    assert sbx.writes and b"beta = 20" in sbx.writes[-1]


@pytest.mark.asyncio
async def test_file_edit_unicode_decorative_drift_finds_fuzzy_window():
    lines = [f"line {i} {'x' * 100}" for i in range(1, 701)]
    lines[419] = "line 420 # ═══ Section: pricing controls"
    large_text = "\n".join(lines) + "\n"
    assert len(large_text.encode("utf-8")) > 64 * 1024
    sbx = _FakeSandbox(large_text)
    ctx = _Ctx(
        sbx,
        conv_id="conv-edit-guards-unicode-drift",
        read_char_budget=len(large_text) + 1024,
    )
    assert (await FileReadTool().run(FileReadArgs(path="large.txt"), ctx)).success

    out = await FileEditTool().run(
        FileEditArgs(
            path="large.txt",
            old="line 420 # === Section: pricing controls",
            new="line 420 # Pricing controls",
        ),
        ctx,
    )

    assert out.success is False
    assert out.error == "old_text_not_found"
    assert "Best fuzzy match region in the current file" in out.content
    assert "\tline 420 # ═══ Section: pricing controls" in out.content
    assert (out.structured or {})["delivered_read"]["full"] is False
    assert sbx.writes == []


@pytest.mark.asyncio
async def test_file_str_replace_and_exact_replace_old_text_misses_use_recovery_refusal():
    sbx = _FakeSandbox("alpha = 1\nbeta = 2\n")

    str_miss = await FileStrReplaceTool().run(
        FileStrReplaceArgs(path="config.py", old_str="gamma = 3", new_str="gamma = 4"),
        _Ctx(sbx, conv_id="conv-edit-guards-str-miss"),
    )
    exact_miss = await ExactReplaceTool().run(
        ExactReplaceTool.definition.args_model(
            path="config.py",
            edits=[{"old_string": "gamma = 3", "new_string": "gamma = 4"}],
        ),
        _Ctx(sbx, conv_id="conv-edit-guards-exact-miss"),
    )

    assert str_miss.success is False
    assert str_miss.error == "old_str_not_found"
    assert "Fresh full current file for config.py" in str_miss.content
    assert (str_miss.structured or {})["delivered_read"]["full"] is True

    assert exact_miss.success is False
    assert exact_miss.error == "EXACT_REPLACE_NO_MATCH"
    assert "Fresh full current file for config.py" in exact_miss.content
    assert (exact_miss.structured or {})["delivered_read"]["full"] is True
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
    assert "file_write refused: config.py already contains exactly this content" in first.content
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
