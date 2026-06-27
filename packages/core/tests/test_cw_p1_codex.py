"""Codex P1 fixes on the context-management change (CW-1..CW-7).

P1-a (CORRECTNESS — reachable dangling pointer): the elided tool-call ARGUMENT
marker claimed the full content was "in the CURRENT WORKSPACE block". For assist-OFF
that is a reachable LIE: an elided arg is a write/edit BODY (an attempted or new
value), NOT the file's current content, so the block never carries it — even when the
call's target path is pinned in full. assist-OFF now rewrites EVERY elided arg to the
NEUTRAL non-dangling marker (re-issue / file_read recovery only); the K1 rejection text
is likewise tier-gated to a neutral file_read pointer for assist-OFF.

P1-c (assist-ON strict byte-identity): CW-3 reworded assist-ON-visible prose
(preamble / pointer / arg marker) for the location-independent PREFIX placement —
but assist-ON keeps the block in the TAIL, where the ORIGINAL pre-CW-3 directional
wording was correct. The wording is now gated so assist-ON renders the pre-CW-3
bytes; the location-independent wording applies ONLY to assist-OFF.
"""

from __future__ import annotations

import asyncio

from disco.core import ActionEvent, LLMMessage, ToolCall
from disco.core.events import (
    _ARG_SNIP_CHARS,
    _snip_args,
    find_elided_arg_markers,
    retarget_elided_arg_markers,
)
from disco.core.loop.context_budget import derive_context_caps
from disco.core.loop.view_render import ViewBuilder, workspace_snapshot_message
from disco.core.view import NoOpCondenser, View

# The EXACT pre-CW-3 workspace preamble (assist-ON / tail-placed block). Byte-identity
# of the assist-ON render to these bytes is the P1-c proof.
_PRE_CW3_PREAMBLE = (
    "# CURRENT WORKSPACE — your files on disk RIGHT NOW (authoritative).\n"
    "Below is the live, exact content of the files you are working on, "
    "re-read from disk this turn. It OVERRIDES any earlier or elided copy of "
    "these files shown above; trust THIS over your memory.\n"
    "To change a file: for a SMALL change, prefer `file_edit` (pass the exact "
    "text you see as `old`) or `file_replace_lines` / `file_insert_lines` (use "
    "the line numbers shown below). For a full rewrite, use `file_write` with "
    "the FULL new content — but you MUST call `file_read` on this file first "
    "if you have written to it before, or the write will be refused. Keep "
    "every existing function, constant, and docstring you are not deliberately "
    "removing — do not drop code you did not mean to delete.\n"
    "**SILENT CONTEXT** — use this block without narrating it. Do NOT "
    "acknowledge the snapshot in your reply (no 'I can see the files', "
    "'the workspace shows…', 'good, the content is here', etc.). "
    "Just continue the work.\n\n"
)

# The EXACT pre-CW-3 elided-arg marker (assist-ON), for an arg of N chars.
_PRE_CW3_MARKER_TAIL = (
    " chars — full content is in the CURRENT WORKSPACE block "
    "below; do not copy this placeholder into a tool argument>"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSandbox:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


class _FakeExecutor:
    def __init__(self, sandbox: _FakeSandbox) -> None:
        self.sandbox = sandbox


class _FakeLoop:
    def __init__(self, *, assist: bool, sandbox: _FakeSandbox, window: int = 192_000) -> None:
        self._assist = assist
        self.executor = _FakeExecutor(sandbox)
        self.condenser = NoOpCondenser()
        self.summarizer = None
        self._window = window

    def _driver_context_window(self) -> int:
        return self._window

    def _gate_recitation(self, view: View, events: list) -> View:
        return view

    def _f8_shrink_file_write_args(self, messages: list, events: list) -> list:
        return messages

    async def _emit(self, event) -> None:  # pragma: no cover
        raise AssertionError("NoOp condenser must not emit")

    async def _events(self) -> list:  # pragma: no cover
        raise AssertionError("_events must not be called")


def _write(path: str, content: str) -> ActionEvent:
    return ActionEvent(
        thought=f"write {path}",
        tool_call=ToolCall(tool_name="file_write", arguments={"path": path, "content": content}),
    )


def _assistant_tool_args(view: View, path: str) -> dict:
    for m in view.messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                if isinstance(tc, dict) and tc.get("arguments", {}).get("path") == path:
                    return tc["arguments"]
    raise AssertionError(f"no assistant tool_call for {path}")


# ---------------------------------------------------------------------------
# P1-a (round-2) — the assist-OFF arg marker is ALWAYS the NEUTRAL non-dangling
# marker (no "it's in the workspace block" claim), even when the path IS pinned:
# an elided arg is a write/edit body, NOT the file's current content, so the block
# does not carry it. No per-arg pinned-vs-omitted guessing.
# ---------------------------------------------------------------------------


def test_retarget_uses_neutral_marker_for_pinned_and_unpinned_alike():
    long = "x" * (_ARG_SNIP_CHARS + 1)
    msg = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[
            {"id": "c1", "name": "file_write",
             "arguments": _snip_args({"path": "pinned.py", "content": long})},
            {"id": "c2", "name": "file_write",
             "arguments": _snip_args({"path": "omitted.py", "content": long})},
        ],
    )
    out = retarget_elided_arg_markers([msg])
    pinned = out[0].tool_calls[0]["arguments"]["content"]
    unpinned = out[0].tool_calls[1]["arguments"]["content"]
    # BOTH get the neutral marker — NEITHER claims the content is in the block (the
    # arg value is a write body, not the file's current content, so the block does not
    # carry it even for the pinned path).
    assert "CURRENT WORKSPACE block" not in pinned
    assert "CURRENT WORKSPACE block" not in unpinned
    assert "do not copy or re-send" in pinned
    assert "do not copy or re-send" in unpinned
    # Both stay detectable by the K1 copy-back execution guard.
    assert find_elided_arg_markers({"content": pinned}) == ["content"]
    assert find_elided_arg_markers({"content": unpinned}) == ["content"]


def test_retarget_argument_without_a_path_uses_nondangling_marker():
    # A non-file tool whose long arg gets elided has no identifiable path → it must
    # NOT claim the content is in the workspace block (it is not pinned there).
    long = "y" * (_ARG_SNIP_CHARS + 1)
    msg = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[{"id": "c1", "name": "shell",
                     "arguments": _snip_args({"command": long})}],
    )
    out = retarget_elided_arg_markers([msg])
    marker = out[0].tool_calls[0]["arguments"]["command"]
    assert "CURRENT WORKSPACE block" not in marker
    assert "do not copy or re-send" in marker


def test_assist_off_build_marks_every_elided_arg_nondangling():
    # End-to-end through ViewBuilder (assist OFF): a small file is pinned in FULL and an
    # oversize file is TRUNCATED in the snapshot — but BOTH writes' elided ARG values get
    # the neutral non-dangling marker (the arg is the attempted body, never in the block).
    caps = derive_context_caps(assist=False, context_window=192_000)
    big = b"z" * (caps.per_file_chars + 20_000)  # exceeds the per-file pin → truncated
    sbx = _FakeSandbox({"small.py": b"const x = 1;\n", "big.py": big})
    builder = ViewBuilder(_FakeLoop(assist=False, sandbox=sbx))
    long = "q" * (_ARG_SNIP_CHARS + 1)
    events = [_write("small.py", long), _write("big.py", long)]

    view = asyncio.run(builder.build(events))
    small_marker = _assistant_tool_args(view, "small.py")["content"]
    big_marker = _assistant_tool_args(view, "big.py")["content"]
    # NEITHER arg marker claims the content is in the block (pinned or not).
    assert "CURRENT WORKSPACE block" not in small_marker
    assert "CURRENT WORKSPACE block" not in big_marker
    # De-temptified neutral marker: an elided-arg placeholder with the anti-copy
    # instruction, and crucially NO "re-issue the call" affordance (the copy-back bait).
    for marker in (small_marker, big_marker):
        assert "elided" in marker
        assert "do not copy or re-send" in marker
        assert "re-issue the call" not in marker


# ---------------------------------------------------------------------------
# P1-c — assist-ON renders the pre-CW-3 bytes (no wording drift)
# ---------------------------------------------------------------------------


def test_assist_on_preamble_is_byte_identical_to_pre_cw3():
    # pin_full=False is the assist-ON path. The rendered block preamble must be the
    # EXACT pre-CW-3 bytes (directional wording, correct for the tail-placed block).
    content = asyncio.run(
        workspace_snapshot_message(
            _FakeSandbox({"app.js": b"const x = 1;\n"}),
            [_write("app.js", "")],
            pin_full=False,
        )
    )
    assert content is not None
    assert content.content.startswith(_PRE_CW3_PREAMBLE)
    # And NONE of the CW-3 location-independent phrasing leaked into assist-ON.
    assert "in this prompt" not in content.content
    assert "OVERRIDES any other copy" not in content.content
    assert "in this block" not in content.content


def test_assist_on_snip_marker_is_byte_identical_to_pre_cw3():
    long = "x" * 4_000
    marker = _snip_args({"path": "a.py", "content": long})["content"]
    assert marker == f"<{len(long):,}{_PRE_CW3_MARKER_TAIL}"
    assert "below" in marker  # the directional word is restored for assist-ON
    # Still caught by the K1 execution guard.
    assert find_elided_arg_markers({"content": marker}) == ["content"]


def test_assist_on_build_keeps_directional_marker_and_pre_cw3_preamble():
    # Full assist-ON ViewBuilder build: the marker stays directional ("below", NOT
    # retargeted) and the snapshot preamble is the pre-CW-3 text.
    sbx = _FakeSandbox({"app.js": b"const x = 1;\n"})
    builder = ViewBuilder(_FakeLoop(assist=True, sandbox=sbx))
    long = "w" * (_ARG_SNIP_CHARS + 1)
    view = asyncio.run(builder.build([_write("app.js", long)]))
    marker = _assistant_tool_args(view, "app.js")["content"]
    assert marker == f"<{len(long):,}{_PRE_CW3_MARKER_TAIL}"
    snap = next(
        m for m in view.messages if m.role == "user" and m.content.startswith("# CURRENT WORKSPACE")
    )
    assert snap.content.startswith(_PRE_CW3_PREAMBLE)
