"""F1 — read-before-rewrite guard on file_write (all tiers).

FileWriteTool refuses a write to an EXISTING file if there has been no
successful FileReadTool.run call and no success-observation-bearing mutator
for that path in this conversation. A NEW (nonexistent) file is always
allowed. A successful mutation returns current numbered content and sets the
read-since-write bit; a rejected blind write does not.

The guard applies to ALL tiers (ctx.assist=True AND ctx.assist=False).

Tracker keys are CANONICALIZED via strip_redundant_workspace_prefix so
'/workspace/foo' and 'workspace/foo' collapse to the same entry as 'foo'.

These tests cover the unit-tool layer (FileWriteTool / FileReadTool and
the other mutators directly, with a fake sandbox) plus a thin
executor-level smoke for the DefaultToolExecutor path.
"""

from __future__ import annotations

import pytest
from disco.core.llm import ModelExecutionPolicy
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
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
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import ToolRegistry, ToolScope

# --- fakes ------------------------------------------------------------------


class _FakeSandbox:
    """In-memory sandbox for the read-before-write tests. Tracks writes so the
    test can assert that a refused file_write did NOT touch the workspace.
    Mirrors the real sandbox's path normalization: strip_redundant_workspace_prefix
    is applied to all paths so 'workspace/foo.py' and '/workspace/foo.py' and
    'foo.py' all resolve to the same in-memory entry (matching real backends)."""

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


def _ctx(
    sbx: _FakeSandbox,
    *,
    assist: bool = False,
    conv_id: str = "conv-f1",
) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv_id,
        assist=assist,
    )


@pytest.fixture(autouse=True)
def _clear_tracker():
    """The read-state dict is module-level in files.py (one entry per
    conversation); reset between tests so they're order-independent."""
    reset_read_tracker()
    yield
    reset_read_tracker()


# --- new file: always allowed ------------------------------------------------


@pytest.mark.asyncio
async def test_new_file_always_allowed():
    """A brand-new (nonexistent) file never trips the guard — there is no prior
    content to ground on. No file_read required."""
    sbx = _FakeSandbox(existing={})
    out = await FileWriteTool().run(
        FileWriteArgs(path="brand_new.py", content="print('hi')\n"),
        _ctx(sbx),
    )
    assert out.success is True
    assert sbx._fs["brand_new.py"] == b"print('hi')\n"


@pytest.mark.asyncio
async def test_new_file_always_allowed_assist_on():
    """New-file exemption holds for assist=True too."""
    sbx = _FakeSandbox(existing={})
    out = await FileWriteTool().run(
        FileWriteArgs(path="new.py", content="x = 1\n"),
        _ctx(sbx, assist=True),
    )
    assert out.success is True
    assert sbx._fs["new.py"] == b"x = 1\n"


# --- successful mutations ground following writes ----------------------------


@pytest.mark.asyncio
async def test_successful_create_grounds_second_write():
    """Create a file (allowed) → success observation grounds the next same-path write."""
    sbx = _FakeSandbox(existing={})
    ctx = _ctx(sbx)
    # First write: new file → allowed
    first = await FileWriteTool().run(FileWriteArgs(path="main.js", content="const x = 1;\n"), ctx)
    assert first.success is True
    second = await FileWriteTool().run(FileWriteArgs(path="main.js", content="const x = 2;\n"), ctx)
    assert second.success is True, second.content
    assert "applied — lines 1-1 now read:" in second.content
    assert sbx._fs["main.js"] == b"const x = 2;\n"


@pytest.mark.asyncio
async def test_successful_create_grounds_second_write_assist_off():
    """Success-observation grounding is not assist-gated."""
    sbx = _FakeSandbox(existing={})
    ctx = _ctx(sbx, assist=False)
    await FileWriteTool().run(FileWriteArgs(path="x.py", content="v1\n"), ctx)
    second = await FileWriteTool().run(FileWriteArgs(path="x.py", content="v2\n"), ctx)
    assert second.success is True, second.content
    assert sbx._fs["x.py"] == b"v2\n"


@pytest.mark.asyncio
async def test_preexisting_file_refused_without_read_assist_off():
    """A file that pre-existed before any write in this conversation is also
    guarded — the guard fires whenever the file exists and hasn't been read."""
    sbx = _FakeSandbox({"config.py": b"OLD = 1\n"})
    out = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"),
        _ctx(sbx, assist=False),
    )
    assert out.success is False
    assert out.error == "read_before_write"
    assert "config.py" in out.content
    assert sbx._fs["config.py"] == b"OLD = 1\n"


@pytest.mark.asyncio
async def test_preexisting_file_refused_without_read_assist_on():
    """Same guard fires for assist=True on a pre-existing file."""
    sbx = _FakeSandbox({"config.py": b"OLD = 1\n"})
    out = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"),
        _ctx(sbx, assist=True),
    )
    assert out.success is False
    assert out.error == "read_before_write"
    assert sbx._fs["config.py"] == b"OLD = 1\n"
    # workspace untouched
    assert sbx.writes == []


# --- read lifts the guard ----------------------------------------------------


@pytest.mark.asyncio
async def test_read_lifts_the_guard():
    """Create file → file_read → file_write SUCCEEDS (the happy grounded path).
    After the write, its success observation grounds a 3rd write too."""
    sbx = _FakeSandbox(existing={})
    ctx = _ctx(sbx)
    # Create the file
    await FileWriteTool().run(FileWriteArgs(path="x.js", content="v1\n"), ctx)
    # Read it
    read_out = await FileReadTool().run(FileReadArgs(path="x.js"), ctx)
    assert read_out.success is True
    # Now write — allowed
    second = await FileWriteTool().run(FileWriteArgs(path="x.js", content="v2\n"), ctx)
    assert second.success is True
    assert sbx._fs["x.js"] == b"v2\n"
    third = await FileWriteTool().run(FileWriteArgs(path="x.js", content="v3\n"), ctx)
    assert third.success is True, third.content
    assert sbx._fs["x.js"] == b"v3\n"


@pytest.mark.asyncio
async def test_read_of_preexisting_file_lifts_guard():
    """Reading a pre-existing file (not yet touched this conversation) lifts
    the guard and the subsequent write succeeds."""
    sbx = _FakeSandbox({"app.py": b"old body\n"})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="app.py"), ctx)
    out = await FileWriteTool().run(FileWriteArgs(path="app.py", content="new body\n"), ctx)
    assert out.success is True
    assert sbx._fs["app.py"] == b"new body\n"


# --- all listed mutators ground the read bit --------------------------------


@pytest.mark.asyncio
async def test_file_append_grounds_read_bit():
    """file_append success observation permits a following write without a read."""
    sbx = _FakeSandbox(existing={})
    ctx = _ctx(sbx)
    # Create file, read it (bit set), then append
    await FileWriteTool().run(FileWriteArgs(path="log.txt", content="line1\n"), ctx)
    await FileReadTool().run(FileReadArgs(path="log.txt"), ctx)
    append_out = await FileAppendTool().run(FileAppendArgs(path="log.txt", content="line2\n"), ctx)
    assert append_out.success is True
    write_out = await FileWriteTool().run(FileWriteArgs(path="log.txt", content="overwrite\n"), ctx)
    assert write_out.success is True, write_out.content
    assert sbx._fs["log.txt"] == b"overwrite\n"


@pytest.mark.asyncio
async def test_file_edit_grounds_read_bit():
    """file_edit success observation permits a following write without a read."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    ctx = _ctx(sbx)
    # Read (bit set), then edit
    await FileReadTool().run(FileReadArgs(path="a.py"), ctx)
    edit_out = await FileEditTool().run(FileEditArgs(path="a.py", old="x = 1", new="x = 2"), ctx)
    assert edit_out.success is True
    write_out = await FileWriteTool().run(FileWriteArgs(path="a.py", content="x = 99\n"), ctx)
    assert write_out.success is True, write_out.content
    assert sbx._fs["a.py"] == b"x = 99\n"


@pytest.mark.asyncio
async def test_file_replace_lines_grounds_read_bit():
    """file_replace_lines success observation permits a following write without a read."""
    sbx = _FakeSandbox({"a.py": b"x = 1\ny = 2\n"})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="a.py"), ctx)
    replace_out = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(path="a.py", start_line=2, end_line=2, new_text="y = 99"),
        ctx,
    )
    assert replace_out.success is True
    write_out = await FileWriteTool().run(FileWriteArgs(path="a.py", content="overwrite\n"), ctx)
    assert write_out.success is True, write_out.content
    assert sbx._fs["a.py"] == b"overwrite\n"


@pytest.mark.asyncio
async def test_file_insert_lines_grounds_read_bit():
    """file_insert_lines success observation permits a following write without a read."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="a.py"), ctx)
    insert_out = await FileInsertLinesTool().run(
        FileInsertLinesArgs(path="a.py", after_line=1, text="y = 2"), ctx
    )
    assert insert_out.success is True
    write_out = await FileWriteTool().run(FileWriteArgs(path="a.py", content="overwrite\n"), ctx)
    assert write_out.success is True, write_out.content
    assert sbx._fs["a.py"] == b"overwrite\n"


@pytest.mark.asyncio
async def test_file_str_replace_grounds_read_bit():
    """file_str_replace success observation permits a following write without a read."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="a.py"), ctx)
    replace_out = await FileStrReplaceTool().run(
        FileStrReplaceArgs(path="a.py", old_str="x = 1", new_str="x = 42"), ctx
    )
    assert replace_out.success is True
    write_out = await FileWriteTool().run(FileWriteArgs(path="a.py", content="overwrite\n"), ctx)
    assert write_out.success is True, write_out.content
    assert sbx._fs["a.py"] == b"overwrite\n"


# --- path alias collapse -----------------------------------------------------


@pytest.mark.asyncio
async def test_path_alias_collapse_workspace_prefix():
    """'/workspace/x.py' and 'workspace/x.py' collapse to the same tracker key
    as 'x.py'. A read under one alias lifts the guard for writes under the other."""
    sbx = _FakeSandbox({"x.py": b"original\n"})
    ctx = _ctx(sbx)
    # Read via prefixed path
    read_out = await FileReadTool().run(FileReadArgs(path="workspace/x.py"), ctx)
    assert read_out.success is True
    # Write via the bare path — should be allowed because aliases collapse
    write_out = await FileWriteTool().run(FileWriteArgs(path="x.py", content="replacement\n"), ctx)
    assert write_out.success is True
    # Write again: the prior write's success observation grounds the canonical path.
    sbx._fs["x.py"] = b"new\n"  # sandbox updated
    write_out2 = await FileWriteTool().run(
        FileWriteArgs(path="/workspace/x.py", content="newer\n"), ctx
    )
    assert write_out2.success is True, write_out2.content
    assert sbx._fs["x.py"] == b"newer\n"


@pytest.mark.asyncio
async def test_path_alias_collapse_dot_slash():
    """'./x.py' and 'x.py' collapse to the same tracker key for success grounding."""
    sbx = _FakeSandbox({"x.py": b"original\n"})
    ctx = _ctx(sbx)
    # Read 'x.py' lifts the guard
    assert (await FileReadTool().run(FileReadArgs(path="x.py"), ctx)).success is True
    app = await FileAppendTool().run(FileAppendArgs(path="./x.py", content="more\n"), ctx)
    assert app.success is True
    sbx._fs["x.py"] = b"original\nmore\n"
    # Comparable-size content: this test is about path-alias grounding, not the
    # >50%-shrink guard (which would rightly refuse a tiny replacement).
    w = await FileWriteTool().run(FileWriteArgs(path="x.py", content="z = 3  # kept\n"), ctx)
    assert w.success is True, w.content
    assert sbx._fs["x.py"] == b"z = 3  # kept\n"


@pytest.mark.asyncio
async def test_path_alias_abs_prefix_read_lifts_bare_write():
    """A file_read via '/workspace/foo.py' lifts the guard for a file_write
    on 'foo.py' (and vice versa) — they map to the same canonical key."""
    sbx = _FakeSandbox({"foo.py": b"old\n"})
    ctx = _ctx(sbx)
    # Read the absolute form
    await FileReadTool().run(FileReadArgs(path="/workspace/foo.py"), ctx)
    # Write the bare form — allowed
    out = await FileWriteTool().run(FileWriteArgs(path="foo.py", content="new\n"), ctx)
    assert out.success is True
    assert sbx._fs["foo.py"] == b"new\n"


# --- no second-attempt bypass ------------------------------------------------


@pytest.mark.asyncio
async def test_no_second_attempt_bypass():
    """Rejected blind writes do not ground the path. Two consecutive file_write calls
    on an existing unread file are BOTH refused."""
    sbx = _FakeSandbox({"cfg.py": b"OLD\n"})
    ctx = _ctx(sbx)
    first = await FileWriteTool().run(FileWriteArgs(path="cfg.py", content="ATTEMPT_1\n"), ctx)
    assert first.success is False
    assert first.error == "read_before_write"
    # Second attempt — still refused (no bypass)
    second = await FileWriteTool().run(FileWriteArgs(path="cfg.py", content="ATTEMPT_2\n"), ctx)
    assert second.success is False
    assert second.error == "read_before_write"
    # workspace unchanged
    assert sbx._fs["cfg.py"] == b"OLD\n"
    assert sbx.writes == []


# --- per-conversation isolation ----------------------------------------------


@pytest.mark.asyncio
async def test_per_conversation_isolation():
    """Tracking is per-conversation: a read in conv A does NOT lift the guard
    for the same file in conv B."""
    sbx_a = _FakeSandbox({"x.py": b"old\n"})
    sbx_b = _FakeSandbox({"x.py": b"old\n"})
    ctx_a = _ctx(sbx_a, conv_id="conv-A")
    ctx_b = _ctx(sbx_b, conv_id="conv-B")
    # conv A: read lifts guard
    await FileReadTool().run(FileReadArgs(path="x.py"), ctx_a)
    out_a = await FileWriteTool().run(FileWriteArgs(path="x.py", content="A\n"), ctx_a)
    assert out_a.success is True
    # conv B: guard still active (conv A's read doesn't help)
    out_b = await FileWriteTool().run(FileWriteArgs(path="x.py", content="B\n"), ctx_b)
    assert out_b.success is False
    assert out_b.error == "read_before_write"
    assert sbx_b._fs["x.py"] == b"old\n"


# --- executor-level smoke: the guard is reachable end-to-end ----------------


@pytest.mark.asyncio
async def test_executor_refuses_blind_write_to_existing_file():
    """DefaultToolExecutor with any model_policy → file_write to an existing
    unread file hits the F1 gate. Guard is no longer assist-gated."""
    sbx = _FakeSandbox({"x.py": b"old\n"})
    reg = ToolRegistry(allow_unclassified_for_testing=True)
    reg.register(FileWriteTool())
    reg.register(FileReadTool())
    ex = DefaultToolExecutor(
        reg,
        ToolScope(allowed_tools=frozenset({"file_write", "file_read"})),
        sandbox=sbx,
        model_policy=ModelExecutionPolicy.standard(),  # assist=False
    )
    from disco.core import ToolCall

    result = await ex.execute(
        ToolCall(tool_name="file_write", arguments={"path": "x.py", "content": "new\n"})
    )
    assert result.success is False
    assert "x.py" in result.content
    assert "file_read" in result.content
    assert sbx._fs["x.py"] == b"old\n"  # untouched


@pytest.mark.asyncio
async def test_executor_assist_on_refuses_blind_write():
    """Same guard fires with weak model_policy (assist=True)."""
    sbx = _FakeSandbox({"x.py": b"old\n"})
    reg = ToolRegistry(allow_unclassified_for_testing=True)
    reg.register(FileWriteTool())
    reg.register(FileReadTool())
    ex = DefaultToolExecutor(
        reg,
        ToolScope(allowed_tools=frozenset({"file_write", "file_read"})),
        sandbox=sbx,
        model_policy=ModelExecutionPolicy(tier="weak", anchored_edit=True),
    )
    from disco.core import ToolCall

    result = await ex.execute(
        ToolCall(tool_name="file_write", arguments={"path": "x.py", "content": "new\n"})
    )
    assert result.success is False
    assert result.content is not None
    assert "file_read" in result.content
    assert sbx._fs["x.py"] == b"old\n"
