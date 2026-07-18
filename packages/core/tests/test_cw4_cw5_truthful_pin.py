"""CW-4 context truth + CW-5 pin survival through condensation.

CW-4 now requires typed revision/range evidence. A bare ``pinned_full_paths`` entry
cannot prove byte identity, and legacy observations without receipts remain verbatim.
The exact receipt cases live in ``test_resource_context.py``.

CW-5: for assist-OFF the pinned working-set block is built from disk and positioned
in the cacheable PREFIX AFTER the §8 condenser fires. This proves the invariant: a
condensation that forgets an OLD span does NOT erase the pin, and the pin still
covers the touched files (the condenser acts on EVENTS; the pin is rebuilt outside
the condensable region).
"""

from __future__ import annotations

import asyncio
import re

from disco.core import (
    ActionEvent,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from disco.core.events import WORKSPACE_SNAPSHOT_SENTINEL, CondensationEvent, EventSource
from disco.core.loop.dedup import collapse_superseded_reads
from disco.core.loop.view_render import ViewBuilder
from disco.core.view import CondensationRequest, NoOpCondenser, View

# REVISION 2 — no directional words allowed in pointer/recovery prose.
_DIRECTIONAL = re.compile(r"\b(?:below|above|earlier|later|following|preceding)\b", re.I)


# ---------------------------------------------------------------------------
# Fakes — minimal sandbox + loop so ViewBuilder.build runs end-to-end.
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
    """ViewBuilder collaborator surface, with a real append-only event store so
    the condense path (emit tombstone → re-fetch events) works."""

    def __init__(
        self,
        *,
        assist: bool,
        sandbox: _FakeSandbox,
        events: list,
        condenser=None,
        window: int = 192_000,
    ) -> None:
        self._assist = assist
        self.executor = _FakeExecutor(sandbox)
        self.condenser = condenser if condenser is not None else NoOpCondenser()
        self.summarizer = None
        self._window = window
        # Seq-stamped append-only log (mirrors the real store).
        self._log: list = []
        for e in events:
            self._append(e)

    def _append(self, event) -> None:
        next_seq = (self._log[-1].seq + 1) if self._log else 1
        self._log.append(event.model_copy(update={"seq": next_seq}))

    def _driver_context_window(self) -> int:
        return self._window

    def _gate_recitation(
        self, view: View, events: list, *, context_pack_active: bool = False
    ) -> View:
        return view

    def _f8_shrink_file_write_args(self, messages: list, events: list) -> list:
        return messages

    async def _emit(self, event) -> None:
        self._append(event)

    async def _events(self) -> list:
        return list(self._log)


class _FiringCondenser:
    """Fires ONCE: forgets the single earliest seq (an old span)."""

    def __init__(self) -> None:
        self.fired = False

    def should_condense(self, view: View, *, token_count):
        return None if self.fired else CondensationRequest(soft=True, reason="tokens")

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        if self.fired:
            return None
        self.fired = True
        seqs = [e.seq for e in events if e.seq is not None]
        if not seqs:
            return None
        start = min(seqs)
        return CondensationEvent(
            forgotten_start_seq=start,
            forgotten_end_seq=start,
            summary="[CONDENSED OLD SPAN]",
            reason="tokens",
        )


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


# ===========================================================================
# CW-4 — collapse_superseded_reads truthfulness (direct unit)
# ===========================================================================


def _two_reads_of(path: str) -> tuple[list[LLMMessage], list]:
    """messages + events for a path read TWICE in full (c1 earlier, c2 latest)."""
    a1 = ActionEvent(
        thought="r1",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": path}, call_id="c1"),
    )
    a2 = ActionEvent(
        thought="r2",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": path}, call_id="c2"),
    )
    messages = [
        LLMMessage(role="assistant", content="reading", tool_calls=[{"id": "c1"}]),
        LLMMessage(role="tool", content="FULL-BODY-READ-1", tool_call_id="c1"),
        LLMMessage(role="assistant", content="reading again", tool_calls=[{"id": "c2"}]),
        LLMMessage(role="tool", content="FULL-BODY-READ-2", tool_call_id="c2"),
    ]
    return messages, [a1, a2]


def test_bare_pinned_path_cannot_compact_legacy_reads():
    msgs, events = _two_reads_of("app.js")
    out = collapse_superseded_reads(msgs, events, pinned_full_paths=frozenset({"app.js"}))
    # A path is not byte identity. Legacy observations have no revision receipts.
    assert out[1].content == "FULL-BODY-READ-1"
    assert out[3].content == "FULL-BODY-READ-2"


def test_unpinned_legacy_reads_are_retained():
    msgs, events = _two_reads_of("big.js")
    # big.js was read but is NOT pinned in full this turn (e.g. truncated/omitted).
    out = collapse_superseded_reads(msgs, events, pinned_full_paths=frozenset())
    assert out[1].content == "FULL-BODY-READ-1"
    assert out[3].content == "FULL-BODY-READ-2"


def test_legacy_reads_are_retained_in_both_assist_modes():
    msgs, events = _two_reads_of("app.js")
    out_none = collapse_superseded_reads(msgs, events)  # default None
    out_explicit_none = collapse_superseded_reads(msgs, events, pinned_full_paths=None)
    assert out_none[1].content == "FULL-BODY-READ-1"
    assert out_explicit_none[1].content == out_none[1].content
    msgs2, events2 = _two_reads_of("big.js")
    out2 = collapse_superseded_reads(msgs2, events2, pinned_full_paths=None)
    assert out2[1].content == "FULL-BODY-READ-1"


# ===========================================================================
# CW-4 — end-to-end through ViewBuilder: a read of a path NOT in the block
# gets the no-claim notice; assist-ON is byte-identical.
# ===========================================================================


def _build_events_read_twice(paths: list[str]) -> list:
    """For each path: a file_write (puts it in the working set) then TWO reads."""
    out: list = []
    for p in paths:
        out.append(
            ActionEvent(
                thought=f"write {p}",
                tool_call=ToolCall(tool_name="file_write", arguments={"path": p, "content": ""}),
            )
        )
        for cid, body in ((f"{p}-r1", "READ-1"), (f"{p}-r2", "READ-2")):
            a = ActionEvent(
                thought=f"read {p}",
                tool_call=ToolCall(tool_name="file_read", arguments={"path": p}, call_id=cid),
            )
            o = ObservationEvent(
                tool_result=ToolResult(
                    call_id=cid, tool_name="file_read", success=True, content=body
                ),
                action_id=a.id,
            )
            out.extend([a, o])
    return out


def test_e2e_assist_off_legacy_reads_are_not_guessed_equivalent():
    # app.js is on disk (pinned full); gone.js was read then deleted (NOT in the
    # block). Both read twice → both stubbed; the notices must differ truthfully.
    sbx = _FakeSandbox({"app.js": b"const x = 1;\n"})  # gone.js intentionally absent
    events = _build_events_read_twice(["app.js", "gone.js"])
    loop = _FakeLoop(assist=False, sandbox=sbx, events=events)
    view = asyncio.run(ViewBuilder(loop).build(loop._log))

    snap = _snapshot_msg(view)
    assert snap is not None and "BEGIN FILE app.js" in snap.content
    # gone.js could not be re-read → it is NOT a full block in the snapshot.
    assert "BEGIN FILE gone.js" not in snap.content

    bodies = [m.content for m in view.messages if m.role == "tool"]
    joined = "\n".join(bodies)
    assert "READ-1" in joined
    assert "READ-2" in joined
    assert "superseded file_read" not in joined


def test_e2e_assist_on_legacy_reads_are_not_guessed_equivalent():
    sbx = _FakeSandbox({"app.js": b"const x = 1;\n"})
    events = _build_events_read_twice(["app.js", "gone.js"])
    loop = _FakeLoop(assist=True, sandbox=sbx, events=events)
    view = asyncio.run(ViewBuilder(loop).build(loop._log))
    joined = "\n".join(m.content for m in view.messages if m.role == "tool")
    assert "READ-1" in joined
    assert "READ-2" in joined
    assert "superseded file_read" not in joined


# ===========================================================================
# CW-5 — the pin covers the working set + survives condensation.
# ===========================================================================


def test_pin_survives_condensation_and_covers_touched_files(monkeypatch):
    # Condensation-mechanics test: the context pack (default ON) would
    # legitimately carry the old goal, which is not what this asserts about.
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "off")
    sbx = _FakeSandbox({"app.js": b"const ANSWER = 42;\n"})
    # An OLD user message (will be forgotten) + a write that creates the working set.
    old = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="UNIQUE-OLD-INSTRUCTION-XYZ"),
    )
    write = ActionEvent(
        thought="scaffold",
        tool_call=ToolCall(tool_name="file_write", arguments={"path": "app.js", "content": ""}),
    )
    obs = ObservationEvent(
        tool_result=ToolResult(call_id="w1", tool_name="file_write", success=True, content="ok"),
        action_id=write.id,
    )
    cond = _FiringCondenser()
    loop = _FakeLoop(assist=False, sandbox=sbx, events=[old, write, obs], condenser=cond)
    view = asyncio.run(ViewBuilder(loop).build(loop._log))

    # The condenser fired (a tombstone was emitted into the log).
    assert cond.fired
    assert any(isinstance(e, CondensationEvent) for e in loop._log)

    # The OLD span was forgotten — its text is gone from the rendered prompt.
    all_text = "\n".join(m.content for m in view.messages)
    assert "UNIQUE-OLD-INSTRUCTION-XYZ" not in all_text
    assert "[CONDENSED OLD SPAN]" in all_text  # the summary took its place

    # The pin SURVIVED: the snapshot is present, in the cacheable PREFIX (index 0),
    # and covers the touched file IN FULL (disk content, not condensed away).
    snap = _snapshot_msg(view)
    assert snap is not None
    assert _snapshot_index(view) == 0
    assert "BEGIN FILE app.js" in snap.content
    assert "const ANSWER = 42;" in snap.content
