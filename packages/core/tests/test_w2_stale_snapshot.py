"""W2 — stale-aware snapshot + read-dedup-for-all.

# Contract

``workspace_snapshot_message`` is the always-fresh per-turn disk-content
block injected after condensation. Before W2 it re-dumped EVERY tracked
file in full on every turn, which caused a large file (e.g. windows.js,
8047 B) to produce repeated "too large — call file_read" markers → the
model re-read the same file on every turn (20× measured in the golden trace).

The evolved contract changes four things:

1. **Stateless current bodies.** Every provider request carries the current
   bounded body. A tracker may detect external change, but prior-turn delivery
   never becomes a pointer because it is absent from the new request.

2. **Stale notice (silent when nothing changed).** ``file_state_notice``
   returns None for an empty stale list; otherwise a NAMED block listing
   only the externally-changed paths. External = disk SHA ≠ tracker SHA.

3. **Windowed-view directive for oversize files.** The old "do NOT call
   file_write" oversize marker is replaced with a windowed-read hint
   pointing at ``file_read(path, offset=L, limit=M)`` + file_edit. The
   "do NOT call file_write" wording is gone.

4. **Receipt-aware read collapse.** Exact revision/range coverage, never path
   recency, determines whether a body is redundant. Legacy reads remain.

# Acceptance (this file)

1. Unchanged file across turns → each stateless request contains the body.
2. Externally-changed file → named in the stale notice (``file_state_notice``
   + ``stale_paths``).
3. Oversize-file branch → windowed directive present; the old "do NOT call
   file_write" wording is ABSENT.
4. ``file_state_notice([])`` → None (silent fast-path).
5. Legacy reads without receipts are not guessed equivalent.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from disco.core import (
    ActionEvent,
    LLMMessage,
    ObservationEvent,
    ToolCall,
    ToolResult,
    WorkspaceMutationEvent,
    agent_view_consistent_events,
)
from disco.core.effects import (
    ActionProfile,
    EffectCapability,
    MutationReceipt,
    ResourceKey,
    ResourceRevision,
)
from disco.core.loop.dedup import collapse_superseded_reads
from disco.core.loop.file_state import (
    _STALE_NOTICE_SENTINEL,
    FileStateTracker,
    file_state_notice,
    reconcile_mutation_receipts,
)
from disco.core.loop.view_render import (
    _WS_PER_FILE_CHARS,
    ViewBuilder,
    workspace_snapshot_message,
)
from event_fakes import with_seqs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """Minimal sandbox double that returns bytes for known paths."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


def _write_event(path: str, content: str = "x", call_id: str = "c1") -> ActionEvent:
    return ActionEvent(
        thought="writing",
        tool_call=ToolCall(
            tool_name="file_write",
            call_id=call_id,
            arguments={"path": path, "content": content},
        ),
    )


def _read_event(path: str, call_id: str = "r1") -> ActionEvent:
    return ActionEvent(
        thought="reading",
        tool_call=ToolCall(
            tool_name="file_read",
            call_id=call_id,
            arguments={"path": path},
        ),
    )


def _read_obs(action: ActionEvent, content: str = "the content") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="file_read",
            success=True,
            content=content,
        ),
        action_id=action.id,
    )


def _snapshot(
    files: dict[str, bytes],
    events: list[Any],
    *,
    tracker: FileStateTracker | None = None,
    stale: frozenset[str] | None = None,
) -> str | None:
    """Run workspace_snapshot_message synchronously; return content or None."""
    sbx = _FakeSandbox(files)
    msg = asyncio.run(
        workspace_snapshot_message(
            sbx,
            with_seqs(events),
            tracker=tracker,
            stale=stale,
        )
    )
    if msg is None:
        return None
    assert isinstance(msg, LLMMessage)
    return msg.content


# ---------------------------------------------------------------------------
# (4) file_state_notice([]) → None  (pure-function fast-path, no I/O)
# ---------------------------------------------------------------------------


def test_file_state_notice_empty_returns_none():
    """``file_state_notice`` with an empty stale list must return None.
    This is the silent fast-path — the most common case (no external
    changes) must produce zero output, not an empty-heading block."""
    assert file_state_notice([]) is None


def test_file_state_notice_nonempty_returns_message():
    """A non-empty stale list produces a named user-role message listing
    each stale path. The sentinel heading is present; each path is named."""
    stale = ["src/foo.py", "src/bar.ts"]
    msg = file_state_notice(stale)
    assert msg is not None
    assert isinstance(msg, LLMMessage)
    assert msg.role == "user"
    assert _STALE_NOTICE_SENTINEL in msg.content
    assert "src/foo.py" in msg.content
    assert "src/bar.ts" in msg.content
    assert "modified externally" not in msg.content
    assert "not by your tool calls" not in msg.content


def test_file_state_notice_single_path():
    """Single stale path → notice names it exactly once."""
    msg = file_state_notice(["only/one.py"])
    assert msg is not None
    assert "only/one.py" in msg.content


# ---------------------------------------------------------------------------
# (1) Every stateless request receives the current bounded body
# ---------------------------------------------------------------------------


def test_unchanged_file_is_present_in_each_stateless_request():
    """A prior provider call is not context for the current provider call."""
    tracker = FileStateTracker()
    events = [_write_event("app.js", "const x = 1;")]
    content = b"const x = 1;"

    # First call: file never in tracker → show full body
    text1 = _snapshot({"app.js": content}, events, tracker=tracker, stale=frozenset())
    assert text1 is not None
    assert "BEGIN FILE app.js" in text1, "first call must show full body"
    assert "const x = 1;" in text1

    # Second call: tracker knows the SHA, but the provider request is stateless.
    text2 = _snapshot({"app.js": content}, events, tracker=tracker, stale=frozenset())
    assert text2 is not None
    assert "BEGIN FILE app.js" in text2
    assert "const x = 1;" in text2
    assert "current, shown earlier" not in text2
    assert text2 == text1


def test_tracker_does_not_create_a_prior_turn_pointer():
    tracker = FileStateTracker()
    events = [_write_event("hello.py", "x=1")]
    _snapshot({"hello.py": b"x=1"}, events, tracker=tracker, stale=frozenset())
    # Second call still carries the body; "shown earlier" is never sufficient.
    text = _snapshot({"hello.py": b"x=1"}, events, tracker=tracker, stale=frozenset())
    assert text is not None
    assert "BEGIN FILE hello.py" in text
    assert "current, shown earlier" not in text


def test_no_tracker_always_shows_full_body():
    """Without a tracker (backward-compat mode / existing tests), every file
    is shown in full on every call — no pointers emitted."""
    events = [_write_event("x.py", "y=2")]
    content = b"y=2"
    # Two calls, no tracker
    t1 = _snapshot({"x.py": content}, events)
    t2 = _snapshot({"x.py": content}, events)
    assert t1 is not None and "BEGIN FILE x.py" in t1
    assert t2 is not None and "BEGIN FILE x.py" in t2


# ---------------------------------------------------------------------------
# (2) Externally-changed file → named in stale notice
# ---------------------------------------------------------------------------


def test_stale_paths_detects_external_change():
    """After the snapshot shows a file, if its disk content changes (external
    edit), ``FileStateTracker.stale_paths`` returns that path in the stale
    list.
    """
    tracker = FileStateTracker()
    original = b"original content"
    changed = b"NEW content from external editor"

    # Simulate: snapshot runs once and records the sha for "readme.md"
    tracker.record_agent_io("readme.md", original, seq=1)

    # External change: disk now has different bytes
    sbx = _FakeSandbox({"readme.md": changed})
    stale = asyncio.run(tracker.stale_paths(sbx, ["readme.md"]))

    assert "readme.md" in stale, "stale_paths must flag readme.md because its disk SHA changed"


def test_stale_paths_empty_when_unchanged():
    """No external change → stale_paths returns an empty list (silent path)."""
    tracker = FileStateTracker()
    content = b"stable content"
    tracker.record_agent_io("stable.py", content, seq=2)
    sbx = _FakeSandbox({"stable.py": content})
    stale = asyncio.run(tracker.stale_paths(sbx, ["stable.py"]))
    assert stale == [], "stale_paths must be empty when disk SHA matches tracker SHA"


def test_stale_paths_skips_never_shown():
    """Paths not yet in the tracker (never shown) are NOT flagged as stale.
    They are 'never-shown'; the snapshot handles them via its full-body branch.
    """
    tracker = FileStateTracker()  # empty tracker
    sbx = _FakeSandbox({"newfile.py": b"brand new"})
    stale = asyncio.run(tracker.stale_paths(sbx, ["newfile.py"]))
    assert stale == [], "never-shown paths must not appear in stale_paths"


def test_externally_changed_file_shown_full_in_snapshot():
    """A file in the ``stale`` set is shown with a full body (not a pointer),
    even if the tracker has seen it before. After rendering, the tracker is
    updated to the new disk SHA.
    """
    tracker = FileStateTracker()
    original = b"old body"
    changed = b"new body after external edit"

    # Turn 1: file shown in full, tracker updated
    events = [_write_event("work.py", "old body")]
    _snapshot({"work.py": original}, events, tracker=tracker, stale=frozenset())
    assert tracker.is_known("work.py")

    # External change → disk SHA ≠ tracker SHA → pass it as stale
    text = _snapshot(
        {"work.py": changed},
        events,
        tracker=tracker,
        stale=frozenset({"work.py"}),
    )
    assert text is not None
    assert "BEGIN FILE work.py" in text, "stale file must be shown in full (not a pointer)"
    assert "new body after external edit" in text

    # After rendering, tracker SHA updated to the new disk SHA
    new_sha = hashlib.sha256(changed).hexdigest()
    assert tracker.get_sha("work.py") == new_sha, (
        "tracker must update to new disk SHA after showing stale file in full"
    )


# ---------------------------------------------------------------------------
# (3) Oversize file → windowed directive, no "do NOT call file_write"
# ---------------------------------------------------------------------------


def test_oversize_file_windowed_directive_no_do_not_write():
    """The W2 oversize marker uses a windowed-read directive and does NOT
    contain the old 'do NOT call file_write' wording.

    Before W2: the marker said 'do NOT call file_write with regenerated
    content' — which FORBADE writes and pressured the model into a
    file_read loop. After W2: the marker says 'file_read(path, offset=L,
    limit=M)' and 'file_edit', without forbidding writes.
    """
    large_content = b"x" * (_WS_PER_FILE_CHARS + 500)
    events = [_write_event("big.js", "x" * (_WS_PER_FILE_CHARS + 500))]
    # No tracker → always shows full (truncated) body, no pointer
    text = _snapshot({"big.js": large_content}, events)
    assert text is not None

    # Old wording must be gone
    assert "do NOT call file_write" not in text, (
        "the 'do NOT call file_write' instruction must be removed (W2 spec §3)"
    )
    # New windowed directive must be present
    assert "file_read" in text, "windowed directive must mention file_read"
    assert "offset" in text, "windowed directive must mention offset parameter"
    assert "file_edit" in text, "windowed directive must mention file_edit"


def test_oversize_file_still_shows_head_and_tail():
    """Oversize files are still head/tail truncated — only the marker changes."""
    large_body = "A" * 3000 + "B" * 4000  # > 6000 char cap
    large_bytes = large_body.encode()
    events = [_write_event("big.py", large_body)]
    text = _snapshot({"big.py": large_bytes}, events)
    assert text is not None
    # Head content present
    assert "AAAA" in text
    # Tail content present
    assert "BBBB" in text
    # Truncation notice present
    assert "more chars" in text


def test_oversize_file_with_tracker_remains_truthfully_windowed():
    """A bounded head/tail window is re-derived for every stateless request."""
    tracker = FileStateTracker()
    large_bytes = b"Z" * (_WS_PER_FILE_CHARS + 200)
    events = [_write_event("windows.js", "Z" * (_WS_PER_FILE_CHARS + 200))]

    # First call → shown (truncated) + tracker updated
    t1 = _snapshot({"windows.js": large_bytes}, events, tracker=tracker, stale=frozenset())
    assert t1 is not None
    assert "BEGIN FILE windows.js" in t1  # full (truncated) block shown

    # Second call → the same explicit bounded window, never a prior-turn pointer.
    t2 = _snapshot({"windows.js": large_bytes}, events, tracker=tracker, stale=frozenset())
    assert t2 is not None
    assert "BEGIN FILE windows.js" in t2
    assert "more chars" in t2
    assert "current, shown earlier" not in t2
    assert t2 == t1


# ---------------------------------------------------------------------------
# (5) collapse_superseded_reads — render-time history body collapse
# ---------------------------------------------------------------------------


def test_collapse_keeps_legacy_reads_without_exact_receipts():
    """Path and recency alone cannot prove two historical bodies equivalent."""
    r1 = _read_event("foo.py", call_id="c1")
    r2 = _read_event("foo.py", call_id="c2")  # later read of same path
    events = with_seqs([r1, _read_obs(r1, "old content"), r2, _read_obs(r2, "new content")])

    messages: list[LLMMessage] = [
        # Simulate the rendered message stream
        LLMMessage(role="tool", content="old content", tool_call_id="c1"),
        LLMMessage(role="tool", content="new content", tool_call_id="c2"),
    ]
    result = collapse_superseded_reads(messages, events)
    assert len(result) == 2
    assert result[0].content == "old content"
    assert result[0].tool_call_id == "c1"
    assert result[1].content == "new content"
    assert result[1].tool_call_id == "c2"


def test_collapse_superseded_reads_keeps_single_read_unchanged():
    """A single file_read (no duplicates) is NOT modified — the 'latest' is
    also the 'only', so it stays in full."""
    r1 = _read_event("solo.py", call_id="c1")
    events = with_seqs([r1, _read_obs(r1, "solo content")])
    messages: list[LLMMessage] = [
        LLMMessage(role="tool", content="solo content", tool_call_id="c1"),
    ]
    result = collapse_superseded_reads(messages, events)
    assert result[0].content == "solo content"  # unchanged
    assert "superseded" not in result[0].content


def test_collapse_superseded_reads_different_paths_independent():
    """File_read results for DIFFERENT paths are independently managed.
    Reads of foo.py do not affect the reads of bar.py.
    """
    r_foo = _read_event("foo.py", call_id="c1")
    r_bar = _read_event("bar.py", call_id="c2")
    events = with_seqs(
        [r_foo, _read_obs(r_foo, "foo content"), r_bar, _read_obs(r_bar, "bar content")]
    )
    messages: list[LLMMessage] = [
        LLMMessage(role="tool", content="foo content", tool_call_id="c1"),
        LLMMessage(role="tool", content="bar content", tool_call_id="c2"),
    ]
    result = collapse_superseded_reads(messages, events)
    # Both kept (each is the only/latest for its path)
    assert result[0].content == "foo content"
    assert result[1].content == "bar content"


def test_collapse_superseded_reads_non_read_messages_untouched():
    """Non-file_read tool results and non-tool messages are NEVER modified."""
    messages: list[LLMMessage] = [
        LLMMessage(role="user", content="please do X"),
        LLMMessage(role="assistant", content="sure"),
        LLMMessage(role="tool", content="shell output", tool_call_id="shell_c1"),
    ]
    events = with_seqs([])  # no file_read events
    result = collapse_superseded_reads(messages, events)
    assert result[0].content == "please do X"
    assert result[1].content == "sure"
    assert result[2].content == "shell output"


def test_collapse_superseded_reads_returns_new_list():
    """The function returns a NEW list; the input is not mutated."""
    r1 = _read_event("f.py", call_id="c1")
    r2 = _read_event("f.py", call_id="c2")
    events = with_seqs([r1, _read_obs(r1, "old"), r2, _read_obs(r2, "new")])
    messages: list[LLMMessage] = [
        LLMMessage(role="tool", content="old", tool_call_id="c1"),
        LLMMessage(role="tool", content="new", tool_call_id="c2"),
    ]
    result = collapse_superseded_reads(messages, events)
    assert result is not messages  # new list
    assert messages[0].content == "old"  # original untouched


# ---------------------------------------------------------------------------
# FileStateTracker — pure-API contracts
# ---------------------------------------------------------------------------


def test_tracker_is_known_false_initially():
    """Fresh tracker has no known paths."""
    t = FileStateTracker()
    assert not t.is_known("anything.py")
    assert t.get_sha("anything.py") is None


def test_tracker_record_and_retrieve():
    """record_agent_io stores the SHA; get_sha and is_known reflect it."""
    t = FileStateTracker()
    content = b"hello world"
    sha = hashlib.sha256(content).hexdigest()
    t.record_agent_io("test.py", content, seq=5)
    assert t.is_known("test.py")
    assert t.get_sha("test.py") == sha


def test_tracker_record_str_content():
    """record_agent_io accepts str content (encodes as UTF-8)."""
    t = FileStateTracker()
    text = "def foo(): pass"
    t.record_agent_io("mod.py", text, seq=1)
    expected_sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    assert t.get_sha("mod.py") == expected_sha


def test_tracker_applies_each_host_mutation_revision_once():
    tracker = FileStateTracker()
    first = hashlib.sha256(b"agent write").hexdigest()
    external = hashlib.sha256(b"external edit").hexdigest()

    tracker.record_mutation_revision("app.js", first, seq=4)
    tracker.record_agent_io("app.js", b"external edit", seq=0)
    tracker.record_mutation_revision("app.js", first, seq=4)

    assert tracker.get_sha("app.js") == external


@pytest.mark.asyncio
async def test_authenticated_write_receipt_is_not_reported_as_external():
    old = b"old bytes"
    written = b"agent bytes"
    external = b"external bytes"
    path = "app.js"
    resource = ResourceKey(namespace="workspace.file", identifier=path)
    action = _write_event(path, written.decode())
    receipt = MutationReceipt(
        resource=resource,
        after=ResourceRevision(
            resource=resource,
            digest=hashlib.sha256(written).hexdigest(),
        ),
        after_size_bytes=len(written),
    )
    result = ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=action.tool_call.tool_name,
            success=True,
            content="write complete",
            action_profile=ActionProfile(
                capabilities=frozenset({EffectCapability.WORKSPACE_MUTATE})
            ),
            effect_receipts=(receipt,),
        ),
        action_id=action.id,
    )
    events = with_seqs([action, result])
    sandbox = _FakeSandbox({path: written})
    builder = ViewBuilder(SimpleNamespace(executor=SimpleNamespace(sandbox=sandbox)))
    builder._file_tracker.record_agent_io(path, old, seq=0)

    _, stale = await builder._compute_stale_paths(events)
    assert stale == []

    sandbox._files[path] = external
    _, stale = await builder._compute_stale_paths(events)
    assert stale == [path]

    # The next snapshot accepts the external bytes as its baseline. Replaying
    # the old receipt must not manufacture another warning.
    builder._file_tracker.record_agent_io(path, external, seq=0)
    _, stale = await builder._compute_stale_paths(events)
    assert stale == []


@pytest.mark.asyncio
async def test_superseded_view_receipt_updates_workspace_without_exposing_transcript():
    path = "app.js"
    old = b"old bytes"
    written = b"late authenticated write"
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
        id="intent_workspace_receipt",
    )
    first_write = _write_event(path, old.decode(), call_id="visible-write").model_copy(
        update={"agent_view_id": "view-1"}
    )
    late_write = _write_event(path, written.decode(), call_id="late-write").model_copy(
        update={"agent_view_id": "view-1"}
    )
    resource = ResourceKey(namespace="workspace.file", identifier=path)
    late_result = ObservationEvent(
        tool_result=ToolResult(
            call_id=late_write.tool_call.call_id,
            tool_name=late_write.tool_call.tool_name,
            success=True,
            content="write complete",
            action_profile=ActionProfile(
                capabilities=frozenset({EffectCapability.WORKSPACE_MUTATE})
            ),
            effect_receipts=(
                MutationReceipt(
                    resource=resource,
                    after=ResourceRevision(
                        resource=resource,
                        digest=hashlib.sha256(written).hexdigest(),
                    ),
                    after_size_bytes=len(written),
                ),
            ),
        ),
        action_id=late_write.id,
        agent_view_id="view-1",
    )
    raw_events = with_seqs(
        [
            intent,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-1",
                run_protocol_version=1,
            ),
            first_write,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-2",
                run_protocol_version=1,
            ),
            late_write,
            late_result,
        ]
    )
    visible = list(agent_view_consistent_events(raw_events))
    visible_ids = {event.id for event in visible}
    assert late_write.id not in visible_ids
    assert late_result.id not in visible_ids

    sandbox = _FakeSandbox({path: written})
    builder = ViewBuilder(SimpleNamespace(executor=SimpleNamespace(sandbox=sandbox)))
    builder._file_tracker.record_agent_io(path, old, seq=0)
    _, stale = await builder._compute_stale_paths(visible)
    assert stale == [path]

    reconcile_mutation_receipts(builder._file_tracker, raw_events)
    _, stale = await builder._compute_stale_paths(visible)
    assert stale == []


def test_tracker_empty_path_is_no_op():
    """record_agent_io with empty path does not add to tracker (defensive)."""
    t = FileStateTracker()
    t.record_agent_io("", b"content", seq=1)
    assert not t.is_known("")


def test_tracker_stale_paths_swallows_file_not_found():
    """If a tracked file is deleted from disk, stale_paths skips it
    (swallows FileNotFoundError) rather than crashing."""

    class _MissingSandbox:
        async def read_file(self, path: str) -> bytes:
            raise FileNotFoundError(path)

    t = FileStateTracker()
    t.record_agent_io("gone.py", b"was here", seq=1)
    result = asyncio.run(t.stale_paths(_MissingSandbox(), ["gone.py"]))
    assert result == [], "FileNotFoundError must be swallowed — no false stale flag"
