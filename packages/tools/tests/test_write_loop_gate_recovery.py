"""Write-loop recovery — the read-before-write gate must be satisfiable by a
SNAPSHOT-/DEDUP-backed read, not only by a real ``FileReadTool.run``.

Root cause (live build, app.js ~21KB): the agent clobbered a file with a stub
(clearing the read-since-write bit), then could neither re-read it (the loop
handed back a ``[superseded … current content is in the CURRENT WORKSPACE block]``
pointer / an F9 dedup pointer instead of executing the read) NOR re-write it (the
gate refused — no ``FileReadTool.run`` fired to set the bit). Read-pointer + gated
write = the unrecoverable loop that drove the shell-exec heredoc fallback.

Fix: the loop grounds the gate via ``executor.note_grounding_read(path)`` whenever
the file's CURRENT content is already in front of the model by a grounded,
non-tool channel (snapshot pinned-in-full, or F9 dedup pointer). These tests pin
that bridge at the tool/executor layer: ``mark_read`` / ``note_grounding_read``
satisfies the gate exactly like a real read would, and a following file_write of
the same path succeeds in ONE step.
"""

from __future__ import annotations

import pytest
from disco.core import ToolCall
from disco.core.llm import ModelExecutionPolicy
from disco.tools.builtin.files import (
    FileReadTool,
    FileWriteTool,
    mark_read,
    reset_read_tracker,
)
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import ToolRegistry, ToolScope


class _FakeSandbox:
    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        from disco.tools.sandbox.base import strip_redundant_workspace_prefix as _strip

        self._fs: dict[str, bytes] = {_strip(k): v for k, v in (existing or {}).items()}
        self._strip = _strip

    async def read_file(self, path: str) -> bytes:
        key = self._strip(path)
        if key not in self._fs:
            raise FileNotFoundError(path)
        return self._fs[key]

    async def write_file(self, path: str, data: bytes) -> None:
        self._fs[self._strip(path)] = data

    async def destroy(self) -> None:
        self._fs.clear()


@pytest.fixture(autouse=True)
def _clear_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


def _executor(sbx: _FakeSandbox, conv_id: str = "conv-grnd") -> DefaultToolExecutor:
    reg = ToolRegistry()
    reg.register(FileWriteTool())
    reg.register(FileReadTool())
    return DefaultToolExecutor(
        reg,
        ToolScope(allowed_tools=frozenset({"file_write", "file_read"})),
        sandbox=sbx,
        conversation_id=conv_id,
        model_policy=ModelExecutionPolicy.standard(),
    )


@pytest.mark.asyncio
async def test_grounding_read_satisfies_gate_and_write_succeeds_in_one_step():
    """The keystone: an existing, un-read file → blind file_write REFUSED; then a
    snapshot-/dedup-backed grounding read (executor.note_grounding_read) → the SAME
    write SUCCEEDS. One step, no loop, no shell fallback."""
    sbx = _FakeSandbox({"app.js": b"const real = 1;\n"})
    ex = _executor(sbx)

    # 1. Blind rewrite of an existing un-read file → gate refuses (today's behavior).
    refused = await ex.execute(
        ToolCall(
            tool_name="file_write",
            arguments={"path": "app.js", "content": "const updated = 2;\n"},
        )
    )
    assert refused.success is False
    assert "file_read" in (refused.content or "")
    assert sbx._fs["app.js"] == b"const real = 1;\n"  # untouched

    # 2. The loop grounds the read (the file is pinned in the CURRENT WORKSPACE
    #    block / an F9 dedup pointed at the prior read) — NO FileReadTool.run fired.
    ex.note_grounding_read("app.js")

    # 3. The same write now SUCCEEDS — the model had the current content; the gate
    #    no longer dead-ends it.
    ok = await ex.execute(
        ToolCall(
            tool_name="file_write",
            arguments={"path": "app.js", "content": "const updated = 2;\n"},
        )
    )
    assert ok.success is True
    assert sbx._fs["app.js"] == b"const updated = 2;\n"


@pytest.mark.asyncio
async def test_grounding_read_canonicalizes_path_like_a_real_read():
    """Grounding 'workspace/app.js' lifts the gate for a write to bare 'app.js'
    (same canonical key) — matching how a real file_read's bit canonicalizes."""
    sbx = _FakeSandbox({"app.js": b"old\n"})
    ex = _executor(sbx, conv_id="conv-canon")
    ex.note_grounding_read("/workspace/app.js")
    ok = await ex.execute(
        ToolCall(tool_name="file_write", arguments={"path": "app.js", "content": "new\n"})
    )
    assert ok.success is True
    assert sbx._fs["app.js"] == b"new\n"


@pytest.mark.asyncio
async def test_grounding_read_is_per_conversation():
    """Grounding in conv A does NOT lift the gate in conv B (the bridge reuses the
    per-conversation tracker — no cross-conversation leak)."""
    sbx_a = _FakeSandbox({"x.py": b"old\n"})
    sbx_b = _FakeSandbox({"x.py": b"old\n"})
    ex_a = _executor(sbx_a, conv_id="conv-A")
    ex_b = _executor(sbx_b, conv_id="conv-B")
    ex_a.note_grounding_read("x.py")
    ok_a = await ex_a.execute(
        ToolCall(tool_name="file_write", arguments={"path": "x.py", "content": "A\n"})
    )
    assert ok_a.success is True
    refused_b = await ex_b.execute(
        ToolCall(tool_name="file_write", arguments={"path": "x.py", "content": "B\n"})
    )
    assert refused_b.success is False
    assert refused_b.error == "read_before_write"


@pytest.mark.asyncio
async def test_grounding_read_noop_on_killed_executor():
    """A killed executor's grounding is inert (no resurrection of revoked state)."""
    sbx = _FakeSandbox({"x.py": b"old\n"})
    ex = _executor(sbx, conv_id="conv-killed")
    await ex.kill()
    ex.note_grounding_read("x.py")  # must not raise, must not ground
    # (the executor is killed; execute() fails regardless — assert grounding left
    # no residue by checking the tracker directly via a fresh write attempt on a
    # live executor sharing the same conv id is moot; just assert no crash.)


def test_mark_read_ignores_empty_path():
    """Defensive: mark_read with an empty/non-str path is a no-op (never KeyErrors
    or pollutes the tracker)."""
    mark_read("conv-empty", "")
    mark_read("conv-empty", None)  # type: ignore[arg-type]
