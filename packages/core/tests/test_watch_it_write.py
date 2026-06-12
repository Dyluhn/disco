"""Watch-it-write: the driver's streamed tool-call argument fragments are decoded
into growing file-content frames and broadcast over the store's EPHEMERAL bus
(never persisted). This is what makes a `file_write` visibly assemble on screen
instead of appearing all at once after a long wait.

These tests are deterministic — they feed synthetic StreamChunks through the REAL
engine hook + REAL store ephemeral pub/sub (no model, no network), so the wiring
is covered without a live LLM.
"""

from __future__ import annotations

import asyncio
import json

from disco.core import SqliteEventStore
from disco.core.llm.types import StreamChunk
from disco.core.loop.stream_extract import extract_partial_string_field
from loop_fakes import ScriptedAgent, build_loop, finish_step

# ---- the partial-JSON field extractor ---------------------------------------


def test_extract_growing_content():
    assert extract_partial_string_field('{"path":"a","content":"// hi', "content") == "// hi"


def test_extract_decodes_escapes():
    raw = '{"content":"line1\\nline2\\t\\"q\\"'
    assert extract_partial_string_field(raw, "content") == 'line1\nline2\t"q"'


def test_extract_stops_cleanly_on_incomplete_trailing_escape():
    # a lone trailing backslash (an escape whose second char hasn't streamed yet)
    assert extract_partial_string_field('{"content":"tail\\', "content") == "tail"


def test_extract_none_until_field_starts():
    assert extract_partial_string_field('{"path":"a.js"', "content") is None


def test_require_complete_waits_for_closing_quote():
    # mid-value → None when require_complete (don't show a half-typed filename)
    assert extract_partial_string_field('{"path":"styles', "path", require_complete=True) is None
    # closed → the whole value
    assert (
        extract_partial_string_field('{"path":"styles.css"', "path", require_complete=True)
        == "styles.css"
    )


# ---- the store's ephemeral broadcast ----------------------------------------


async def test_ephemeral_is_live_only_and_not_persisted():
    store = SqliteEventStore(":memory:")
    received: list[dict] = []

    async def listen():
        async for frame in await store.subscribe_ephemeral("c1"):
            received.append(frame)

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.02)  # let the subscriber register
    store.publish_ephemeral("c1", {"type": "file_stream", "delta": "x"})
    store.publish_ephemeral("c1", {"type": "file_stream", "delta": "y"})
    await asyncio.sleep(0.02)
    task.cancel()

    assert [f["delta"] for f in received] == ["x", "y"]
    # nothing was persisted to the event log
    assert await store.get_events("c1") == []


async def test_publish_with_no_listener_is_dropped_not_raised():
    store = SqliteEventStore(":memory:")
    store.publish_ephemeral("nobody", {"delta": "z"})  # must not raise


# ---- the engine hook end-to-end (synthetic stream) --------------------------


def _chunks_for(path: str, content: str, *, frag: int = 5):
    """Split a file_write tool call's JSON arguments into StreamChunk fragments,
    the way an OpenAI-compatible provider streams `function.arguments` deltas."""
    args = json.dumps({"path": path, "content": content})
    return [
        StreamChunk(tool_name="file_write", tool_index=0, tool_args_delta=args[i : i + frag])
        for i in range(0, len(args), frag)
    ]


async def test_hook_publishes_growing_frames_that_reconstruct_the_file():
    store = SqliteEventStore(":memory:")
    cid = "build1"
    loop, _ = build_loop(ScriptedAgent([finish_step()]), store=store, conversation_id=cid)
    loop.stream_sink = lambda frame, _c=cid: store.publish_ephemeral(_c, frame)

    frames: list[dict] = []

    async def listen():
        async for f in await store.subscribe_ephemeral(cid):
            frames.append(f)

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.02)

    content = "/* header */\n" + "body { color: red; }\n" * 12  # > flush threshold
    hook = loop._build_stream_hook()
    for ch in _chunks_for("styles.css", content):
        await hook(ch)
    await asyncio.sleep(0.02)
    task.cancel()

    assert frames, "expected at least one file_stream frame"
    assert all(f["type"] == "file_stream" for f in frames)
    # the path is whole on every frame — never a half-typed 'styles'
    assert {f["path"] for f in frames} == {"styles.css"}
    # the deltas concatenate back to (a prefix of) the file — coalescing may leave
    # a sub-threshold tail unsent, which the authoritative ActionEvent supersedes.
    rebuilt = "".join(f["delta"] for f in frames)
    assert content.startswith(rebuilt)
    assert len(rebuilt) >= len(content) - loop._STREAM_FLUSH_CHARS


async def test_hook_ignores_non_write_tools():
    store = SqliteEventStore(":memory:")
    cid = "build2"
    loop, _ = build_loop(ScriptedAgent([finish_step()]), store=store, conversation_id=cid)
    published: list[dict] = []
    loop.stream_sink = published.append

    hook = loop._build_stream_hook()
    args = json.dumps({"command": "ls -la"})
    for i in range(0, len(args), 4):
        await hook(StreamChunk(tool_name="shell", tool_index=0, tool_args_delta=args[i : i + 4]))

    assert published == []  # a `shell` call carries no file body → nothing streamed


def test_no_sink_means_no_hook():
    store = SqliteEventStore(":memory:")
    loop, _ = build_loop(ScriptedAgent([finish_step()]), store=store, conversation_id="b3")
    # stream_sink defaults to None → the hook is None → agent.step won't stream
    assert loop.stream_sink is None
    assert loop._build_stream_hook() is None
