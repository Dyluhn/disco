"""[REL-RC-G] Line-edit refusals are self-recovering targeted reads.

The guard still blocks stale/blind line edits, but those refusals now carry fresh numbered
content and credit it only for targeted edits. They must not unlock blind file_write.
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    FileEditArgs,
    FileEditTool,
    FileInsertLinesArgs,
    FileInsertLinesTool,
    FileReadArgs,
    FileReadTool,
    FileReplaceLinesArgs,
    FileReplaceLinesTool,
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
            raise FileNotFoundError(f"no such file: {path}")
        return self._fs[key]

    async def write_file(self, path: str, data: bytes) -> None:
        key = self._strip(path)
        self._fs[key] = data
        self.writes.append((path, data))


def _ctx(sbx: _FakeSandbox, conv_id: str = "conv-rcg") -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv_id,
        assist=False,
    )


@pytest.fixture(autouse=True)
def _clear_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


_FILLER = "\n".join(
    f"<p>row {i}: lorem ipsum dolor sit amet consectetur adipiscing</p>" for i in range(1, 41)
)
BIG = f"<html><body>\n<h1>Acme Cloud</h1>\n{_FILLER}\n<footer>OLD FOOTER</footer>\n</body></html>\n"
BIG_BYTES = BIG.encode("utf-8")
assert len(BIG_BYTES) > 1500
assert len(BIG_BYTES) <= 16 * 1024

_MID_LINES = [f"<p>pricing row {i}: {'x' * 100}</p>" for i in range(1, 191)]
MID = "<html><body>\n<h1>Acme Cloud</h1>\n" + "\n".join(_MID_LINES) + "\n</body></html>\n"
MID_BYTES = MID.encode("utf-8")
assert len(MID_BYTES) > 16 * 1024
assert len(MID_BYTES) <= 64 * 1024

_LARGE_LINES = [f"line {i} {'x' * 100}" for i in range(1, 701)]
LARGE = "\n".join(_LARGE_LINES) + "\n"
LARGE_BYTES = LARGE.encode("utf-8")
assert len(LARGE_BYTES) > 64 * 1024


@pytest.mark.asyncio
async def test_outside_success_view_refusal_delivers_numbered_content_and_retry_succeeds():
    """Outside-view refusal provides content; a corrected retry then succeeds."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)

    assert (await FileReadTool().run(FileReadArgs(path="index.html"), ctx)).success
    first = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="index.html",
            start_line=2,
            end_line=2,
            new_text="<h1>Acme</h1>\n<h2>Cloud</h2>",
        ),
        ctx,
    )
    assert first.success, first.content
    assert "applied — lines 1-13 now read:" in first.content
    assert "\t<h2>Cloud</h2>" in first.content

    outside_view = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="index.html",
            start_line=43,
            end_line=43,
            new_text="<footer>NEW FOOTER</footer>",
        ),
        ctx,
    )
    assert outside_view.success is False
    assert outside_view.error == "FRESH_READ_REQUIRED"
    assert "Fresh full current file for index.html" in outside_view.content
    assert "Line numbers may have shifted" in outside_view.content
    assert "\t<footer>OLD FOOTER</footer>" in outside_view.content
    assert (outside_view.structured or {})["delivered_read"]["full"] is True

    retry = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="index.html",
            start_line=44,
            end_line=44,
            new_text="<footer>NEW FOOTER</footer>",
        ),
        ctx,
    )
    assert retry.success, retry.content
    assert b"<footer>NEW FOOTER</footer>" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_same_line_count_replace_advances_grounding_for_consecutive_line_edits():
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)

    assert (await FileReadTool().run(FileReadArgs(path="index.html"), ctx)).success
    first = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="index.html",
            start_line=2,
            end_line=2,
            new_text="<h1>Acme Cloud Pro</h1>",
        ),
        ctx,
    )
    assert first.success, first.content

    second = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="index.html",
            start_line=3,
            end_line=3,
            new_text="<p>row 1: updated</p>",
        ),
        ctx,
    )
    assert second.success, second.content
    assert b"Acme Cloud Pro" in sbx._fs["index.html"]
    assert b"row 1: updated" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_refusal_delivered_content_does_not_unlock_blind_file_write():
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)

    refused = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="index.html",
            start_line=2,
            end_line=2,
            new_text="<h1>Acme Cloud Pro</h1>",
        ),
        ctx,
    )
    assert refused.success is False
    assert refused.error == "FRESH_READ_REQUIRED"
    assert "Fresh full current file for index.html" in refused.content

    write = await FileWriteTool().run(
        FileWriteArgs(path="index.html", content=BIG.replace("OLD FOOTER", "BLIND FOOTER")),
        ctx,
    )
    assert write.success is False
    assert write.error == "read_before_write"
    assert sbx._fs["index.html"] == BIG_BYTES


@pytest.mark.asyncio
async def test_large_line_refusal_delivers_bounded_window():
    sbx = _FakeSandbox({"large.txt": LARGE_BYTES})
    ctx = _ctx(sbx)

    refused = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="large.txt", start_line=120, end_line=120, new_text="line 120 edited"
        ),
        ctx,
    )
    assert refused.success is False
    assert refused.error == "FRESH_READ_REQUIRED"
    assert (
        "Fresh current window for large.txt [lines 80-160 of 700; total lines: 700]"
        in refused.content
    )
    delivered = (refused.structured or {})["delivered_read"]
    assert delivered["full"] is False
    assert delivered["ranges"] == [(80, 160)]
    assert "\tline 120 " in refused.content

    retry = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="large.txt", start_line=120, end_line=120, new_text="line 120 edited"
        ),
        ctx,
    )
    assert retry.success, retry.content
    assert b"line 120 edited" in sbx._fs["large.txt"]


@pytest.mark.asyncio
async def test_mid_file_insert_refusal_delivers_full_content_and_retry_succeeds():
    sbx = _FakeSandbox({"index.html": MID_BYTES})
    ctx = _ctx(sbx)

    refused = await FileInsertLinesTool().run(
        FileInsertLinesArgs(path="index.html", after_line=2, text="<section>Enterprise</section>"),
        ctx,
    )
    assert refused.success is False
    assert refused.error == "FRESH_READ_REQUIRED"
    assert "Fresh full current file for index.html" in refused.content
    assert "\t<h1>Acme Cloud</h1>" in refused.content
    assert (refused.structured or {})["delivered_read"]["full"] is True

    retry = await FileInsertLinesTool().run(
        FileInsertLinesArgs(path="index.html", after_line=2, text="<section>Enterprise</section>"),
        ctx,
    )
    assert retry.success, retry.content
    assert b"<section>Enterprise</section>" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_mid_file_edit_refusal_delivers_full_content_retry_succeeds_and_write_stays_blocked():
    sbx = _FakeSandbox({"index.html": MID_BYTES})
    ctx = _ctx(sbx)

    refused = await FileEditTool().run(
        FileEditArgs(path="index.html", old="<h1>Wrong</h1>", new="<h1>Enterprise</h1>"),
        ctx,
    )
    assert refused.success is False
    assert refused.error == "FRESH_READ_REQUIRED"
    assert "Fresh full current file for index.html" in refused.content
    assert "\t<h1>Acme Cloud</h1>" in refused.content
    assert (refused.structured or {})["delivered_read"]["full"] is True

    write = await FileWriteTool().run(
        FileWriteArgs(path="index.html", content=MID.replace("Acme Cloud", "Blind Rewrite")),
        ctx,
    )
    assert write.success is False
    assert write.error == "read_before_write"

    retry = await FileEditTool().run(
        FileEditArgs(path="index.html", old="<h1>Acme Cloud</h1>", new="<h1>Enterprise</h1>"),
        ctx,
    )
    assert retry.success, retry.content
    assert b"<h1>Enterprise</h1>" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_large_anchored_file_edit_refusal_delivers_no_content():
    sbx = _FakeSandbox({"large.html": LARGE_BYTES})
    ctx = _ctx(sbx)

    refused = await FileEditTool().run(
        FileEditArgs(path="large.html", old="line 120", new="line 120 edited"),
        ctx,
    )
    assert refused.success is False
    assert refused.error == "FRESH_READ_REQUIRED"
    assert "Fresh full current file" not in refused.content
    assert "Fresh current window" not in refused.content
    assert "line 120" not in refused.content
    assert "delivered_read" not in (refused.structured or {})


@pytest.mark.asyncio
async def test_insert_success_observation_includes_updated_numbered_window():
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)

    assert (await FileReadTool().run(FileReadArgs(path="index.html"), ctx)).success
    inserted = await FileInsertLinesTool().run(
        FileInsertLinesArgs(path="index.html", after_line=2, text="<section>Pricing</section>"),
        ctx,
    )
    assert inserted.success, inserted.content
    assert "applied — lines 1-13 now read:" in inserted.content
    assert "\t<section>Pricing</section>" in inserted.content
