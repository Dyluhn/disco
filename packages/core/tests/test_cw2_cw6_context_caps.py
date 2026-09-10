"""CW-2 + CW-6 — capability-gated, context-window-derived context caps.

Proves the read-loop root fix:
  * assist OFF + a real window → the snapshot pin / read / obs-snip caps scale up so
    an 11KB board.js and an 8KB windows.js are pinned in FULL (not head/tail
    truncated) — the files that re-read forever under the old hardcoded caps.
  * assist ON → the caps + the rendered snapshot are byte-identical to today.
  * the consistency invariant obs_snip >= read_budget >= per_file holds.
  * the events.py observation-snip override raises the snip for assist-OFF and is a
    no-op (byte-identical) when unset / at the 8k baseline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from disco.core import ActionEvent, LLMMessage, SqliteEventStore, ToolCall
from disco.core.events import ObservationEvent, ToolResult, obs_snip_override
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.loop.context_budget import (
    ASSIST_MAX_FILES,
    ASSIST_OBS_SNIP_CHARS,
    ASSIST_PER_FILE_CHARS,
    ASSIST_READ_CHAR_BUDGET,
    ASSIST_TOTAL_CHARS,
    ContextCaps,
    derive_context_caps,
)
from disco.core.loop.engine import AgentLoop
from disco.core.loop.view_render import _BASELINE_CAPS, ViewBuilder, workspace_snapshot_message
from disco.core.view import NoOpCondenser
from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer, NeverConfirm, ScriptedAgent

# A representative capable-model window (Claude/GPT large context).
WINDOW_192K = 192_000


# ---- derive_context_caps ----------------------------------------------------


def test_assist_on_returns_byte_identical_baseline():
    caps = derive_context_caps(assist=True, context_window=WINDOW_192K)
    assert caps == ContextCaps(
        max_files=ASSIST_MAX_FILES,
        per_file_chars=ASSIST_PER_FILE_CHARS,
        total_chars=ASSIST_TOTAL_CHARS,
        read_char_budget=ASSIST_READ_CHAR_BUDGET,
        obs_snip_chars=ASSIST_OBS_SNIP_CHARS,
    )
    # The view_render baseline (the caps==None fallback) is the same bundle.
    assert _BASELINE_CAPS == caps


def test_unknown_window_falls_back_to_baseline():
    for window in (None, 0, -5):
        caps = derive_context_caps(assist=False, context_window=window)
        assert caps.per_file_chars == ASSIST_PER_FILE_CHARS
        assert caps.total_chars == ASSIST_TOTAL_CHARS


def test_assist_off_192k_scales_caps_and_fits_big_source_files():
    caps = derive_context_caps(assist=False, context_window=WINDOW_192K)
    # Total snapshot budget ~50% of the window in chars (window*3.5*0.5 == 336k).
    assert 300_000 <= caps.total_chars <= 380_000
    # Per-file cap large enough for a real source file (40-60k range).
    assert 40_000 <= caps.per_file_chars <= 60_000
    # Breadth raised well above the assist 8.
    assert 16 <= caps.max_files <= 24
    # The golden-regression offenders both fit UNDER the per-file pin → full pin.
    assert 11_000 < caps.per_file_chars  # board.js (11KB)
    assert 8_047 < caps.per_file_chars  # windows.js (8047B)


def test_consistency_invariant_obs_ge_read_ge_per_file():
    # Across a wide range of windows the invariant must hold by construction.
    for window in (4_000, 8_000, 32_000, 128_000, 192_000, 1_000_000):
        caps = derive_context_caps(assist=False, context_window=window)
        assert caps.read_char_budget >= caps.per_file_chars
        assert caps.obs_snip_chars >= caps.read_char_budget


def test_small_window_capable_model_stays_bounded_and_sane():
    caps = derive_context_caps(assist=False, context_window=4_000)
    # Never WORSE than the assist baseline (floored), never unbounded.
    assert caps.total_chars >= ASSIST_TOTAL_CHARS
    assert caps.per_file_chars >= ASSIST_PER_FILE_CHARS
    assert caps.per_file_chars <= 48_000


def test_huge_window_is_capped_not_unbounded():
    caps = derive_context_caps(assist=False, context_window=1_000_000)
    # Per-file is ceilinged at 48k regardless of how large the window grows.
    assert caps.per_file_chars == 48_000
    # CW P1-b (round-2): the read budget is in RAW chars and EQUALS the raw per-file pin
    # (file_read budgets the page on raw file chars), so a raw pin-sized file reads in
    # ONE shot. The line-numbering render overhead lands on the OBS-SNIP cap, not the
    # read budget (so a single non-pinned read can't dump a huge rendered page).
    assert caps.read_char_budget == 48_000
    assert caps.read_char_budget == caps.per_file_chars
    # The obs snip covers the worst-case line-numbered render of a one-shot read of the
    # raw budget (~2-char lines, the x\n×24000 case) → a pin-fitting read is never snipped.
    from disco.core.loop.context_budget import _rendered_obs_floor

    assert caps.obs_snip_chars == _rendered_obs_floor(caps.read_char_budget)
    assert caps.obs_snip_chars >= caps.read_char_budget


def _real_loop(*, window: int | None, sandbox: _FakeSandbox | None = None) -> AgentLoop:
    executor = FakeExecutor()
    if sandbox is not None:
        executor.sandbox = sandbox
    return AgentLoop(
        "context-caps",
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=ModelExecutionPolicy.standard(),
        driver_context_window=window,
    )


def test_real_agent_loop_threads_driver_window_into_view_caps():
    loop = _real_loop(window=128_000)

    assert loop._driver_context_window() == 128_000
    assert ViewBuilder(loop)._resolve_caps() == derive_context_caps(
        assist=False,
        context_window=128_000,
    )


def test_real_agent_loop_128k_view_pins_an_11k_file_in_full():
    body = b"// board.js\n" + b"b" * 11_000
    loop = _real_loop(window=128_000, sandbox=_FakeSandbox({"board.js": body}))

    view = asyncio.run(ViewBuilder(loop).build(_writes(["board.js"])))
    snapshot = next(
        message for message in view.messages if "BEGIN FILE board.js" in message.content
    )

    assert "more chars" not in snapshot.content
    assert "b" * 500 in snapshot.content


# ---- snapshot pin: full vs truncated ----------------------------------------


class _FakeSandbox:
    def __init__(self, files: dict[str, bytes]):
        self._files = files

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


def _writes(paths: Iterable[str]) -> list[ActionEvent]:
    return [
        ActionEvent(
            thought=f"wrote {p}",
            tool_call=ToolCall(tool_name="file_write", arguments={"path": p, "content": ""}),
        )
        for p in paths
    ]


def _snapshot(files: dict[str, bytes], caps: ContextCaps | None) -> str:
    sbx = _FakeSandbox(files)
    msg = asyncio.run(workspace_snapshot_message(sbx, _writes(list(files)), caps=caps))
    assert isinstance(msg, LLMMessage)
    return msg.content


def test_assist_off_pins_11kb_and_8kb_files_in_full():
    board = b"// board.js\n" + b"b" * 11_000  # ~11KB
    windows = b"// windows.js\n" + b"w" * 8_047  # ~8KB
    caps = derive_context_caps(assist=False, context_window=WINDOW_192K)
    content = _snapshot({"board.js": board, "windows.js": windows}, caps)
    # Both files render in FULL — no head/tail truncation marker.
    assert "more chars" not in content
    assert "this file is large" not in content
    # The full body of each is present (the last bytes survive, i.e. not tail-clipped).
    assert "b" * 200 in content
    assert "w" * 200 in content


def test_assist_on_truncates_11kb_file_byte_identical_to_today():
    board = b"// board.js\n" + b"b" * 11_000
    # caps=None → the assist-ON baseline (8/6000/16000) == today's hardcoded path.
    none_content = _snapshot({"board.js": board}, None)
    baseline_caps_content = _snapshot(
        {"board.js": board}, derive_context_caps(assist=True, context_window=WINDOW_192K)
    )
    # The 11KB file EXCEEDS the 6000 per-file cap → head/tail truncated (today's
    # behavior), and threading the assist-ON caps explicitly is byte-identical to
    # the caps=None default.
    assert "more chars" in none_content
    assert none_content == baseline_caps_content


# ---- events.py observation snip override (CW-6) ------------------------------


def _obs(content: str) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name="file_read", success=True, content=content),
        action_id="a1",
    )


def test_obs_snip_default_unchanged_when_override_unset():
    big = "z" * 40_000
    msg = _obs(big).to_llm_message()
    # Default 8k snip is in effect → the content is snipped (corrupted middle).
    assert "snipped" in msg.content
    assert len(msg.content) < 40_000


def test_obs_snip_override_keeps_large_read_intact():
    big = "z" * 40_000
    token = obs_snip_override.set(48_000)  # the assist-OFF 192k obs cap
    try:
        msg = _obs(big).to_llm_message()
    finally:
        obs_snip_override.reset(token)
    # Under the raised cap the full read survives — NOT snipped to a corrupt head/tail.
    assert "snipped" not in msg.content
    assert big in msg.content


def test_obs_snip_override_at_baseline_is_noop():
    # An override equal to the 8k baseline must NOT change behavior (byte-identical).
    big = "z" * 40_000
    plain = _obs(big).to_llm_message().content
    token = obs_snip_override.set(ASSIST_OBS_SNIP_CHARS)  # 8000, not > 8000
    try:
        overridden = _obs(big).to_llm_message().content
    finally:
        obs_snip_override.reset(token)
    assert overridden == plain
