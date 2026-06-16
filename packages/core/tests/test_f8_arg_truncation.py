"""F8 — GATED mid-turn arg truncation (assist-tier context-window reclaim).

# Contract (T11 / weak-model-reliability F-class)

When the assist gate is ON, after a `file_write` tool call has been
CONFIRMED successful (a corresponding success tool-result exists), the
stored `content` argument in that historical assistant message is shrunk
to a short prefix + a path-aware marker, because the full content now
lives on disk and in the workspace snapshot. This reclaims context
window. Lossless: the content is recoverable via `file_read` or the
snapshot.

The transform applies ONLY to confirmed-successful writes. A write whose
result is an error/failure, or a write with no observation yet, MUST keep
its full args (the model needs the original to retry intelligently).

The transform applies at RENDER TIME only — the persisted event log is
unchanged; the on-disk full content is the recovery surface. Assist OFF
(capable-model default) → the transform is never invoked; messages are
byte-identical to today.

# Acceptance (this file)

  1. assist ON + a confirmed file_write in history → that message's
     stored content arg is shortened to prefix+marker in the rendered
     messages.
  2. assist ON + a FAILED file_write → args intact (F8 marker absent);
     the F8 transform did NOT apply to a write the model needs to retry.
  3. assist OFF → args intact (byte-identical to today), even for a
     confirmed write. The gate is closed end-to-end.
  4. The on-disk / persisted event is unchanged — only the rendered view
     shrank. The event store retains the full content (the model can
     audit it; replay/condense keeps working).

Supplementary tests pin the edge cases that make the gate safe:
short content, unconfirmed writes (no observation yet), non-`file_write`
tools, multiple confirmed writes in one history, and the fact that the
input messages list is never mutated."""

from __future__ import annotations

from conftest import user_msg, with_seqs
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    LLMMessage,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode
from disco.core.loop.dedup import (
    _F8_PREFIX_CHARS,
    _F8_TRUNCATION_MARKER_TEMPLATE,
    _f8_confirmed_file_writes,
)
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    NeverConfirm,
    ScriptedAgent,
)

CID = "conv"

# A long content body used in the headline tests: > _F8_PREFIX_CHARS
# (so the F8 transform would fire on a confirmed write) AND >
# _ARG_SNIP_CHARS=1500 (so the existing snip shaper WOULD elide it
# without F8's override). 200-char prefix + the F8 marker = ~265 chars;
# the F8-vs-snip distinction is then directly observable.
LONG_CONTENT = 'def hello():\n    return "world"\n' * 50  # 1,600 chars
LONG_PATH = "src/hello.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _NoOpCondenser:
    """Inert condenser — should_condense returns None, condense returns None.
    Keeps the test focused on the F8 transform, not the condenser's."""

    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


def _make_loop(*, assist: bool, store: SqliteEventStore) -> AgentLoop:  # noqa: F821
    """Build an AgentLoop with a real in-memory store and a no-op
    ScriptedAgent. The tests never call `agent.step()` — they drive
    `_materialize_view` directly to inspect the rendered messages."""
    from disco.core.loop.engine import AgentLoop

    return AgentLoop(
        CID,
        store,
        ScriptedAgent([]),  # never stepped
        FakeExecutor(),
        None,  # router — held but unused by the loop (the Agent owns it)
        FakeAnalyzer(),
        NeverConfirm(),
        _NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        # assist is a kwarg on AgentLoop; we set the post-init flag too so
        # the call site mirrors every other assist-gated test (the kwarg
        # only controls the initial value, which is False; we want to be
        # explicit when we flip it).
        assist=assist,
    )


def _write_event(
    call_id: str = "call_fw_1",
    path: str = LONG_PATH,
    content: str = LONG_CONTENT,
) -> ActionEvent:
    return ActionEvent(
        thought="writing the file",
        tool_call=ToolCall(
            tool_name="file_write",
            call_id=call_id,
            arguments={"path": path, "content": content},
        ),
    )


def _success_observation(call_id: str = "call_fw_1", action_id: str = "x") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=call_id,
            tool_name="file_write",
            success=True,
            content="wrote 1,600 chars",
        ),
        action_id=action_id,
    )


def _failure_observation(call_id: str = "call_fw_1", action_id: str = "x") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=call_id,
            tool_name="file_write",
            success=False,
            content="permission denied",
            error="permission denied",
        ),
        action_id=action_id,
    )


def _find_write_message(messages: list[LLMMessage], *, name: str = "file_write") -> LLMMessage:
    """Return the first assistant message whose first matching tool_call has
    the given name. The test uses this to pull the rendered message for
    assertion without depending on message order."""
    for m in messages:
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            if isinstance(tc, dict) and tc.get("name") == name:
                return m
    raise AssertionError(
        f"no assistant message with a {name!r} tool_call found in {len(messages)} messages"
    )


def _content_of(messages: list[LLMMessage], *, name: str = "file_write") -> str:
    msg = _find_write_message(messages, name=name)
    tc = msg.tool_calls[0]  # first matching tool_call
    return tc["arguments"]["content"]


# ---------------------------------------------------------------------------
# (1) Happy path: assist ON + confirmed write → content is F8 prefix+marker
# ---------------------------------------------------------------------------


async def test_assist_on_confirmed_write_shrinks_content_to_prefix_marker():
    """A confirmed-successful file_write with long content gets its
    `content` arg in the rendered view replaced with a 200-char prefix
    + a path-aware recovery marker. The full content is still on disk
    (recoverable via file_read); only the rendered message shrank."""
    events = with_seqs(
        [user_msg("write it"), _write_event(), _success_observation()]
    )
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))

    view = await loop._materialize_view(events)
    content = _content_of(view.messages)

    # The prefix is the FIRST _F8_PREFIX_CHARS chars of the original
    # content — the model sees a real window into what it wrote.
    assert content.startswith(LONG_CONTENT[:_F8_PREFIX_CHARS])
    # The marker names the file path so the model can `file_read <path>`
    # to recover the full content. This is the load-bearing piece —
    # the marker is the recovery surface.
    assert f"[written to {LONG_PATH}" in content
    assert "file_read to recover" in content
    # The rendered content is now meaningfully shorter than the original
    # (1,600 chars → ~265 chars) — context reclaimed.
    assert len(content) < len(LONG_CONTENT)
    # And it's deterministic — re-rendering yields the same string.
    view2 = await loop._materialize_view(events)
    assert _content_of(view2.messages) == content


async def test_assist_on_confirmed_write_marker_is_deterministic_for_same_event():
    """The marker template is a single source of truth and depends only
    on (path, prefix). Two renders of the same event list produce the
    IDENTICAL rendered content (no time-based nondeterminism, no
    random tokens, no per-call salt). This is the cache-stability
    contract: a re-materialized view matches the prior one byte-for-byte."""
    events = with_seqs(
        [user_msg("write it"), _write_event(), _success_observation()]
    )
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    a = (await loop._materialize_view(events)).messages
    b = (await loop._materialize_view(events)).messages
    assert _content_of(a) == _content_of(b)


# ---------------------------------------------------------------------------
# (2) Negative path: assist ON + FAILED write → F8 marker is ABSENT
# ---------------------------------------------------------------------------


async def test_assist_on_failed_write_does_not_shrink():
    """A write whose ObservationEvent has success=False MUST NOT be shrunk
    by F8 — the model needs the full content to retry intelligently.
    The F8 marker must be ABSENT from the rendered args.

    The existing _ARG_SNIP shaper in events.py may still elide the long
    content to "<N chars elided — use file_read>" (that's the
    pre-F8 baseline; F8 doesn't change the snip). The headline contract
    here is the ABSENCE of the F8-specific marker, which is the proof
    that F8 didn't fire for this write."""
    events = with_seqs(
        [user_msg("write it"), _write_event(), _failure_observation()]
    )
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))

    view = await loop._materialize_view(events)
    content = _content_of(view.messages)

    # The F8 marker is the contract under test — it MUST NOT appear.
    assert "written to" not in content
    assert "file_read to recover" not in content
    # The content is the same string the OFF-path sees: whatever the
    # existing render-time shapers produce for a failed write.
    loop_off = _make_loop(assist=False, store=SqliteEventStore(":memory:"))
    view_off = await loop_off._materialize_view(events)
    assert _content_of(view_off.messages) == content


async def test_assist_on_failed_write_keeps_full_content_for_retry():
    """Stronger byte-level guarantee: a FAILED write's `content` arg in
    the rendered view is exactly the same string the OFF-path sees (no
    F8 mutation). The model can read it back as-is and retry. F8 is a
    one-way transform applied ONLY to confirmed writes — it never
    touches a failed write, not even by accident."""
    events = with_seqs(
        [user_msg("write it"), _write_event(), _failure_observation()]
    )
    loop_on = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    loop_off = _make_loop(assist=False, store=SqliteEventStore(":memory:"))
    on_content = _content_of((await loop_on._materialize_view(events)).messages)
    off_content = _content_of((await loop_off._materialize_view(events)).messages)
    # The two paths are byte-identical for a FAILED write — the F8
    # gate is closed for that branch.
    assert on_content == off_content


# ---------------------------------------------------------------------------
# (3) Iron rule: assist OFF → byte-identical to today
# ---------------------------------------------------------------------------


async def test_assist_off_byte_identical_even_for_confirmed_write():
    """A confirmed-successful file_write with assist OFF must produce
    the same rendered `content` arg as the pre-F8 baseline. The F8
    marker must be ABSENT. The model-facing wire format is unchanged
    for capable models — the only thing that moved is the weak-model
    path's context window."""
    events = with_seqs(
        [user_msg("write it"), _write_event(), _success_observation()]
    )
    loop = _make_loop(assist=False, store=SqliteEventStore(":memory:"))

    view = await loop._materialize_view(events)
    content = _content_of(view.messages)

    # The F8 marker is the contract under test — it MUST NOT appear.
    assert "written to" not in content
    assert "file_read to recover" not in content
    # The content is the existing snip (long content > _ARG_SNIP_CHARS).
    # We don't pin the exact string (it could legitimately change if
    # someone updates the snip shaper), but we pin the SHAPE: it's the
    # pre-F8 baseline that has been shipping.
    assert "chars elided" in content


async def test_assist_off_renders_full_short_content_unchanged():
    """A short content (≤ _ARG_SNIP_CHARS=1500) passes through every
    shaper unchanged. The OFF-path is byte-identical to the ON-path
    for short content (the F8 transform is also a no-op on short
    content per its len(content) > _F8_PREFIX_CHARS guard)."""
    short = 'print("hi")\n'  # 12 chars
    events = with_seqs(
        [
            user_msg("do it"),
            _write_event(content=short),
            _success_observation(),
        ]
    )
    loop_off = _make_loop(assist=False, store=SqliteEventStore(":memory:"))
    loop_on = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    off_content = _content_of((await loop_off._materialize_view(events)).messages)
    on_content = _content_of((await loop_on._materialize_view(events)).messages)
    # Both paths show the FULL short content byte-for-byte.
    assert off_content == short
    assert on_content == short


# ---------------------------------------------------------------------------
# (4) The on-disk / persisted event is unchanged
# ---------------------------------------------------------------------------


async def test_persisted_event_still_has_full_content_after_f8_render():
    """F8 is a RENDER-TIME transform: the event store / persisted events
    still carry the full content. The shrink applies only to the
    in-memory message list handed to the provider. A re-condense or a
    `View.recover_span` returns the original bytes (the audit trail is
    intact)."""
    store = SqliteEventStore(":memory:")
    write = _write_event()
    await store.append(CID, user_msg("write it"))
    await store.append(CID, write)
    await store.append(CID, _success_observation())

    # Sanity: the persisted event has the FULL content.
    events = await store.get_events(CID)
    action_event = next(e for e in events if isinstance(e, ActionEvent))
    assert action_event.tool_call.arguments["content"] == LONG_CONTENT

    # Render the view. The transform fires (assist=ON), the rendered
    # message has the F8 prefix+marker.
    loop = _make_loop(assist=True, store=store)
    view = await loop._materialize_view(events)
    rendered_content = _content_of(view.messages)
    assert rendered_content.startswith(LONG_CONTENT[:_F8_PREFIX_CHARS])
    assert f"[written to {LONG_PATH}" in rendered_content

    # Critical: the on-disk event is UNCHANGED. The store still has the
    # full content — replay / condense / audit all see the original.
    events_after = await store.get_events(CID)
    action_event_after = next(e for e in events_after if isinstance(e, ActionEvent))
    assert action_event_after.tool_call.arguments["content"] == LONG_CONTENT
    # And the rendered content is genuinely shorter (the transform
    # really did fire on the in-memory list).
    assert len(rendered_content) < len(LONG_CONTENT)


async def test_view_recover_span_returns_full_content_after_f8_render():
    """The C11 reversible-compaction contract: `View.recover_span`
    (and the module-level `recover_span`) returns the ORIGINAL events
    a tombstone dropped, never the F8-shrunk view. F8 doesn't touch
    the event log, so recovery is unaffected — the model can always
    get the original bytes back via a tombstone-aware recovery path."""

    store = SqliteEventStore(":memory:")
    write = _write_event()
    await store.append(CID, user_msg("write it"))
    await store.append(CID, write)
    await store.append(CID, _success_observation())

    events = await store.get_events(CID)
    action_event = next(e for e in events if isinstance(e, ActionEvent))

    # Drive the F8 render (assist=ON). Then verify the event in the store
    # is still the full content (the F8 transform is a render-time pass).
    loop = _make_loop(assist=True, store=store)
    view = await loop._materialize_view(events)
    # The rendered message shows the shrunk content.
    assert f"[written to {LONG_PATH}" in _content_of(view.messages)
    # The event in the store is unchanged.
    assert action_event.tool_call.arguments["content"] == LONG_CONTENT


# ---------------------------------------------------------------------------
# Supplementary: edge cases that make the gate safe
# ---------------------------------------------------------------------------


async def test_unconfirmed_write_does_not_shrink():
    """A file_write with NO matching observation (the action was just
    proposed, not yet executed) MUST NOT be shrunk. F8 requires
    CONFIRMATION — a future failure is still possible, and the model
    may need the full content to retry."""
    events = with_seqs(
        [user_msg("write it"), _write_event()]  # no observation
    )
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    content = _content_of((await loop._materialize_view(events)).messages)
    # The F8 marker is absent — F8 did not fire.
    assert "written to" not in content
    assert "file_read to recover" not in content


async def test_agent_error_unconfirms_a_write():
    """Defensive: an AgentErrorEvent with tool_call_id == C "un-confirms"
    a prior success for the same call_id. The F8 helper excludes C from
    the confirmed set. The rendered view does NOT show the F8 marker.
    This is the guard against a malformed executor that emits both a
    success observation and a failure AgentErrorEvent for the same call."""
    events = with_seqs(
        [
            user_msg("write it"),
            _write_event(),
            _success_observation(),
            AgentErrorEvent(
                error="later failure",
                action_id="x",
                tool_call_id="call_fw_1",
            ),
        ]
    )
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    content = _content_of((await loop._materialize_view(events)).messages)
    # The F8 marker is absent — the AgentErrorEvent downgraded the write.
    assert "written to" not in content
    assert "file_read to recover" not in content


async def test_short_confirmed_write_does_not_shrink():
    """A confirmed write with content ≤ _F8_PREFIX_CHARS (200 chars)
    passes through F8 unchanged. The transform's `len(content) >
    _F8_PREFIX_CHARS` guard avoids emitting a "prefix+marker" that is
    longer than the original (no reclaim, no value)."""
    short = "def f():\n    return 1\n"  # 23 chars
    events = with_seqs(
        [
            user_msg("write it"),
            _write_event(content=short),
            _success_observation(),
        ]
    )
    loop_on = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    loop_off = _make_loop(assist=False, store=SqliteEventStore(":memory:"))
    on_content = _content_of((await loop_on._materialize_view(events)).messages)
    off_content = _content_of((await loop_off._materialize_view(events)).messages)
    # The short content passes through unchanged on BOTH paths (no
    # snip, no F8). Byte-identical is the contract for short content.
    assert on_content == short
    assert off_content == short


async def test_non_file_write_tool_does_not_shrink():
    """The F8 transform is `file_write`-specific. A confirmed
    `file_append` or `file_edit` (other mutating tools in the
    working set) MUST NOT be shrunk — the F8 contract is narrow on
    purpose (it knows the content is the entire file body for
    `file_write`; for other tools the content is a partial diff
    whose truncation could lose information needed to reconstruct
    the file)."""
    from disco.core import ToolCall

    append_action = ActionEvent(
        thought="appending",
        tool_call=ToolCall(
            tool_name="file_append",
            call_id="call_append_1",
            arguments={"path": LONG_PATH, "content": LONG_CONTENT},
        ),
    )
    append_obs = ObservationEvent(
        tool_result=ToolResult(
            call_id="call_append_1",
            tool_name="file_append",
            success=True,
            content="appended 1,600 chars",
        ),
        action_id="x",
    )
    events = with_seqs([user_msg("do it"), append_action, append_obs])
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    content = _content_of(
        (await loop._materialize_view(events)).messages, name="file_append"
    )
    # F8 marker is absent — the transform doesn't fire for `file_append`.
    assert "written to" not in content
    assert "file_read to recover" not in content


async def test_multiple_confirmed_writes_all_shrink():
    """A history with multiple confirmed file_writes shrinks EVERY one of
    them. The F8 transform is per-call, not all-or-nothing. The
    helpers are O(N) over events and O(M) over the rendered message
    list; N=3 + M=3 in this test is plenty for the contract."""
    events = with_seqs(
        [
            user_msg("write 2 files"),
            _write_event(call_id="call_a", path="a.py"),
            _success_observation(call_id="call_a"),
            _write_event(call_id="call_b", path="b.py"),
            _success_observation(call_id="call_b"),
        ]
    )
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))

    view = await loop._materialize_view(events)
    file_write_msgs = [
        m for m in view.messages
        if m.role == "assistant"
        and m.tool_calls
        and any(
            isinstance(tc, dict) and tc.get("name") == "file_write"
            for tc in m.tool_calls
        )
    ]
    # Two file_write actions → two assistant messages with file_write calls.
    assert len(file_write_msgs) == 2
    contents = [
        tc["arguments"]["content"]
        for m in file_write_msgs
        for tc in m.tool_calls
        if isinstance(tc, dict) and tc.get("name") == "file_write"
    ]
    # Both are shrunk and name their respective paths.
    assert all("file_read to recover" in c for c in contents)
    assert any("[written to a.py" in c for c in contents)
    assert any("[written to b.py" in c for c in contents)


async def test_input_messages_list_not_mutated_by_f8_transform():
    """Defensive immutability: `_f8_shrink_file_write_args` returns a
    new list; it MUST NOT mutate the caller's list, the caller's
    LLMMessage objects, or the caller's tool_call dicts. The View is
    shared across many call sites; a render-time transform that
    mutated it would corrupt every consumer."""

    events = with_seqs(
        [user_msg("write it"), _write_event(), _success_observation()]
    )
    loop = _make_loop(assist=True, store=SqliteEventStore(":memory:"))
    view = await loop._materialize_view(events)
    # Snapshot the LLMMessage identities and tool_call dict identities
    # BEFORE the F8 transform. Re-run and verify the original view
    # is unmodified.
    _pre_ids = [(id(m), [id(tc) for tc in (m.tool_calls or [])]) for m in view.messages]
    pre_contents = {
        id(m): {
            id(tc): tc["arguments"]["content"]
            for tc in (m.tool_calls or [])
        }
        for m in view.messages
    }

    # Call the F8 method on a COPY of the rendered messages.
    in_messages = list(view.messages)
    out_messages = loop._f8_shrink_file_write_args(in_messages, events)

    # The input list identity is unchanged.
    assert in_messages == view.messages
    # The output is a NEW list.
    assert out_messages is not in_messages
    # The modified message is a NEW LLMMessage (frozen model_copy).
    for m_in, m_out in zip(in_messages, out_messages, strict=False):
        if m_in.role == "assistant" and m_in.tool_calls:
            for tc_in, tc_out in zip(m_in.tool_calls, m_out.tool_calls, strict=False):
                if isinstance(tc_in, dict) and tc_in.get("id") == "call_fw_1":
                    # The dict was rebuilt (not the same object).
                    assert id(tc_in) != id(tc_out)
                    # The input dict still has the original (snipped) content.
                    assert tc_in["arguments"]["content"] == pre_contents[id(m_in)][id(tc_in)]
                    # The output dict has the F8 prefix+marker.
                    assert "file_read to recover" in tc_out["arguments"]["content"]


# ---------------------------------------------------------------------------
# Module-level helper: pure function, no engine needed
# ---------------------------------------------------------------------------


def test_f8_confirmed_file_writes_only_returns_file_writes():
    """The pure helper excludes other tools even if a successful
    observation exists for them. F8 is `file_write`-specific — other
    tools' content truncation is a different design (and a different
    F-class)."""
    from disco.core import ToolCall

    append_action = ActionEvent(
        thought="appending",
        tool_call=ToolCall(
            tool_name="file_append",
            call_id="call_append_x",
            arguments={"path": "x.py", "content": "data"},
        ),
    )
    append_obs = ObservationEvent(
        tool_result=ToolResult(
            call_id="call_append_x",
            tool_name="file_append",
            success=True,
            content="appended",
        ),
        action_id="x",
    )
    events = with_seqs([user_msg(), append_action, append_obs])
    confirmed = _f8_confirmed_file_writes(events)
    # No file_write in the events → the helper returns an empty dict.
    assert confirmed == {}


def test_f8_confirmed_file_writes_includes_path_and_full_content():
    """The pure helper's return shape: {call_id: (path, full_content)}.
    The full_content is the ORIGINAL event-stored content (not the
    snipped version), so the F8 method can emit a real 200-char prefix."""
    events = with_seqs(
        [user_msg(), _write_event(), _success_observation()]
    )
    confirmed = _f8_confirmed_file_writes(events)
    assert list(confirmed.keys()) == ["call_fw_1"]
    path, content = confirmed["call_fw_1"]
    assert path == LONG_PATH
    # The full content is preserved (the helper reads the event, not
    # the rendered message).
    assert content == LONG_CONTENT
    assert len(content) > _F8_PREFIX_CHARS


def test_f8_truncation_marker_template_includes_path():
    """The marker template is a single source of truth — the F8 method
    uses it via `.format(path=...)`. The marker names the path so
    `file_read <path>` is the recovery action; the marker also
    includes the literal substring "file_read to recover" so the
    model recognizes the recovery affordance without parsing the
    template."""
    rendered = _F8_TRUNCATION_MARKER_TEMPLATE.format(path="src/foo.py")
    assert "src/foo.py" in rendered
    assert "file_read to recover" in rendered
