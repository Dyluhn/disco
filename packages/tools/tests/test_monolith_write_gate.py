"""MONO-1 — the monolith write gate (all tiers).

file_write / file_append refuse to CREATE or GROW a web-source file
(.html/.css/.js/…) past _MONOLITH_MAX_LINES / _MONOLITH_MAX_BYTES, with a
corrective nudge naming the split (styles.css / app.js / one page per file).

Live forensics behind the gate (Pillar-B gauntlet, 2026-07-07): one build wrote
a single 71KB / 1,770-line index.html; every later edit then had to be grounded
through the read-before-edit gates, costing 32 edit refusals and the whole
run budget. Same-scope builds structured as 3-4 files ≤24KB paid 4-6 refusals
and passed cleanly. Linting is too late — the gate fires AT the write.

Boundaries proven here:
  - new source file over the cap (lines OR bytes) → refused, nothing written
  - under-cap source file → allowed
  - non-source extensions (.json/.svg/.txt) → exempt at any size
  - SHRINKING an existing over-cap file → allowed (cleanup never blocked)
  - growing rewrite of an existing file past the cap → refused
  - append that CROSSES the cap → refused; append to an ALREADY-over-cap
    (legacy) file → allowed (repairs never obstructed)
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    _MONOLITH_MAX_BYTES,
    _MONOLITH_MAX_LINES,
    FileAppendArgs,
    FileAppendTool,
    FileReadArgs,
    FileReadTool,
    FileWriteArgs,
    FileWriteTool,
    reset_read_tracker,
)

# --- fakes (same shape as test_f3_read_before_write) --------------------------


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


def _ctx(sbx: _FakeSandbox, *, conv_id: str = "conv-mono") -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv_id,
    )


@pytest.fixture(autouse=True)
def _clear_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


# Content helpers. Line-cap breach with small bytes; byte-cap breach with few lines.
_OVER_LINES = "<div></div>\n" * (_MONOLITH_MAX_LINES + 1)
_UNDER_CAP = "<div></div>\n" * 50
_OVER_BYTES = "x" * (_MONOLITH_MAX_BYTES + 1) + "\n"


# --- creation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_source_file_over_line_cap_refused():
    sbx = _FakeSandbox()
    out = await FileWriteTool().run(
        FileWriteArgs(path="index.html", content=_OVER_LINES), _ctx(sbx)
    )
    assert out.success is False
    assert out.error == "monolith_write"
    msg = out.content or ""
    # The refusal is a DIAGNOSIS + RECIPE, not a rule citation: it must name the
    # exact tripwire, state that nothing was written, and give the html-specific
    # split recipe for the content the agent still holds.
    assert f"{_MONOLITH_MAX_LINES}-line cap" in msg  # which limit tripped
    assert "Nothing was written" in msg  # workspace-state clarity
    assert "styles.css" in msg and "app.js" in msg  # html recipe
    assert "<link" in msg and "<script src=" in msg  # concrete replacements
    assert sbx.writes == []  # nothing touched the workspace


@pytest.mark.asyncio
async def test_new_source_file_over_byte_cap_refused():
    sbx = _FakeSandbox()
    out = await FileWriteTool().run(FileWriteArgs(path="app.js", content=_OVER_BYTES), _ctx(sbx))
    assert out.success is False and out.error == "monolith_write"
    msg = out.content or ""
    assert "KB cap" in msg  # the byte tripwire is named
    # .js gets the module recipe, not the html one
    assert "ES modules" in msg and "import/export" in msg


@pytest.mark.asyncio
async def test_under_cap_source_file_allowed():
    sbx = _FakeSandbox()
    out = await FileWriteTool().run(FileWriteArgs(path="index.html", content=_UNDER_CAP), _ctx(sbx))
    assert out.success is True


@pytest.mark.asyncio
async def test_non_source_extension_exempt():
    """Data/asset files legitimately run large — only source files are gated.
    Content must be syntactically valid per type (the separate W3 syntax gate
    still applies to everything); only the SIZE exemption is under test here."""
    sbx = _FakeSandbox()
    big = "x" * (_MONOLITH_MAX_BYTES + 1024)
    cases = {
        "data.json": '{"data": "' + big + '"}',
        "notes.txt": big + "\n",
    }
    for path, content in cases.items():
        assert len(content.encode()) > _MONOLITH_MAX_BYTES
        out = await FileWriteTool().run(FileWriteArgs(path=path, content=content), _ctx(sbx))
        assert out.success is True, f"{path} should be exempt: {out.content}"


# --- existing files: shrink allowed, growth refused ------------------------------


@pytest.mark.asyncio
async def test_shrinking_an_over_cap_file_allowed():
    """Cleanup of a legacy monolith must never be blocked — a rewrite that is
    no larger than the current content passes even if still over the cap."""
    legacy = _OVER_LINES.encode()
    sbx = _FakeSandbox(existing={"index.html": legacy})
    # ground the rewrite (read-before-write gate) then shrink by one line
    await FileReadTool().run(FileReadArgs(path="index.html"), _ctx(sbx))
    smaller = "<div></div>\n" * _MONOLITH_MAX_LINES  # still over? == cap → under
    out = await FileWriteTool().run(FileWriteArgs(path="index.html", content=smaller), _ctx(sbx))
    assert out.success is True


@pytest.mark.asyncio
async def test_growing_rewrite_past_cap_refused():
    sbx = _FakeSandbox(existing={"index.html": _UNDER_CAP.encode()})
    await FileReadTool().run(FileReadArgs(path="index.html"), _ctx(sbx))
    out = await FileWriteTool().run(
        FileWriteArgs(path="index.html", content=_OVER_LINES), _ctx(sbx)
    )
    assert out.success is False and out.error == "monolith_write"


# --- append: crossing refused, legacy-over-cap repairable -------------------------


@pytest.mark.asyncio
async def test_append_crossing_cap_refused():
    """The installment monolith: a file just under the cap + a chunk that would
    push the RESULT over → refused (the result is judged, not the chunk)."""
    base = ("y" * 1024 + "\n") * 40  # ~40KB, 40 lines — under both caps
    sbx = _FakeSandbox(existing={"app.js": base.encode()})
    chunk = ("z" * 1024 + "\n") * 10  # +10KB → crosses 48KB
    out = await FileAppendTool().run(FileAppendArgs(path="app.js", content=chunk), _ctx(sbx))
    assert out.success is False and out.error == "monolith_write"


@pytest.mark.asyncio
async def test_append_to_legacy_over_cap_file_allowed():
    """A file ALREADY over the cap (created pre-gate) stays appendable — the
    gate stops new monoliths forming, it does not wall off repairs (e.g.
    re-adding a lost closing tag at EOF)."""
    sbx = _FakeSandbox(existing={"index.html": _OVER_LINES.encode()})
    out = await FileAppendTool().run(
        FileAppendArgs(path="index.html", content="</body></html>\n"), _ctx(sbx)
    )
    assert out.success is True


@pytest.mark.asyncio
async def test_append_under_cap_allowed():
    sbx = _FakeSandbox(existing={"index.html": _UNDER_CAP.encode()})
    out = await FileAppendTool().run(
        FileAppendArgs(path="index.html", content="<footer></footer>\n"), _ctx(sbx)
    )
    assert out.success is True
