"""[REL-RC-D] Anchored edits advance grounding to the post-edit bytes.

Root cause fixed: on a successful mutation the next same-file edit used to see stale
pre-edit grounding and get refused as STALE_FILE_CONTEXT. Successful mutators now return
a numbered current-region observation and ground that visible region at the post-edit sha.
Text-anchored follow-up edits can also retain complete textual knowledge from a full read or
model-authored whole file. Genuinely unseen regions and shifted line-number edits still fail closed.

The fresh-read guard only engages for files > 1500 bytes (small files stay fully in context), so
these fixtures use a >1500-byte file.
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


def _ctx(sbx: _FakeSandbox, conv_id: str = "conv-rcd") -> ToolContext:
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


# A >1500-byte HTML file with distinct, unique anchors to edit.
_FILLER = "\n".join(
    f"<p>row {i}: lorem ipsum dolor sit amet consectetur adipiscing</p>" for i in range(40)
)
BIG = f"<html><body>\n<h1>Acme Cloud</h1>\n{_FILLER}\n<footer>OLD FOOTER</footer>\n</body></html>\n"
BIG_BYTES = BIG.encode("utf-8")
assert len(BIG_BYTES) > 1500  # guard is active only above this threshold


@pytest.mark.asyncio
async def test_back_to_back_anchored_edits_using_returned_region_without_reread():
    """read → edit A ok → edit B in A's returned region ok with NO intervening read."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)
    assert (await FileReadTool().run(FileReadArgs(path="index.html"), ctx)).success
    a = await FileEditTool().run(
        FileEditArgs(path="index.html", old="<h1>Acme Cloud</h1>", new="<h1>Acme Cloud Pro</h1>"),
        ctx,
    )
    assert a.success, a.content
    assert "applied — lines 1-12 now read:" in a.content
    b = await FileEditTool().run(
        FileEditArgs(
            path="index.html",
            old="<p>row 0: lorem ipsum dolor sit amet consectetur adipiscing</p>",
            new="<p>row 0: updated</p>",
        ),
        ctx,
    )
    assert b.success, b.content
    assert b"Acme Cloud Pro" in sbx._fs["index.html"]
    assert b"row 0: updated" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_stale_old_text_still_fails_after_edit():
    """Re-grounding must NOT make a wrong/stale `old` silently apply: text that no longer exists
    (because a prior edit changed it) fails cleanly as old_text_not_found — never a
    blind mutation."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="index.html"), ctx)
    await FileEditTool().run(
        FileEditArgs(path="index.html", old="<h1>Acme Cloud</h1>", new="<h1>Renamed</h1>"), ctx
    )
    bad = await FileEditTool().run(
        FileEditArgs(path="index.html", old="<h1>Acme Cloud</h1>", new="<h1>Zzz</h1>"), ctx
    )
    assert bad.success is False
    assert bad.error == "old_text_not_found"


@pytest.mark.asyncio
async def test_full_file_write_success_observation_grounds_followup_write():
    """Spec F1: file_write success returns a head view and grounds the path."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="index.html"), ctx)
    first = await FileWriteTool().run(
        FileWriteArgs(path="index.html", content=BIG.replace("OLD FOOTER", "W1 FOOTER")), ctx
    )
    assert first.success is True
    second = await FileWriteTool().run(
        FileWriteArgs(path="index.html", content=BIG.replace("OLD FOOTER", "W2 FOOTER")), ctx
    )
    assert second.success is True, second.content
    assert "applied — lines 1-40 now read:" in second.content
    assert "[total lines: 44]" in second.content
    assert b"W2 FOOTER" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_model_authored_large_file_stays_fully_grounded_across_distant_edits():
    """The model knows every byte it wrote; its own edit must not erase that fact."""
    sbx = _FakeSandbox()
    ctx = _ctx(sbx)
    created = await FileWriteTool().run(FileWriteArgs(path="index.html", content=BIG), ctx)
    assert created.success is True

    header = await FileEditTool().run(
        FileEditArgs(path="index.html", old="<h1>Acme Cloud</h1>", new="<h1>Acme Pro</h1>"), ctx
    )
    assert header.success is True, header.content
    footer = await FileEditTool().run(
        FileEditArgs(path="index.html", old="OLD FOOTER", new="READY FOOTER"), ctx
    )

    assert footer.success is True, footer.content
    assert b"Acme Pro" in sbx._fs["index.html"]
    assert b"READY FOOTER" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_chunked_model_authored_file_stays_grounded_across_distant_edits():
    """Bounded create+append calls still represent complete model-authored bytes."""
    sbx = _FakeSandbox()
    ctx = _ctx(sbx)
    split = len(BIG) // 2
    first = await FileAppendTool().run(FileAppendArgs(path="index.html", content=BIG[:split]), ctx)
    second = await FileAppendTool().run(FileAppendArgs(path="index.html", content=BIG[split:]), ctx)
    assert first.success and second.success

    header = await FileEditTool().run(
        FileEditArgs(path="index.html", old="Acme Cloud", new="Acme Pro"), ctx
    )
    footer = await FileEditTool().run(
        FileEditArgs(path="index.html", old="OLD FOOTER", new="READY FOOTER"), ctx
    )

    assert header.success, header.content
    assert footer.success, footer.content
    assert b"Acme Pro" in sbx._fs["index.html"]
    assert b"READY FOOTER" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_external_mutation_still_stale_after_edit():
    """A genuine EXTERNAL change (bytes mutated behind the tools) after an anchored edit must still
    trip STALE_FILE_CONTEXT — re-grounding tracks the engine's own writes, not third-party ones."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="index.html"), ctx)
    await FileEditTool().run(
        FileEditArgs(path="index.html", old="OLD FOOTER", new="EDITED FOOTER"), ctx
    )
    # Someone/something rewrites the file outside the file tools → grounding is now stale.
    sbx._fs["index.html"] = BIG.replace("OLD FOOTER", "EXTERNALLY CHANGED").encode("utf-8")
    out = await FileEditTool().run(
        FileEditArgs(path="index.html", old="Acme Cloud", new="Acme Cloud X"), ctx
    )
    assert out.success is False
    assert out.error == "STALE_FILE_CONTEXT"


@pytest.mark.asyncio
async def test_failed_edit_does_not_falsely_ground():
    """A FAILED edit (no read first) must not ground the file: a subsequent full file_write is still
    refused. Re-grounding happens only on the SUCCESS path, after the bytes are committed."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)
    # No read first → the edit is refused by the fresh-read guard (no mutation, no grounding).
    blocked = await FileEditTool().run(
        FileEditArgs(path="index.html", old="OLD FOOTER", new="X FOOTER"), ctx
    )
    assert blocked.success is False
    # Grounding was NOT granted by the failed edit → a full file_write still requires a fresh read.
    w = await FileWriteTool().run(
        FileWriteArgs(path="index.html", content=BIG.replace("OLD FOOTER", "Z")), ctx
    )
    assert w.success is False
    assert w.error == "read_before_write"
    assert sbx._fs["index.html"] == BIG_BYTES  # untouched


@pytest.mark.asyncio
async def test_line_based_edit_can_use_returned_numbered_region():
    """A line edit anchored in the prior success observation can proceed without re-reading."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="index.html"), ctx)
    # Anchored edit that ADDS a line → every line below shifts by one.
    a = await FileEditTool().run(
        FileEditArgs(
            path="index.html", old="<h1>Acme Cloud</h1>", new="<h1>Acme</h1>\n<h2>Cloud</h2>"
        ),
        ctx,
    )
    assert a.success, a.content
    assert "applied — lines 1-13 now read:" in a.content
    # The returned view includes line 2, so a follow-up line edit there is grounded.
    rl = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(path="index.html", start_line=2, end_line=2, new_text="X"), ctx
    )
    assert rl.success is True, rl.content
    assert b"\nX\n" in sbx._fs["index.html"]
