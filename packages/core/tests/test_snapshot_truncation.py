"""Tests for the workspace snapshot truncation guidance (Order fix-b-buildengine)."""

import asyncio
from collections.abc import Iterable

from disco.core import ActionEvent, LLMMessage, ToolCall
from disco.core.loop import AgentLoop
from disco.core.loop.engine import _WS_PER_FILE_CHARS
from loop_fakes import (
    FakeAnalyzer,
    FakeSummarizer,
    NeverConfirm,
    NoOpCondenser,
    ScriptedAgent,
    build_loop,
    finish_step,
)

class FakeSandboxInstance:
    def __init__(self, *, files=None, raises=None):
        self._files = dict(files or {})
        self._raises = dict(raises or {})

    async def read_file(self, path: str) -> bytes:
        if path in self._raises:
            raise self._raises[path]
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

class SandboxExecutor:
    def __init__(self, sandbox: FakeSandboxInstance | None):
        self.sandbox = sandbox

    def available_tools(self):
        return []

    async def execute(self, call):
        raise NotImplementedError

def _events_with_file_writes(paths: Iterable[str]) -> list[ActionEvent]:
    return [
        ActionEvent(
            thought=f"wrote {p}",
            tool_call=ToolCall(tool_name="file_write", arguments={"path": p, "content": ""}),
        )
        for p in paths
    ]

def _loop_with_sandbox(sandbox: FakeSandboxInstance | None) -> AgentLoop:
    from disco.core import SqliteEventStore

    store = SqliteEventStore(":memory:")
    agent = ScriptedAgent([finish_step()])
    loop, _ = build_loop(
        agent,
        store=store,
        executor=SandboxExecutor(sandbox),
        analyzer=FakeAnalyzer(),
        policy=NeverConfirm(),
        condenser=NoOpCondenser(),
        summarizer=FakeSummarizer(),
    )
    return loop

def _snapshot_text(sandbox: FakeSandboxInstance | None, paths: Iterable[str]) -> str | None:
    loop = _loop_with_sandbox(sandbox)
    msg = asyncio.run(loop._workspace_snapshot_message(_events_with_file_writes(paths)))
    if msg is None:
        return None
    assert isinstance(msg, LLMMessage)
    return msg.content

def test_large_file_truncation_guidance():
    # File ABOVE the cap
    large_body = b"x" * (_WS_PER_FILE_CHARS + 1000)
    sandbox = FakeSandboxInstance(files={"large.py": large_body})
    content = _snapshot_text(sandbox, ["large.py"])
    assert content is not None
    
    # Assert specific truncation message is present
    assert "this file is too large to show in full" in content
    assert "call file_read on this path to see the full content" in content
    assert "make targeted changes with file_edit (content-anchored old\\u2192new)" in content or "make targeted changes with file_edit (content-anchored old→new)" in content
    assert "do NOT call file_write with regenerated content" in content

def test_small_file_no_truncation_guidance():
    # File BELOW the cap
    small_body = b"y" * (_WS_PER_FILE_CHARS - 1000)
    sandbox = FakeSandboxInstance(files={"small.py": small_body})
    content = _snapshot_text(sandbox, ["small.py"])
    assert content is not None
    
    # Assert truncation message is NOT present
    assert "this file is too large to show in full" not in content
    assert "call file_read on this path" not in content
    assert "make targeted changes with file_edit" not in content
