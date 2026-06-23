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


async def _ctx(read_char_budget: int | None, assist: bool = False):
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
        assist=assist,
    )
    return ctx, inst


def _big_file_body(n_lines: int) -> bytes:
    # ~40 chars/line × 1000 lines ≈ 40KB — well over the 7k default, under a 48k pin.
    return ("\n".join(f"line {i:04d} " + "x" * 30 for i in range(n_lines))).encode()


async def test_default_budget_pages_large_file():
    # No threaded budget → the static 7k RAW default → a 40KB file is PAGED. (assist-OFF
    # ctx ⇒ raw budgeting; the line-numbered render of a 7k-raw page is ~1.1× the raw
    # budget for normal ~40-char lines, so the page is still well-bounded — NOT the 40KB.)
    ctx, inst = await _ctx(None)
    await inst.write_file("big.py", _big_file_body(1_000))
    out = await FileReadTool().run(FileReadTool().definition.args_model(path="big.py"), ctx)
    assert "read more with offset=" in out.content  # did NOT fit in one page
    assert len(out.content) < 9_000  # bounded near the 7k raw budget, not the 40KB file
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


async def test_pin_sized_short_line_file_reads_in_one_shot():
    # CW P1-b (round-2) — the INVARIANT: a RAW file at the per-file pin size reads in ONE
    # shot even with MANY SHORT lines (whose line-numbered render inflates the size well
    # past the raw pin). file_read budgets the page on RAW chars (the same unit the pin
    # uses), so the raw budget EQUALS the raw pin and a pin-fitting file never pages; the
    # render overhead is absorbed by the (raised) obs-snip cap, not the read budget.
    from disco.core.loop.context_budget import derive_context_caps

    caps = derive_context_caps(assist=False, context_window=192_000)
    per_file = caps.per_file_chars  # 48_000 (the pin ceiling)
    # Build a RAW file of ~per_file chars made of short (~12-char) lines.
    line = "x" * 11  # 11 content chars + newline = 12 raw chars/line
    n_lines = per_file // 12
    body = ("\n".join(line for _ in range(n_lines))).encode()
    assert abs(len(body) - per_file) <= 12  # raw size fits the per-file pin
    # The read budget is RAW and equals the raw pin (no render headroom on the budget).
    assert caps.read_char_budget == per_file

    ctx, inst = await _ctx(caps.read_char_budget)
    await inst.write_file("short.py", body)
    out = await FileReadTool().run(FileReadTool().definition.args_model(path="short.py"), ctx)
    assert "read more with offset=" not in out.content  # ONE shot, not paged
    assert f"of {n_lines}]" in out.content  # the header shows the whole file was read
    # The line-numbered render is materially LARGER than the raw pin (that was the bug) —
    # but the obs-snip cap covers it, so the one-shot observation is not snipped.
    assert len(out.content) > per_file  # rendered > raw, yet still one-shot
    assert len(out.content) <= caps.obs_snip_chars
    await inst.destroy()


async def test_pathological_xn_file_one_shots_and_obs_not_corrupted():
    # CW P1-b (round-2) — the codex pathological case: a 48k RAW file of "x\n"×24000
    # (2 raw chars/line) whose line-numbered render is ~192k. It must READ IN ONE SHOT
    # (raw budgeting) and its observation must NOT be corrupted-truncated by the snip.
    from disco.core.events import ObservationEvent, ToolResult, obs_snip_override
    from disco.core.loop.context_budget import derive_context_caps

    caps = derive_context_caps(assist=False, context_window=192_000)
    n_lines = 24_000
    body = ("x\n" * n_lines).encode()  # 48_000 raw chars == the per-file pin
    assert len(body) == caps.per_file_chars == caps.read_char_budget

    ctx, inst = await _ctx(caps.read_char_budget)
    await inst.write_file("xn.py", body)
    out = await FileReadTool().run(FileReadTool().definition.args_model(path="xn.py"), ctx)
    assert "read more with offset=" not in out.content  # ONE shot — not paged
    assert f"lines 1-{n_lines} of {n_lines}]" in out.content  # whole file in one read
    # The render is ~192k (far above raw) — the obs-snip cap is sized to cover it.
    assert len(out.content) > 180_000
    assert len(out.content) <= caps.obs_snip_chars  # the snip cap bounds it

    # Push it through the observation snip under the derived cap: NOT corrupted.
    token = obs_snip_override.set(caps.obs_snip_chars)
    try:
        msg = ObservationEvent(
            tool_result=ToolResult(
                call_id="c1", tool_name="file_read", success=True, content=out.content
            ),
            action_id="a1",
        ).to_llm_message()
    finally:
        obs_snip_override.reset(token)
    assert "snipped" not in msg.content  # the full read survives intact
    assert msg.content == out.content
    await inst.destroy()


async def test_normal_48k_code_file_one_shots():
    # A NORMAL ~48k source file (40-char lines) also reads in one shot under the pin.
    from disco.core.loop.context_budget import derive_context_caps

    caps = derive_context_caps(assist=False, context_window=192_000)
    n_lines = caps.per_file_chars // 41  # ~40-char content + newline ≈ 41 raw/line
    body = _big_file_body(n_lines)
    assert len(body) <= caps.per_file_chars  # raw fits the pin
    ctx, inst = await _ctx(caps.read_char_budget)
    await inst.write_file("code.py", body)
    out = await FileReadTool().run(FileReadTool().definition.args_model(path="code.py"), ctx)
    assert "read more with offset=" not in out.content  # ONE shot
    assert len(out.content) <= caps.obs_snip_chars
    await inst.destroy()


async def test_assist_on_charges_rendered_so_short_line_page_stays_under_obs_snip():
    # assist-ON keeps charging the LINE-NUMBERED render against the 7k budget (NOT raw),
    # so a short-line page can't render past the 8k obs-snip baseline. Proof: with the
    # same 7k budget, the assist-ON page contains FEWER lines than the assist-OFF (raw)
    # page of the SAME short-line file — i.e. assist-ON did not switch to raw budgeting.
    # 13k raw: over the 7k budget (so both page) but under the 14k F7 head-only threshold.
    body = ("x\n" * 6_500).encode()  # 13k raw of 2-char lines
    ctx_on, inst_on = await _ctx(_READ_CHAR_BUDGET, assist=True)
    await inst_on.write_file("s.py", body)
    out_on = await FileReadTool().run(FileReadTool().definition.args_model(path="s.py"), ctx_on)
    ctx_off, inst_off = await _ctx(_READ_CHAR_BUDGET, assist=False)
    await inst_off.write_file("s.py", body)
    out_off = await FileReadTool().run(FileReadTool().definition.args_model(path="s.py"), ctx_off)
    # Both page (16k > 7k budget); assist-ON's page is RENDER-bounded (stays under the 8k
    # obs-snip), assist-OFF's is RAW-bounded (more lines, larger render).
    assert "read more with offset=" in out_on.content
    assert "read more with offset=" in out_off.content
    assert len(out_on.content) < 8_000  # under the assist-ON obs-snip baseline
    assert out_on.content.count("\n") < out_off.content.count("\n")  # fewer lines/page
    await inst_on.destroy()
    await inst_off.destroy()
