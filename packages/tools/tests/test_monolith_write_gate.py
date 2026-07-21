"""P-F5B — large source files stay editable without dictating product shape.

The old MONO-1 policy hard-refused source files above 800 lines / 48 KiB. A real
eight-revision canary proved that this universal web recipe can directly
contradict an explicit user/target file layout. Large-file reliability now comes
from bounded reads, fresh grounding, targeted edits, and atomic writes — not a
hard product-shape gate.
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    FileAppendArgs,
    FileAppendTool,
    FileReadArgs,
    FileReadTool,
    FileReplaceLinesArgs,
    FileReplaceLinesTool,
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


# Realistic valid CSS shapes. The first reproduces the live 732 -> 895 append;
# the second is the earlier 1,770-line / roughly 71 KiB stress shape.
_BASE_732 = "".join(f"/* existing rule {i:04d} {'x' * 30} */\n" for i in range(1, 733))
_PRICING_163 = "".join(f"/* pricing rule {i:04d} */\n" for i in range(1, 164))
_LARGE_1770 = "".join(f"/* source rule {i:04d} {'x' * 24} */\n" for i in range(1, 1771))


# --- creation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_1770_line_source_file_is_supported():
    sbx = _FakeSandbox()
    out = await FileWriteTool().run(
        FileWriteArgs(path="styles.css", content=_LARGE_1770), _ctx(sbx)
    )
    assert out.success is True, out.content
    assert sbx._fs["styles.css"] == _LARGE_1770.encode()


@pytest.mark.asyncio
async def test_live_732_to_895_line_stylesheet_append_succeeds_in_place():
    sbx = _FakeSandbox(existing={"styles.css": _BASE_732.encode()})
    out = await FileAppendTool().run(
        FileAppendArgs(path="styles.css", content=_PRICING_163), _ctx(sbx)
    )
    assert out.success is True, out.content
    result = sbx._fs["styles.css"].decode()
    assert len(result.splitlines()) == 895
    assert "pricing rule 0163" in result
    assert set(sbx._fs) == {"styles.css"}


@pytest.mark.asyncio
async def test_large_source_supports_bounded_read_then_targeted_edit():
    sbx = _FakeSandbox(existing={"styles.css": _LARGE_1770.encode()})
    ctx = _ctx(sbx)

    read = await FileReadTool().run(FileReadArgs(path="styles.css", offset=890, limit=20), ctx)
    assert read.success is True
    assert "source rule 0900" in (read.content or "")

    out = await FileReplaceLinesTool().run(
        FileReplaceLinesArgs(
            path="styles.css",
            start_line=900,
            end_line=900,
            new_text="/* source rule 0900 edited */",
        ),
        ctx,
    )
    assert out.success is True, out.content
    assert "source rule 0900 edited" in sbx._fs["styles.css"].decode()
