"""T8 (E4) - workspace snapshot must skip binary files + note deleted/unreadable.

The snapshot in `AgentLoop._workspace_snapshot_message` re-derives the live
on-disk content of the working set from the sandbox each turn. The E4 fix
adds four guards (plan T8 / weak-model-reliability.md WAVE 3):

1. A file whose first 1KB contains a NUL byte is treated as BINARY and omitted
   from the snapshot, with a "[binary omitted: <path>]" note.
2. A large mostly-text file with a stray NUL past the first 1KB is NOT
   omitted (the NUL check is bounded to the head sample).
3. `.disco-spill-*` files (T10's overflow logs) are skipped outright from the
   working set.
4. Read errors are surfaced as notes, not silent skips:
     FileNotFoundError  -> "[file gone: <path>]"
     PermissionError    -> "[unreadable: permission: <path>]"

These tests construct the AgentLoop with a fake executor that exposes a
duck-typed `sandbox` attribute (a fake SandboxInstance) and call
`_workspace_snapshot_message` directly. They assert on the rendered
LLMMessage.content string.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from disco.core import (
    ActionEvent,
    LLMMessage,
    ToolCall,
)
from disco.core.loop import AgentLoop
from loop_fakes import (
    FakeAnalyzer,
    FakeSummarizer,
    NeverConfirm,
    NoOpCondenser,
    ScriptedAgent,
    build_loop,
    finish_step,
)

# ---- a fake SandboxInstance that returns / raises per-path ------------------


class FakeSandboxInstance:
    """Implements the duck-typed slice of SandboxInstance the snapshot uses:
    `read_file(path) -> bytes` (async), and nothing else matters here."""

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
    """Minimal executor stub: exposes a `sandbox` attribute and the loop's
    ToolExecutor surface (we never actually `execute()` from these tests)."""

    def __init__(self, sandbox: FakeSandboxInstance | None):
        self.sandbox = sandbox

    def available_tools(self):
        return []

    async def execute(self, call):
        raise NotImplementedError  # snapshot tests don't drive execution


# ---- helpers ----------------------------------------------------------------


def _events_with_file_writes(paths: Iterable[str]) -> list[ActionEvent]:
    """Each path is a previous file_write - those are the "mutated" working set
    the snapshot reads. We seed them as ActionEvents (no observation needed;
    the snapshot only scans `tool_call.tool_name` + `arguments.path`)."""
    return [
        ActionEvent(
            thought=f"wrote {p}",
            tool_call=ToolCall(tool_name="file_write", arguments={"path": p, "content": ""}),
        )
        for p in paths
    ]


def _loop_with_sandbox(sandbox: FakeSandboxInstance | None) -> AgentLoop:
    """A loop that never starts an LLM call - we only call
    `_workspace_snapshot_message` on it directly."""
    from disco.core import SqliteEventStore

    store = SqliteEventStore(":memory:")
    # ScriptedAgent w/ a single finish step is enough; we never invoke .step().
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


# ---- 1. NUL in first 1KB -> omitted + note ---------------------------------


def test_binary_file_with_nul_in_first_1kb_is_omitted_with_note():
    """A file whose first 1024 bytes contain a NUL is treated as binary and
    is NOT included in the snapshot blocks. The snapshot appends a
    "[binary omitted: <path>]" note so the model knows the file exists but
    was deliberately excluded."""
    binary = b"\x00" + b"PK\x03\x04" + b"\x00" * 200  # fake ZIP header - NUL in byte 0
    sandbox = FakeSandboxInstance(files={"build/app.zip": binary})
    content = _snapshot_text(sandbox, ["build/app.zip"])
    assert content is not None
    # The file is NOT in the BEGIN/END block markers
    assert "BEGIN FILE build/app.zip" not in content
    assert "END FILE build/app.zip" not in content
    # The omitted notice is present
    assert "[binary omitted: build/app.zip]" in content


# ---- 2. Stray NUL past 1KB -> NOT omitted (bounded sample check) -----------


def test_stray_nul_past_1kb_in_mostly_text_file_is_NOT_omitted():
    """A ~1MB text file with a single stray NUL at byte ~1MB is still
    treated as text: the NUL detector only samples the first 1024 bytes. The
    file appears in the snapshot as a normal truncated block (head/tail,
    markers, and all)."""
    # A ~1.15MB text file, with one stray NUL injected at byte 1,000,000 (well
    # past the 1KB sample). The text after decoding is NUL-tainted, so the
    # snapshot decodes with `errors="replace"` and shows head + tail as usual.
    body = (b"the quick brown fox jumps over the lazy dog. " * 23000)  # ~1.15MB
    assert len(body) > 1_000_000
    body = body[:1_000_000] + b"\x00" + body[1_000_001:]
    sandbox = FakeSandboxInstance(files={"src/large.txt": body})
    content = _snapshot_text(sandbox, ["src/large.txt"])
    assert content is not None
    # The file IS in the snapshot - head/tail-truncated block, not omitted
    assert "BEGIN FILE src/large.txt" in content
    assert "END FILE src/large.txt" in content
    assert "[binary omitted: src/large.txt]" not in content


# ---- 3. .disco-spill-* files are skipped from the working set --------------


def test_disco_spill_files_are_skipped_from_working_set():
    """`.disco-spill-<uuid>.log` is T10's overflow-log path. Even if the agent
    touched one (via file_write - exotic but possible), it must NOT appear in
    the snapshot: those are noise, not deliverables."""
    sandbox = FakeSandboxInstance(
        files={
            "src/app.py": b"print('hello')\n",
            ".disco-spill-abc123.log": b"huge stdout...",
        }
    )
    content = _snapshot_text(sandbox, ["src/app.py", ".disco-spill-abc123.log"])
    assert content is not None
    # Normal file IS in the snapshot
    assert "BEGIN FILE src/app.py" in content
    assert "END FILE src/app.py" in content
    # The spill file is NOT in the snapshot blocks
    assert "BEGIN FILE .disco-spill-abc123.log" not in content
    assert "END FILE .disco-spill-abc123.log" not in content
    # And no note for the spill file (it's just excluded from the working set)
    assert ".disco-spill-abc123.log" not in content


# ---- 4. Normal .py file is unaffected (text-file regression guard) ---------


def test_normal_python_file_is_included_unaffected():
    """Sanity / regression guard: a normal .py file is included in the snapshot
    with its BEGIN/END markers, content rendered, and NO omission note."""
    py_body = b"def hello():\n    return 'world'\n"
    sandbox = FakeSandboxInstance(files={"hello.py": py_body})
    content = _snapshot_text(sandbox, ["hello.py"])
    assert content is not None
    assert "BEGIN FILE hello.py" in content
    assert "def hello():" in content
    assert "END FILE hello.py" in content
    assert "[binary omitted: hello.py]" not in content
    assert "[file gone: hello.py]" not in content
    assert "[unreadable: permission: hello.py]" not in content


# ---- 5. FileNotFoundError -> "[file gone: <path>]" note --------------------


def test_deleted_file_emits_file_gone_note():
    """When the sandbox's read_file raises FileNotFoundError (the file was
    deleted since the action was recorded), the snapshot appends a
    "[file gone: <path>]" note instead of silently skipping."""
    sandbox = FakeSandboxInstance(
        files={"keep.py": b"keep\n"},
        raises={"gone.py": FileNotFoundError("gone.py")},
    )
    content = _snapshot_text(sandbox, ["keep.py", "gone.py"])
    assert content is not None
    # The kept file is still in the snapshot
    assert "BEGIN FILE keep.py" in content
    # The gone file gets a note, not a BEGIN/END block
    assert "BEGIN FILE gone.py" not in content
    assert "[file gone: gone.py]" in content
    # And not miscategorized as binary or permission
    assert "[binary omitted: gone.py]" not in content
    assert "[unreadable: permission: gone.py]" not in content


# ---- 6. PermissionError -> "[unreadable: permission: <path>]" note ---------


def test_permission_error_emits_unreadable_note():
    """A PermissionError from the sandbox's read_file produces a
    "[unreadable: permission: <path>]" note."""
    sandbox = FakeSandboxInstance(
        files={"visible.py": b"print(1)\n"},
        raises={"secret.py": PermissionError("denied")},
    )
    content = _snapshot_text(sandbox, ["visible.py", "secret.py"])
    assert content is not None
    assert "BEGIN FILE visible.py" in content
    assert "BEGIN FILE secret.py" not in content
    assert "[unreadable: permission: secret.py]" in content
    assert "[file gone: secret.py]" not in content
    assert "[binary omitted: secret.py]" not in content


# ---- 7. Preamble contains silent-context instruction + existing guidance -----


def test_preamble_contains_silent_context_instruction_and_existing_guidance():
    """The snapshot preamble must tell the model NOT to narrate or acknowledge
    the block, and must contain the anti-clobber guidance (the 'authoritative' /
    'trust THIS' / 'file_write' / 'file_read' lines) and the F1 read-before-
    rewrite directive. The old 'AVOID line-number edits' instruction is REMOVED
    because it conflicts with the read-before-rewrite gate (F1 order)."""
    sandbox = FakeSandboxInstance(files={"app.py": b"x = 1\n"})
    content = _snapshot_text(sandbox, ["app.py"])
    assert content is not None

    # Don't-narrate instruction must be present
    assert "SILENT CONTEXT" in content, "preamble must include SILENT CONTEXT marker"
    assert "do NOT" in content or "Do NOT" in content, (
        "preamble must instruct the model not to narrate the snapshot"
    )

    # Anti-clobber lines must remain intact
    assert "authoritative" in content, "preamble must still say 'authoritative'"
    assert "trust THIS" in content, "preamble must still say 'trust THIS over your memory'"
    assert "file_write" in content, "preamble must still reference file_write"

    # F1: read-before-rewrite guidance must be present
    assert "file_read" in content, (
        "preamble must tell the model to call file_read before a full rewrite"
    )

    # Old 'AVOID line-number edits' instruction is gone — F1 prefers targeted edits
    assert "AVOID line-number edits" not in content, (
        "preamble must NOT tell the model to avoid line-number edits (conflicts with F1)"
    )
