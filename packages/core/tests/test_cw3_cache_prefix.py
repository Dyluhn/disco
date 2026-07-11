"""CW-3 — cache the pinned working-set block (move it to the cacheable prefix).

Prompt caching is PREFIX-based: a byte-identical block appended in the VOLATILE
TAIL (after the changing event history) is never a stable prefix → never
cache-hits → re-billed every turn (the 60k/turn pain). CW-3 moves the pinned
CURRENT WORKSPACE block to BEFORE the event history (the stable prefix, right
after the system prompt) for capable models, keeps it byte-identical turn-over-
turn while the working set is unchanged, and adds an Anthropic cache breakpoint
after it. assist-ON behavior is byte-identical to before (tail + W2 pointer).

Coverage:
  (a) two consecutive assist-OFF turns with NO file change → the pinned block is
      byte-identical AND positioned BEFORE the event history (cacheable).
  (b) the Anthropic payload gets a cache_control breakpoint AFTER the prefix block.
  (c) the OpenAI/OpenRouter path keeps the block in the prefix (a plain string at
      index 1, within the prompt_cache_key prefix span).
  (d) assist-ON render is unchanged: snapshot in the TAIL + the W2 pointer collapse
      still fires on the second unchanged turn.
  (e) NO directional words (below/above/earlier/later/following/preceding) in the
      assist-OFF generated workspace/recovery PROSE.
"""

from __future__ import annotations

import asyncio
import re

from disco.core import ActionEvent, LLMMessage, ToolCall
from disco.core.events import WORKSPACE_SNAPSHOT_SENTINEL, _snip_args
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
    ToolSpec,
)
from disco.core.loop.dedup import _SUPERSEDED_READ_NOTICE
from disco.core.loop.view_render import ViewBuilder, workspace_snapshot_message
from disco.core.view import NoOpCondenser, View

# Forbidden directional words — the block moved, so any of these in the
# workspace/recovery PROSE is now a lie about where the content lives.
_DIRECTIONAL = re.compile(r"\b(?:below|above|earlier|later|following|preceding)\b", re.I)


# ---------------------------------------------------------------------------
# Fakes — a minimal sandbox + loop so ViewBuilder.build can run end-to-end.
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
    """The minimal ViewBuilder collaborator surface used by ``build``."""

    def __init__(self, *, assist: bool, sandbox: _FakeSandbox, window: int = 192_000) -> None:
        self._assist = assist
        self.executor = _FakeExecutor(sandbox)
        self.condenser = NoOpCondenser()  # never condenses → no tombstone path
        self.summarizer = None
        self._window = window

    def _driver_context_window(self) -> int:
        return self._window

    def _gate_recitation(
        self, view: View, events: list, *, context_pack_active: bool = False
    ) -> View:
        return view  # no recap gate in the test

    def _f8_shrink_file_write_args(self, messages: list, events: list) -> list:
        return messages  # only reached when assist is ON; identity here

    async def _emit(self, event) -> None:  # pragma: no cover - NoOp condenser never emits
        raise AssertionError("condenser is NoOp; _emit must not be called")

    async def _events(self) -> list:  # pragma: no cover
        raise AssertionError("_events must not be called without a tombstone")


def _writes(paths: list[str]) -> list[ActionEvent]:
    return [
        ActionEvent(
            thought=f"wrote {p}",
            tool_call=ToolCall(tool_name="file_write", arguments={"path": p, "content": ""}),
        )
        for p in paths
    ]


def _snapshot_msg(view: View) -> LLMMessage | None:
    for m in view.messages:
        if m.role == "user" and m.content.startswith(WORKSPACE_SNAPSHOT_SENTINEL):
            return m
    return None


def _snapshot_index(view: View) -> int:
    for i, m in enumerate(view.messages):
        if m.role == "user" and m.content.startswith(WORKSPACE_SNAPSHOT_SENTINEL):
            return i
    return -1


# ---------------------------------------------------------------------------
# (a) assist-OFF: byte-identical block, positioned before the event history
# ---------------------------------------------------------------------------


def test_assist_off_block_is_byte_stable_and_in_the_prefix():
    files = {"app.js": b"const x = 1;\n", "board.js": b"// board\n" + b"b" * 200}
    sbx = _FakeSandbox(files)
    builder = ViewBuilder(_FakeLoop(assist=False, sandbox=sbx))
    events = _writes(["app.js", "board.js"])

    view1 = asyncio.run(builder.build(events))
    view2 = asyncio.run(builder.build(events))  # same working set, no change

    snap1, snap2 = _snapshot_msg(view1), _snapshot_msg(view2)
    assert snap1 is not None and snap2 is not None
    # Byte-identical across the two unchanged turns → a stable cache prefix.
    assert snap1.content == snap2.content
    # Positioned BEFORE the event history: the block is the FIRST message in
    # view.messages (routing prepends the system prompt → it lands at index 1,
    # immediately after the system prompt, before the conversation).
    assert _snapshot_index(view1) == 0
    assert _snapshot_index(view2) == 0
    # The block is NOT in the tail (some history follows it).
    assert len(view1.messages) > 1
    # pin_full → full bodies, never the per-turn pointer collapse (which would
    # break byte-stability for an unchanged file).
    assert "BEGIN FILE app.js" in snap1.content
    assert "BEGIN FILE app.js" in snap2.content
    assert "unchanged since last shown" not in snap1.content


# ---------------------------------------------------------------------------
# (d) assist-ON: snapshot in the TAIL + W2 pointer collapse preserved
# ---------------------------------------------------------------------------


def test_assist_on_keeps_tail_placement_and_pointer_collapse():
    files = {"app.js": b"const x = 1;\n"}
    sbx = _FakeSandbox(files)
    builder = ViewBuilder(_FakeLoop(assist=True, sandbox=sbx))
    events = _writes(["app.js"])

    view1 = asyncio.run(builder.build(events))
    # Snapshot is the LAST message (tail), not the prefix.
    assert _snapshot_index(view1) == len(view1.messages) - 1
    assert _snapshot_index(view1) != 0
    snap1 = _snapshot_msg(view1)
    assert snap1 is not None and "BEGIN FILE app.js" in snap1.content

    # Second unchanged turn → the W2 pointer collapse fires (byte-identical to
    # the pre-CW-3 assist-ON behavior): no full body, a one-line pointer instead.
    # CW P1-c — assist-ON renders the pre-CW-3 pointer wording ("current, shown
    # earlier"), not the CW-3 location-independent wording (which is assist-OFF only).
    view2 = asyncio.run(builder.build(events))
    snap2 = _snapshot_msg(view2)
    assert snap2 is not None
    assert "BEGIN FILE app.js" not in snap2.content
    assert "current, shown earlier" in snap2.content
    assert _snapshot_index(view2) == len(view2.messages) - 1


# ---------------------------------------------------------------------------
# (b)+(c) provider cache marking — prefix block in the cached span
# ---------------------------------------------------------------------------


def _provider_req(messages: list[LLMMessage], tools=("shell", "file_write")) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=messages,
        tools=[ToolSpec(name=n, description="", parameters_schema={}) for n in tools],
    )


def _prefix_messages() -> list[LLMMessage]:
    # The assist-OFF wire shape: system, then the pinned workspace block, then history.
    snap = asyncio.run(
        workspace_snapshot_message(
            _FakeSandbox({"app.js": b"const x = 1;\n"}),
            _writes(["app.js"]),
            pin_full=True,
        )
    )
    assert snap is not None
    return [
        LLMMessage(role="system", content="You are an agent."),
        snap,
        LLMMessage(role="user", content="build the app"),
        LLMMessage(role="assistant", content="working on it"),
    ]


def test_anthropic_breakpoint_after_the_prefix_workspace_block():
    adapter = OpenAIProvider("http://x/v1", api_key=None)
    body = adapter._payload(
        _provider_req(_prefix_messages()), "anthropic/claude-3.5-sonnet", stream=False
    )
    msgs = body["messages"]
    # index 0 = system (cacheable text block); index 1 = the pinned workspace block.
    assert msgs[0]["role"] == "system"
    assert isinstance(msgs[0]["content"], list)
    assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    block = msgs[1]
    assert block["role"] == "user"
    # The workspace block became a cacheable text block → breakpoint AFTER it.
    assert isinstance(block["content"], list)
    assert block["content"][0]["text"].startswith(WORKSPACE_SNAPSHOT_SENTINEL)
    assert block["content"][0]["cache_control"] == {"type": "ephemeral"}
    # the tool surface breakpoint is still present
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_openai_keeps_workspace_block_plain_in_the_prefix():
    adapter = OpenAIProvider("http://x/v1", api_key=None)
    body = adapter._payload(_provider_req(_prefix_messages()), "qwen3.6-27b", stream=False)
    msgs = body["messages"]
    # Local/OpenAI: the block stays a plain string (block arrays would be rejected),
    # sitting at index 1 — WITHIN the immutable prefix the prompt_cache_key keys on.
    block = msgs[1]
    assert block["role"] == "user"
    assert isinstance(block["content"], str)
    assert block["content"].startswith(WORKSPACE_SNAPSHOT_SENTINEL)
    assert "cache_control" not in block
    assert body["prompt_cache_key"].startswith("pmx-")


def test_anthropic_tail_block_is_not_marked():
    # assist-ON wire shape: the workspace block is in the TAIL (after history),
    # NOT at sys_idx+1 → it must NOT get a cache breakpoint (byte-identical to today).
    adapter = OpenAIProvider("http://x/v1", api_key=None)
    snap = asyncio.run(
        workspace_snapshot_message(
            _FakeSandbox({"app.js": b"const x = 1;\n"}), _writes(["app.js"]), pin_full=True
        )
    )
    assert snap is not None
    messages = [
        LLMMessage(role="system", content="You are an agent."),
        LLMMessage(role="user", content="build the app"),
        snap,  # tail position
    ]
    body = adapter._payload(_provider_req(messages), "anthropic/claude-3.5-sonnet", stream=False)
    # The message right after system (index 1) is plain history, not marked.
    assert isinstance(body["messages"][1]["content"], str)
    # The tail block stays a plain string (no breakpoint added to it).
    assert isinstance(body["messages"][2]["content"], str)


# ---------------------------------------------------------------------------
# (e) location-independent wording — no directional words in generated prose
# ---------------------------------------------------------------------------


def test_no_directional_words_in_assist_off_workspace_prose():
    # File bytes are directional-word-free so any match comes from the PROSE
    # (preamble + markers), not the file content inside the block (codex scope).
    snap = asyncio.run(
        workspace_snapshot_message(
            _FakeSandbox({"app.js": b"const x = 1;\n"}),
            _writes(["app.js"]),
            pin_full=True,
        )
    )
    assert snap is not None
    hit = _DIRECTIONAL.search(snap.content)
    assert hit is None, f"directional word in the workspace block prose: {hit!r}"


def test_no_directional_words_in_recovery_markers():
    # The superseded-read pointer (assist-OFF, pinned path) references the workspace
    # block and must name it location-independently.
    superseded = _SUPERSEDED_READ_NOTICE.format(path="app.js")
    assert _DIRECTIONAL.search(superseded) is None, superseded
    assert "in this prompt" in superseded

    # Elided args render as the canonical DISCO-ELIDED sentinel; retargeting a
    # count-bearing marker keeps it directional-word-free and non-dangling.
    from disco.core.events import retarget_elided_arg_markers

    raw = _snip_args({"content": "x" * 4_000})["content"]
    assert raw.startswith("[[DISCO-ELIDED:")
    assert _DIRECTIONAL.search(raw) is None, raw
    msg = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[{
            "id": "c1",
            "name": "file_write",
            "arguments": {"path": "app.js", "content": raw},
        }],
    )
    out = retarget_elided_arg_markers([msg])
    snipped = out[0].tool_calls[0]["arguments"]["content"]
    assert _DIRECTIONAL.search(snipped) is None, snipped
    assert "CURRENT WORKSPACE block" not in snipped  # neutral — no dangling block claim
    assert snipped.startswith("[[DISCO-ELIDED:")
    assert "history display only" in snipped
