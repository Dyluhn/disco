"""W3/W4 — syntax-gate writes + auto-revert + capability-gated file_str_replace.

W3 — _gated_write: every writer runs new content through _syntax_errors and
diff-filters against pre-existing errors so pre-existing breakage is not
counted against the edit. On new errors: AUTO-REVERT + failure outcome.

W4 — file_str_replace: anchored str-replace offered only to capable models
(Requirement.ANCHORED_EDIT); registry withholds it from the weak tier's
advertised set.
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
from disco.tools.registry import (
    AGENT_TOOLS,
    agent_scope,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """In-memory sandbox that tracks all writes."""

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


def _ctx(sbx: _FakeSandbox, *, assist: bool = False, conv: str = "conv-w3") -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv,
        assist=assist,
    )


@pytest.fixture(autouse=True)
def _clear_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


# ===========================================================================
# W3 — syntax gate on file_write
# ===========================================================================


@pytest.mark.asyncio
async def test_w3_write_bad_python_over_good_file_reverted():
    """W3 headline: writing syntactically broken Python over a good file
    → auto-revert to old content + failure outcome.
    Note: file_read is required first (F1 gate) to prove we've seen the content."""
    good = b"x = 1\n"
    sbx = _FakeSandbox({"foo.py": good})
    ctx = _ctx(sbx)
    # F1: read first so the write is allowed to reach the W3 gate.
    await FileReadTool().run(FileReadArgs(path="foo.py"), ctx)
    out = await FileWriteTool().run(
        FileWriteArgs(path="foo.py", content="def broken(\n"),
        ctx,
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert "SyntaxError" in out.content
    assert "NOT applied" in out.content
    assert "DO NOT re-run" in out.content
    # workspace reverted
    assert sbx._fs["foo.py"] == good


@pytest.mark.asyncio
async def test_w3_write_bad_python_new_file_applied_and_flagged():
    """W3: a NEW file with a syntax error has no prior content to revert to,
    so the write IS applied but the failure outcome still flags the error."""
    sbx = _FakeSandbox({})
    out = await FileWriteTool().run(
        FileWriteArgs(path="new.py", content="def broken(\n"),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert "SyntaxError" in out.content
    # applied (no old to revert to) — flag wording says "it was applied"
    assert "applied" in out.content
    assert "new.py" in sbx._fs  # file was written


@pytest.mark.asyncio
async def test_w3_write_good_python_passes():
    """W3: writing syntactically valid Python passes the gate."""
    sbx = _FakeSandbox({"foo.py": b"x = 1\n"})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="foo.py"), ctx)
    out = await FileWriteTool().run(
        FileWriteArgs(path="foo.py", content="x = 2\ny = 3\n"),
        ctx,
    )
    assert out.success is True
    assert sbx._fs["foo.py"] == b"x = 2\ny = 3\n"


@pytest.mark.asyncio
async def test_w3_write_unparseable_type_written_normally():
    """W3: a file type we don't parse (.md, .txt, .sh) bypasses the gate —
    never block what we can't parse."""
    sbx = _FakeSandbox({"README.md": b"# old\n"})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="README.md"), ctx)
    out = await FileWriteTool().run(
        FileWriteArgs(path="README.md", content="this is {{{{ badly formed"),
        ctx,
    )
    assert out.success is True
    assert sbx._fs["README.md"] == b"this is {{{{ badly formed"


@pytest.mark.asyncio
async def test_w3_write_preexisting_syntax_error_not_counted():
    """W3 diff-filter: a pre-existing SyntaxError is NOT attributed to the edit.
    The gate only counts NEWLY-introduced errors."""
    bad_py = b"def broken(\n"
    sbx = _FakeSandbox({"foo.py": bad_py})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="foo.py"), ctx)
    # Rewrite with DIFFERENT broken code — still has a SyntaxError, but it
    # was already there: the gate should NOT revert.
    out = await FileWriteTool().run(
        FileWriteArgs(path="foo.py", content="class also_broken(\n"),
        ctx,
    )
    assert out.success is True  # pre-existing SyntaxError is not counted
    assert sbx._fs["foo.py"] == b"class also_broken(\n"


@pytest.mark.asyncio
async def test_w3_write_bad_json_over_good_json_reverted():
    """W3: writing invalid JSON over a valid JSON file → reverted."""
    sbx = _FakeSandbox({"cfg.json": b'{"key": 1}\n'})
    ctx = _ctx(sbx)
    await FileReadTool().run(FileReadArgs(path="cfg.json"), ctx)
    out = await FileWriteTool().run(
        FileWriteArgs(path="cfg.json", content="{bad json"),
        ctx,
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert "JSONDecodeError" in out.content
    assert sbx._fs["cfg.json"] == b'{"key": 1}\n'


# ---------------------------------------------------------------------------
# W3 gates on other writers (smoke coverage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_w3_file_edit_bad_python_reverted():
    """W3 applies on file_edit: broken edit is reverted."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    out = await FileEditTool().run(
        FileEditArgs(path="a.py", old="x = 1", new="def bad("),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert sbx._fs["a.py"] == b"x = 1\n"


@pytest.mark.asyncio
async def test_w3_file_append_bad_python_reverted():
    """W3 applies on file_append: appending broken syntax is reverted."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    out = await FileAppendTool().run(
        FileAppendArgs(path="a.py", content="def broken(\n"),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert sbx._fs["a.py"] == b"x = 1\n"


@pytest.mark.asyncio
async def test_w3_file_replace_lines_bad_python_reverted():
    """W3 applies on file_replace_lines: broken replacement is reverted."""
    sbx = _FakeSandbox({"a.py": b"x = 1\ny = 2\n"})
    out = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(path="a.py", start_line=2, end_line=2, new_text="def bad("),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert sbx._fs["a.py"] == b"x = 1\ny = 2\n"


@pytest.mark.asyncio
async def test_w3_file_insert_lines_bad_python_reverted():
    """W3 applies on file_insert_lines: inserting broken syntax is reverted."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    out = await FileInsertLinesTool().run(
        FileInsertLinesArgs(path="a.py", after_line=1, text="def broken("),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert sbx._fs["a.py"] == b"x = 1\n"


# ===========================================================================
# W4 — file_str_replace: anchored str-replace
# ===========================================================================


@pytest.mark.asyncio
async def test_w4_str_replace_unique_match_applied():
    """W4: a unique old_str is found exactly once → replaced."""
    sbx = _FakeSandbox({"a.py": b"x = 1\ny = 2\n"})
    out = await FileStrReplaceTool().run(
        FileStrReplaceArgs(path="a.py", old_str="x = 1", new_str="x = 99"),
        _ctx(sbx),
    )
    assert out.success is True
    assert sbx._fs["a.py"] == b"x = 99\ny = 2\n"


@pytest.mark.asyncio
async def test_w4_str_replace_non_unique_old_str_errors_no_write():
    """W4: multiple occurrences of old_str → error with line numbers, no write."""
    sbx = _FakeSandbox({"a.py": b"x = 1\nx = 1\n"})
    original = sbx._fs["a.py"]
    out = await FileStrReplaceTool().run(
        FileStrReplaceArgs(path="a.py", old_str="x = 1", new_str="x = 99"),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "old_str_not_unique"
    assert "Multiple occurrences" in out.content
    # line numbers present
    assert "1" in out.content and "2" in out.content
    # file untouched
    assert sbx._fs["a.py"] == original
    assert len(sbx.writes) == 0


@pytest.mark.asyncio
async def test_w4_str_replace_zero_match_fails_no_write():
    """W4: old_str not found (and no whitespace-strip match) → not-found error."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    out = await FileStrReplaceTool().run(
        FileStrReplaceArgs(path="a.py", old_str="z = 999", new_str="z = 0"),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "old_str_not_found"
    assert sbx._fs["a.py"] == b"x = 1\n"


@pytest.mark.asyncio
async def test_w4_str_replace_whitespace_strip_retry_succeeds():
    """W4: old_str with extra surrounding whitespace falls through to the
    whitespace-strip retry when the exact match fails."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    # Pass old_str with leading/trailing whitespace so exact fails, strip succeeds.
    out = await FileStrReplaceTool().run(
        FileStrReplaceArgs(path="a.py", old_str="  x = 1  ", new_str="x = 42"),
        _ctx(sbx),
    )
    assert out.success is True
    assert b"x = 42" in sbx._fs["a.py"]


@pytest.mark.asyncio
async def test_w4_str_replace_with_w3_gate_reverts_bad_python():
    """W4 + W3 integration: file_str_replace passes through the syntax gate;
    a replacement that introduces a SyntaxError is auto-reverted."""
    sbx = _FakeSandbox({"a.py": b"x = 1\n"})
    out = await FileStrReplaceTool().run(
        FileStrReplaceArgs(path="a.py", old_str="x = 1", new_str="def broken("),
        _ctx(sbx),
    )
    assert out.success is False
    assert out.error == "syntax_gate_reverted"
    assert sbx._fs["a.py"] == b"x = 1\n"


# ===========================================================================
# W4 registry — capability-based withholding
# ===========================================================================


def test_w4_registry_withholds_str_replace_from_non_anchored_policy():
    """standard + anchored_edit=False: file_str_replace is in AGENT_TOOLS but
    NOT in the advertised set (the policy's withheld_tools drives the exclusion)."""
    scope = agent_scope(model_policy=ModelExecutionPolicy(tier="standard", anchored_edit=False))
    assert "file_str_replace" in scope.allowed_tools
    assert scope.advertised_tools is not None
    assert "file_str_replace" not in scope.advertised_tools


def test_w4_registry_grants_str_replace_to_standard_anchored_policy():
    """standard + anchored_edit=True: file_str_replace is in the advertised set."""
    scope = agent_scope(model_policy=ModelExecutionPolicy.standard())
    assert "file_str_replace" in scope.allowed_tools
    # advertised_tools=None means "advertise all allowed"
    assert scope.advertised_tools is None


def test_w4_file_str_replace_in_agent_tools():
    """file_str_replace must be in AGENT_TOOLS (security allowlist)."""
    assert "file_str_replace" in AGENT_TOOLS


def test_w4_non_anchored_policy_advertised_excludes_str_replace():
    """standard + anchored_edit=False advertises AGENT_TOOLS minus file_str_replace."""
    scope = agent_scope(model_policy=ModelExecutionPolicy(tier="standard", anchored_edit=False))
    assert scope.advertised_tools == AGENT_TOOLS - frozenset({"file_str_replace"})
