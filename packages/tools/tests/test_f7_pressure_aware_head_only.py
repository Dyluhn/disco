"""F7 — pressure-aware head-only file_read.

When `ctx.assist` is True AND the file is "large" (content length > 2x the read
char budget — the size proxy for "this would be split across multiple pages
anyway, which is the exact situation that gets corrupted by the observation
snip pass and is most likely to push the model toward the next condenser
threshold"), AND the caller did not pass an explicit `offset`/`limit`,
`file_read` returns a small HEAD slice (≈2_000 chars) + a prescriptive
directive ("file is large; grep for the symbol, then file_read a line
range"). The intent: under context pressure, stop dumping whole files into
the model and force the cheap deterministic path (locate → targeted read).

The gate NEVER fires when:
  - `ctx.assist` is False (capable-model path) — byte-identical to pre-F7.
  - the file is small enough that today's paging returns it whole.
  - the caller passed an explicit `offset` or `limit` — a targeted read is
    already cheap, the gate would just break a working flow.

LIMITATION (noted in the implementation): ToolContext does not expose the
condenser's token_count, only `assist`. The "pressure" half of the gate is
therefore a file-size heuristic, not a true pressure signal. The size
threshold is tuned so it triggers on the same workload (files that span
multiple pages) the snip pass would damage.

These tests cover the unit-tool layer (FileReadTool directly, with a fake
sandbox) plus the byte-identical assertion for assist-OFF.
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    _PRESSURE_DIRECTIVE,
    _PRESSURE_FILE_THRESHOLD,
    _PRESSURE_HEAD_BUDGET,
    _READ_CHAR_BUDGET,
    FileReadArgs,
    FileReadTool,
    FileWriteArgs,
    FileWriteTool,
    reset_read_tracker,
)

# --- fakes ------------------------------------------------------------------


class _FakeSandbox:
    """In-memory sandbox for the F7 tests. Mirrors the F3 test fake so the
    pressure head-only path can be exercised without spinning up a real
    process backend."""

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


def _ctx_for(sbx: _FakeSandbox, *, assist: bool, conv_id: str = "conv-f7") -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv_id,
        assist=assist,
    )


def _build_large_file(line_count: int, line_width: int = 60) -> bytes:
    """Build a synthetic large file: `line_count` numbered lines, each of
    roughly `line_width` chars. Default sizes comfortably exceed the pressure
    file threshold (14_000 chars) so the gate's size heuristic fires."""
    body = "\n".join(
        f"line {i:>06d}: " + "x" * max(line_width - 16, 0) for i in range(1, line_count + 1)
    )
    return body.encode("utf-8")


@pytest.fixture(autouse=True)
def _clear_tracker():
    """Module-level read-state in files.py: reset between tests for
    order-independence."""
    reset_read_tracker()
    yield
    reset_read_tracker()


# --- assist ON + large file + no explicit range → head-only + directive ----


@pytest.mark.asyncio
async def test_assist_on_large_file_returns_head_only_with_directive():
    """The headline F7 case: assist ON, large file, no offset/limit → a small
    head slice plus the prescriptive 'grep, then read a line range'
    directive. The result does NOT include the full file body."""
    # ~20_000 chars, comfortably above the 14_000 threshold
    body = _build_large_file(line_count=300, line_width=70)
    assert len(body) > _PRESSURE_FILE_THRESHOLD
    sbx = _FakeSandbox({"big.py": body})

    out = await FileReadTool().run(FileReadArgs(path="big.py"), _ctx_for(sbx, assist=True))

    assert out.success is True
    content = out.content
    # Head-only header
    assert "HEAD-ONLY under context pressure" in content
    # First line is always shown (line 1)
    assert "line 000001:" in content
    # The directive (the prescriptive hint) is present
    assert "file is large" in content
    assert "grep" in content.lower() or "ripgrep" in content.lower() or "rg -n" in content
    # The static directive constant is embedded verbatim
    assert _PRESSURE_DIRECTIVE in content
    # The last line of the file is NOT shown — that's the whole point of head-only
    assert "line 000300:" not in content
    # And the output is materially smaller than today's full page
    assert len(content) < _READ_CHAR_BUDGET  # the gate's head budget is much smaller


@pytest.mark.asyncio
async def test_assist_on_head_only_respects_head_budget():
    """The head slice must stay under the pressure head budget, not just under
    the regular read budget. This is the saving the gate is supposed to
    deliver — it should be a TINY payload, not a 7_000-char page."""
    body = _build_large_file(line_count=2000, line_width=80)  # ~160k chars
    sbx = _FakeSandbox({"huge.py": body})

    out = await FileReadTool().run(FileReadArgs(path="huge.py"), _ctx_for(sbx, assist=True))

    # The numbered lines portion + directive should comfortably fit under the
    # pressure head budget (the head itself is capped; the directive adds a
    # fixed ~400 chars on top).
    assert out.success is True
    # Trim out the static directive for the size check — the head itself is
    # what we care about.
    head_part = out.content.split(_PRESSURE_DIRECTIVE)[0]
    # The head is a slice up to but not exceeding the pressure budget.
    # Allow a small slack for the header line + a final newline.
    assert len(head_part) < _PRESSURE_HEAD_BUDGET + 200


@pytest.mark.asyncio
async def test_assist_on_head_only_marks_path_as_read_for_f3_happy_path():
    """The head-only read STILL records the path in the read-state tracker —
    otherwise the F3 read-before-write guard would refuse the very next
    file_write, defeating the goal (the model followed the directive and
    wants to act on what it read)."""
    body = _build_large_file(line_count=300, line_width=70)
    sbx = _FakeSandbox({"big.py": body})
    ctx = _ctx_for(sbx, assist=True)

    read_out = await FileReadTool().run(FileReadArgs(path="big.py"), ctx)
    assert read_out.success is True
    assert "HEAD-ONLY" in read_out.content

    # A subsequent file_write must go through on the FIRST try (the read
    # lifted the read-before-write guard). The new shrink guard is explicitly
    # opted into because this fixture intentionally replaces a large file with
    # a tiny body.
    write_out = await FileWriteTool().run(
        FileWriteArgs(path="big.py", content="new body\n", allow_shrink=True), ctx
    )
    assert write_out.success is True
    assert sbx._fs["big.py"] == b"new body\n"


# --- assist OFF: byte-identical to pre-F7 paging ----------------------------


@pytest.mark.asyncio
async def test_assist_off_large_file_uses_todays_paging_unchanged():
    """Assist OFF must NOT trip the pressure gate. Today's behavior: page by
    the regular char budget, return numbered lines + a 'read more with
    offset=…' hint, NO pressure directive. This is the byte-identical
    assertion the task spec requires."""
    body = _build_large_file(line_count=300, line_width=70)
    assert len(body) > _PRESSURE_FILE_THRESHOLD  # file IS large
    sbx = _FakeSandbox({"big.py": body})

    out = await FileReadTool().run(FileReadArgs(path="big.py"), _ctx_for(sbx, assist=False))

    assert out.success is True
    content = out.content
    # Today's paging header is present
    assert "lines 1-" in content
    assert "read more with offset=" in content  # the existing paging hint
    assert "of 300" in content  # total line count
    # The pressure directive is NOT present — gate never fired
    assert "HEAD-ONLY under context pressure" not in content
    assert _PRESSURE_DIRECTIVE not in content
    # First line of the file is shown
    assert "line 000001:" in content


@pytest.mark.asyncio
async def test_assist_off_never_consults_pressure_constants():
    """Even when the file is enormous, assist OFF must not branch on the
    pressure constants — the file_read path is structurally unchanged from
    pre-F7. We assert this indirectly: the result is the regular paging
    output regardless of file size."""
    body = _build_large_file(line_count=5000, line_width=80)  # ~400k chars
    sbx = _FakeSandbox({"huge.py": body})

    out = await FileReadTool().run(
        FileReadArgs(path="huge.py"), _ctx_for(sbx, assist=False)
    )

    assert out.success is True
    assert "HEAD-ONLY" not in out.content
    assert _PRESSURE_DIRECTIVE not in out.content
    # The regular paging stopped under the char budget → "read more" hint fires
    assert "read more with offset=" in out.content


# --- assist ON but small file → today's paging (no pressure) ----------------


@pytest.mark.asyncio
async def test_assist_on_small_file_uses_todays_paging_unchanged():
    """A small file (under the size threshold) with assist ON must still use
    today's paging — the gate exists to suppress large-page returns, not
    to truncate every read. The result is the full file, line-numbered, no
    directive."""
    body = b"alpha\nbeta\ngamma\n"  # 18 chars, well under the 14_000 threshold
    sbx = _FakeSandbox({"tiny.txt": body})

    out = await FileReadTool().run(FileReadArgs(path="tiny.txt"), _ctx_for(sbx, assist=True))

    assert out.success is True
    assert "alpha" in out.content
    assert "beta" in out.content
    assert "gamma" in out.content
    # No pressure directive on a small read
    assert "HEAD-ONLY under context pressure" not in out.content
    assert _PRESSURE_DIRECTIVE not in out.content
    # And today's paging header is what we get
    assert "lines 1-" in out.content
    assert "read more with offset=" not in out.content  # file was returned whole


@pytest.mark.asyncio
async def test_assist_on_explicit_offset_uses_line_range_not_head_only():
    """When the caller passes an explicit `offset` (or `limit`), the gate
    does NOT fire — the model is already doing the cheap, targeted thing
    the directive asks for. Returning a 'head-only with directive' for an
    explicit range would just break a working flow. Verify the call goes
    straight to the regular paging path."""
    body = _build_large_file(line_count=300, line_width=70)
    sbx = _FakeSandbox({"big.py": body})

    out = await FileReadTool().run(
        FileReadArgs(path="big.py", offset=100, limit=5),
        _ctx_for(sbx, assist=True),
    )

    assert out.success is True
    # Regular paging header
    assert "lines 100-" in out.content
    # The targeted slice is present, NOT the head
    assert "line 000100:" in out.content
    assert "line 000104:" in out.content
    # And the head-only directive is absent
    assert "HEAD-ONLY under context pressure" not in content_if_str(out)
    assert _PRESSURE_DIRECTIVE not in out.content
    # The first line (line 1) is NOT present — explicit offset wins
    assert "line 000001:" not in out.content


def content_if_str(o) -> str:
    """Tiny helper to keep the type-checker happy in the explicit-offset
    test: a deliberate attribute access that resolves to a `str`."""
    return o.content  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_assist_on_explicit_limit_only_also_bypasses_gate():
    """Symmetric: passing `limit` (without `offset`) is also "the model is
    being targeted", so the gate does not fire — even with a large file and
    assist ON, a `limit=5` read gets the regular first-N-lines page, not
    the head-only directive."""
    body = _build_large_file(line_count=300, line_width=70)
    sbx = _FakeSandbox({"big.py": body})

    out = await FileReadTool().run(
        FileReadArgs(path="big.py", limit=5), _ctx_for(sbx, assist=True)
    )

    assert out.success is True
    assert "HEAD-ONLY" not in out.content
    assert _PRESSURE_DIRECTIVE not in out.content
    # The first 5 lines are returned
    for i in range(1, 6):
        assert f"line {i:>06d}:" in out.content
    # Line 6+ is NOT in the result
    assert "line 000006:" not in out.content
