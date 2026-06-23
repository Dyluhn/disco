"""CW-6 — the file_read page budget is capability-derived via ToolContext.

A file that fits the assist-OFF snapshot pin must also be readable in ONE shot:
ctx.read_char_budget (threaded by the runtime from derive_context_caps) raises the
file_read page budget. Unset → the static 7k default (assist-ON parity).
"""

from __future__ import annotations

import pytest
from disco.tools import (
    DefaultToolExecutor,
    ToolScope,
    build_default_registry,
)
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import FileReadTool
from disco.tools.builtin.files import _READ_CHAR_BUDGET
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxService
from tool_fakes import FakeSandboxInstance, call

pytestmark = pytest.mark.asyncio


async def _ctx(read_char_budget: int | None):
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    ctx = ToolContext(
        sandbox=inst,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="c",
        read_char_budget=read_char_budget,
    )
    return ctx, inst


def _big_file_body(n_lines: int) -> bytes:
    # ~40 chars/line × 1000 lines ≈ 40KB — well over the 7k default, under a 48k pin.
    return ("\n".join(f"line {i:04d} " + "x" * 30 for i in range(n_lines))).encode()


async def test_default_budget_pages_large_file():
    # No threaded budget → the static 7k default → a 40KB file is PAGED.
    ctx, inst = await _ctx(None)
    await inst.write_file("big.py", _big_file_body(1_000))
    out = await FileReadTool().run(FileReadTool().definition.args_model(path="big.py"), ctx)
    assert "read more with offset=" in out.content  # did NOT fit in one page
    assert len(out.content) <= _READ_CHAR_BUDGET + 200  # bounded by the default budget
    await inst.destroy()


async def test_threaded_budget_reads_pin_sized_file_in_one_shot():
    # An assist-OFF 48k read budget → the same 40KB file reads in ONE page.
    ctx, inst = await _ctx(48_000)
    body = _big_file_body(1_000)
    await inst.write_file("big.py", body)
    out = await FileReadTool().run(FileReadTool().definition.args_model(path="big.py"), ctx)
    assert "read more with offset=" not in out.content  # whole file in one shot
    assert "line 0999" in out.content  # the LAST line is present (fully read)
    await inst.destroy()


async def test_unset_budget_is_byte_identical_to_constant():
    # read_char_budget=None and ==_READ_CHAR_BUDGET produce identical output.
    body = _big_file_body(1_000)
    ctx_none, inst1 = await _ctx(None)
    await inst1.write_file("f.py", body)
    out_none = await FileReadTool().run(
        FileReadTool().definition.args_model(path="f.py"), ctx_none
    )
    ctx_const, inst2 = await _ctx(_READ_CHAR_BUDGET)
    await inst2.write_file("f.py", body)
    out_const = await FileReadTool().run(
        FileReadTool().definition.args_model(path="f.py"), ctx_const
    )
    assert out_none.content == out_const.content
    await inst1.destroy()
    await inst2.destroy()


async def test_executor_threads_read_char_budget_end_to_end():
    # The runtime-style wiring: an executor constructed with read_char_budget stamps
    # it onto every ToolContext, so file_read honors it without per-tool plumbing.
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("big.py", _big_file_body(1_000))
    ex = DefaultToolExecutor(
        build_default_registry(),
        ToolScope(allowed_tools=frozenset({"file_read"})),
        sandbox=sandbox,
        read_char_budget=48_000,
    )
    res = await ex.execute(call("file_read", path="big.py"))
    assert res.success
    assert "read more with offset=" not in res.content  # whole file in one shot
    assert "line 0999" in res.content


async def test_executor_default_budget_pages_large_file():
    # No read_char_budget threaded (assist-ON / non-build executors) → 7k default.
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("big.py", _big_file_body(1_000))
    ex = DefaultToolExecutor(
        build_default_registry(),
        ToolScope(allowed_tools=frozenset({"file_read"})),
        sandbox=sandbox,
    )
    res = await ex.execute(call("file_read", path="big.py"))
    assert res.success
    assert "read more with offset=" in res.content  # paged at the default budget
