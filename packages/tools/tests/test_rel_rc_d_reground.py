"""[REL-RC-D] Anchored edits advance grounding to the post-edit bytes.

Root cause fixed: on a successful mutation the read-before-write guard discarded the coarse
read_since_write bit but left reads[path].sha at the PRE-edit value, so the model's OWN next
same-file edit saw a stale sha and was refused as STALE_FILE_CONTEXT — misreporting a tracked,
deterministic, host-validated edit as external drift. Back-to-back same-file edits then wedged the
build loop into STUCK (the revise_thrice failure). Anchored edits (file_edit / file_str_replace /
exact_replace) now re-ground to the just-written bytes (preserving prior grounding shape — never
promoting a partial read to whole-file); line-based edits, full/blind writes, and external
mutations still discard/force a fresh read.

The fresh-read guard only engages for files > 1500 bytes (small files stay fully in context), so
these fixtures use a >1500-byte file.
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    FileEditArgs,
    FileEditTool,
    FileReadArgs,
    FileReadTool,
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
_FILLER = "\n".join(f"<p>row {i}: lorem ipsum dolor sit amet consectetur adipiscing</p>" for i in range(40))
BIG = f"<html><body>\n<h1>Acme Cloud</h1>\n{_FILLER}\n<footer>OLD FOOTER</footer>\n</body></html>\n"
BIG_BYTES = BIG.encode("utf-8")
assert len(BIG_BYTES) > 1500  # guard is active only above this threshold


@pytest.mark.asyncio
async def test_back_to_back_anchored_edits_without_reread():
    """THE fix: read → edit A ok → edit B ok on the SAME file with NO intervening read.
    Before REL-RC-D the 2nd edit was refused STALE_FILE_CONTEXT and the loop wedged."""
    sbx = _FakeSandbox({"index.html": BIG_BYTES})
    ctx = _ctx(sbx)
    assert (await FileReadTool().run(FileReadArgs(path="index.html"), ctx)).success
    a = await FileEditTool().run(
        FileEditArgs(path="index.html", old="<h1>Acme Cloud</h1>", new="<h1>Acme Cloud Pro</h1>"), ctx
    )
    assert a.success, a.content
    b = await FileEditTool().run(
        FileEditArgs(path="index.html", old="OLD FOOTER", new="NEW FOOTER"), ctx
    )
    assert b.success, b.content
    assert b"Acme Cloud Pro" in sbx._fs["index.html"]
    assert b"NEW FOOTER" in sbx._fs["index.html"]


@pytest.mark.asyncio
async def test_stale_old_text_still_fails_after_edit():
    """Re-grounding must NOT make a wrong/stale `old` silently apply: text that no longer exists
    (because a prior edit changed it) fails cleanly as old_text_not_found — never a blind mutation."""
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
async def test_full_file_write_still_ungrounds_and_requires_fresh_read():
    """Class C is unchanged: a full/blind file_write still clears grounding, so a 2nd write with no
    intervening read is refused. Re-grounding is ONLY for anchored edits, never full rewrites."""
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
    assert second.success is False
    assert second.error == "read_before_write"


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
