"""F3 — gated read-before-write guard on file_write.

When `ctx.assist` is True (weak-model assist tier), `file_write` refuses the
FIRST write to an existing file the agent has not read in this conversation,
returning a prescriptive hint. The 2nd write attempt is allowed (the model's
explicit "yes, full-replace"). A NEW (nonexistent) file is always allowed; a
file the agent has already read or written is always allowed.

When `ctx.assist` is False (capable-model path), the guard is a true no-op —
behavior is byte-identical to the pre-F3 file_write (the gate is never
consulted, so no read, no state lookup, no extra branch beyond the `if`).

These tests cover BOTH the unit-tool layer (FileWriteTool / FileReadTool
directly, with a fake sandbox) and a thin executor-level smoke that the
DefaultToolExecutor passes `ctx.assist` through (via model_policy.assist) and
that the assist-OFF branch is exercised end-to-end.
"""

from __future__ import annotations

import pytest
from disco.core.llm import ModelExecutionPolicy
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    FileReadArgs,
    FileReadTool,
    FileWriteArgs,
    FileWriteTool,
    reset_read_tracker,
)
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import ToolRegistry, ToolScope

# --- fakes ------------------------------------------------------------------


class _FakeSandbox:
    """In-memory sandbox for the read-before-write tests. Tracks writes so the
    test can assert that a refused file_write did NOT touch the workspace."""

    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        self._fs: dict[str, bytes] = dict(existing or {})
        self.writes: list[tuple[str, bytes]] = []

    async def read_file(self, path: str) -> bytes:
        if path not in self._fs:
            raise FileNotFoundError(f"no such file: {path}")
        return self._fs[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._fs[path] = data
        self.writes.append((path, data))


def _ctx_for(sbx: _FakeSandbox, *, assist: bool, conv_id: str = "conv-f3") -> ToolContext:
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


# --- assist ON: the gate fires ------------------------------------------------


@pytest.mark.asyncio
async def test_assist_on_first_write_to_unread_existing_file_refused():
    """The headline F3 case: a weak model attempts to overwrite a file it
    never read. The first attempt is refused with a prescriptive hint, and the
    workspace is untouched."""
    sbx = _FakeSandbox({"config.py": b"OLD = 1\n"})
    out = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"),
        _ctx_for(sbx, assist=True),
    )
    assert out.success is False
    assert out.error == "read_before_write"
    assert "config.py" in out.content
    assert "read" in out.content.lower()  # the hint mentions file_read
    assert "second attempt" in out.content.lower()  # the 2nd-attempt escape hatch
    assert sbx.writes == []  # workspace untouched on refusal
    # disk still has the original bytes
    assert sbx._fs["config.py"] == b"OLD = 1\n"


@pytest.mark.asyncio
async def test_assist_on_second_write_to_unread_file_allowed():
    """The model takes the hint: rather than reading, it re-issues the write
    (explicit "yes, full-replace"). The 2nd attempt goes through and the
    workspace reflects the new bytes."""
    sbx = _FakeSandbox({"config.py": b"OLD = 1\n"})
    ctx = _ctx_for(sbx, assist=True)
    first = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"), ctx
    )
    assert first.success is False  # refused
    second = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"), ctx
    )
    assert second.success is True
    assert sbx._fs["config.py"] == b"NEW = 99\n"
    assert ("config.py", b"NEW = 99\n") in sbx.writes


@pytest.mark.asyncio
async def test_assist_on_read_lifts_the_guard():
    """A file_read between attempts lifts the warning: the write goes through
    on the very next try (no 2nd-attempt gamble needed). This is the happy
    path the guard is designed to encourage."""
    sbx = _FakeSandbox({"config.py": b"OLD = 1\n"})
    ctx = _ctx_for(sbx, assist=True)
    first = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"), ctx
    )
    assert first.success is False  # refused
    # model reads the file to see what's there
    read_out = await FileReadTool().run(
        FileReadArgs(path="config.py"), ctx
    )
    assert read_out.success is True
    # now the write is allowed on the FIRST attempt
    second = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"), ctx
    )
    assert second.success is True
    assert sbx._fs["config.py"] == b"NEW = 99\n"


@pytest.mark.asyncio
async def test_assist_on_new_file_always_allowed():
    """A NEW (nonexistent) file never trips the guard — the guard is only
    about clobbering EXISTING unread files (SmallCode's exact semantic)."""
    sbx = _FakeSandbox(existing={})  # empty workspace
    out = await FileWriteTool().run(
        FileWriteArgs(path="fresh.py", content="print('hi')\n"),
        _ctx_for(sbx, assist=True),
    )
    assert out.success is True
    assert sbx._fs["fresh.py"] == b"print('hi')\n"


@pytest.mark.asyncio
async def test_assist_on_previously_read_file_always_allowed():
    """A file the agent already read in this conversation can be overwritten
    on the first try (no need for the 2nd-attempt escape hatch)."""
    sbx = _FakeSandbox({"x.py": b"old body\n"})
    ctx = _ctx_for(sbx, assist=True)
    # read first
    await FileReadTool().run(FileReadArgs(path="x.py"), ctx)
    # then write — allowed
    out = await FileWriteTool().run(
        FileWriteArgs(path="x.py", content="new body\n"), ctx
    )
    assert out.success is True
    assert sbx._fs["x.py"] == b"new body\n"


@pytest.mark.asyncio
async def test_assist_on_per_conversation_isolation():
    """Tracking is per-conversation: writes in conv A do NOT lift a warning
    for the same file in conv B. (Two different agents, two different sessions
    — the guard is not a global lock.)"""
    sbx_a = _FakeSandbox({"x.py": b"old\n"})
    sbx_b = _FakeSandbox({"x.py": b"old\n"})
    ctx_a = _ctx_for(sbx_a, assist=True, conv_id="conv-A")
    ctx_b = _ctx_for(sbx_b, assist=True, conv_id="conv-B")
    # conv A: 1st write refused, 2nd allowed
    out_a1 = await FileWriteTool().run(
        FileWriteArgs(path="x.py", content="A\n"), ctx_a
    )
    assert out_a1.success is False
    # conv B has its own state — 1st write is also refused (NOT lifted by A)
    out_b1 = await FileWriteTool().run(
        FileWriteArgs(path="x.py", content="B\n"), ctx_b
    )
    assert out_b1.success is False  # NOT lifted by A's writes
    # conv A: 2nd attempt now allowed
    out_a2 = await FileWriteTool().run(
        FileWriteArgs(path="x.py", content="A\n"), ctx_a
    )
    assert out_a2.success is True
    assert sbx_a._fs["x.py"] == b"A\n"
    assert sbx_b._fs["x.py"] == b"old\n"  # conv B's file still untouched


# --- assist OFF: byte-identical to pre-F3 ------------------------------------


@pytest.mark.asyncio
async def test_assist_off_first_write_to_unread_existing_file_allowed():
    """Gate OFF — a 1st write to an existing, unread file is allowed.
    Crucially the gate is NEVER consulted, so the result is byte-identical
    to the pre-F3 file_write (success, no hint, no error, content written)."""
    sbx = _FakeSandbox({"config.py": b"OLD = 1\n"})
    out = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"),
        _ctx_for(sbx, assist=False),
    )
    assert out.success is True
    assert out.error is None
    assert "wrote" in out.content
    assert sbx._fs["config.py"] == b"NEW = 99\n"
    assert ("config.py", b"NEW = 99\n") in sbx.writes


@pytest.mark.asyncio
async def test_assist_off_never_consults_tracker_state():
    """Even after the tracker has 'warned' a path (a prior assist-ON refusal),
    assist OFF must allow the write without lifting or clearing the warning.
    This proves the assist-OFF branch does not interact with the tracker at
    all (the check is guarded, not just optional)."""
    # pre-populate tracker state as if a prior assist-ON session had warned
    from disco.tools.builtin import files as _files

    _files._read_state.setdefault("conv-f3", {"read": set(), "written": set(), "warned": set()})
    _files._read_state["conv-f3"]["warned"].add("config.py")
    sbx = _FakeSandbox({"config.py": b"OLD = 1\n"})
    out = await FileWriteTool().run(
        FileWriteArgs(path="config.py", content="NEW = 99\n"),
        _ctx_for(sbx, assist=False, conv_id="conv-f3"),
    )
    assert out.success is True  # assist OFF never consults tracker
    assert sbx._fs["config.py"] == b"NEW = 99\n"


# --- executor-level smoke: ctx.assist flows through --------------------------


@pytest.mark.asyncio
async def test_executor_assist_on_refuses_via_default_tool_executor():
    """End-to-end: DefaultToolExecutor with weak model_policy → file_write hits the
    F3 gate and returns a failure ToolResult. Verifies the gate is reachable
    from the production call path, not just the tool's .run() surface."""
    sbx = _FakeSandbox({"x.py": b"old\n"})
    reg = ToolRegistry()
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
    assert "x.py" in result.content
    assert "read" in result.content.lower()
    assert sbx._fs["x.py"] == b"old\n"  # untouched


@pytest.mark.asyncio
async def test_executor_assist_off_writes_through_byte_identically():
    """End-to-end: DefaultToolExecutor with standard model_policy → file_write goes
    through unchanged. No hint, no error, content lands on disk — proves the
    assist-OFF path is the pre-F3 code path byte-for-byte."""
    sbx = _FakeSandbox({"x.py": b"old\n"})
    reg = ToolRegistry()
    reg.register(FileWriteTool())
    reg.register(FileReadTool())
    ex = DefaultToolExecutor(
        reg,
        ToolScope(allowed_tools=frozenset({"file_write", "file_read"})),
        sandbox=sbx,
        model_policy=ModelExecutionPolicy.standard(),
    )
    from disco.core import ToolCall

    result = await ex.execute(
        ToolCall(tool_name="file_write", arguments={"path": "x.py", "content": "new\n"})
    )
    assert result.success is True
    assert result.error is None
    assert sbx._fs["x.py"] == b"new\n"
