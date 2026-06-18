"""K1 — elision-marker EXECUTION guard (data-loss / read-loop prevention).

# Contract

`events._snip_args` renders an over-long tool argument (e.g. a file_write body)
as a short placeholder in the action HISTORY so a 59 KB body isn't re-sent every
turn. A weak model can COPY that placeholder back into a REAL tool argument on a
later turn. If executed, that would:

  1. overwrite real on-disk content with the ~72-byte placeholder (DATA LOSS); and
  2. re-feed the marker into the next file_read, which returns the placeholder,
     which the model copies again — the reproduced 88× read loop.

The guard, in `observe.Observer.execute_and_observe` (BEFORE `executor.execute`),
rejects any tool call whose arguments carry an elision marker: it emits an
`AgentErrorEvent` (one observation per action — the contract) telling the model
the argument is a placeholder, was NOT executed, and to resend the FULL content
from the live CURRENT WORKSPACE snapshot. It NEVER runs the tool with a marker.

The detector (`find_elided_arg_markers`) matches the marker STRUCTURE
(`<N chars … {elided|full content} …>`), not the exact wording, so the marker can
be reworded without the guard going blind — both the legacy "…elided…" form and
the reworded "…full content…" form are caught.

# Acceptance (this file)

  1. an action carrying the (reworded) marker in a file_write body → REJECTED:
     executor NOT called; exactly one AgentErrorEvent emitted, correlated to the
     action; the error names the offending key and tells the model to resend.
  2. the LEGACY marker form ("…chars elided — already applied; use file_read…")
     → also REJECTED (the detector matches structure, not wording).
  3. a clean argument (no marker) → executes normally (executor called once,
     ObservationEvent emitted).
  4. a benign string that merely resembles the marker shape but lacks the
     signature word (e.g. "<5 chars>") → NOT rejected (no false positive).
  5. pure-function contracts: `find_elided_arg_markers` returns the offending
     keys; `_snip_args` rewords to the snapshot-pointing form AND that reworded
     form is self-consistently detected (round-trip).
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
)
from disco.core.events import _snip_args, find_elided_arg_markers
from disco.core.llm import OperatingMode
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    NeverConfirm,
    ScriptedAgent,
)

CID = "conv"

# The legacy marker (pre-K1 wording) — a model that learned it from an older
# trace can still emit it; the structural detector must catch it.
_LEGACY_MARKER = "<4,441 chars elided — already applied; use file_read for the content>"


class _NoOpCondenser:
    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


def _make_loop(*, executor=None):  # noqa: ANN202
    from disco.core.loop.engine import AgentLoop

    if executor is None:
        executor = FakeExecutor(
            result=ToolResult(
                call_id="ignored", tool_name="ignored", success=True, content="ok"
            )
        )
    return AgentLoop(
        CID,
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),  # never stepped
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        _NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        assist=False,  # K1 is tier-INDEPENDENT — must fire for capable models too
    )


def _write_action(call_id: str, *, path: str, content: str) -> ActionEvent:
    return ActionEvent(
        thought="writing the file",
        tool_call=ToolCall(
            tool_name="file_write",
            call_id=call_id,
            arguments={"path": path, "content": content},
        ),
    )


async def _drive_execute(loop, action: ActionEvent) -> list[Event]:  # noqa: ANN001
    """Append the action (the run loop's _emit) then drive _execute_and_observe,
    which runs the K1 guard before the executor. Returns the post-call events."""
    persisted = await loop.store.append(CID, action)
    await loop._execute_and_observe(persisted)
    return await loop.store.get_events(CID)


# ---------------------------------------------------------------------------
# (1) reworded marker copied into a file_write body → rejected, not executed
# ---------------------------------------------------------------------------


async def test_marker_in_write_body_is_rejected_and_not_executed():
    # The marker the shaper would render for a real body the model already wrote.
    marker = _snip_args({"content": "x" * 5000})["content"]
    loop = _make_loop()
    action = _write_action("call_w1", path="src/windows.js", content=marker)
    events = await _drive_execute(loop, action)

    # The executor was NEVER called — no placeholder was written to disk.
    assert len(loop.executor.calls) == 0, (
        "K1 must reject a tool call carrying the elision marker BEFORE execution; "
        "executing it would overwrite real content with the placeholder"
    )
    # Exactly one error observation, correlated to the action.
    errs = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(errs) == 1
    assert errs[0].action_id == action.id
    assert errs[0].tool_call_id == action.tool_call.call_id
    # No success observation was emitted (the contract: one observation, an error).
    assert not [e for e in events if isinstance(e, ObservationEvent)]
    # The error tells the model what happened + how to recover.
    msg = errs[0].error
    assert "content" in msg  # names the offending key
    assert "CURRENT WORKSPACE" in msg
    assert "NOT" in msg  # "was NOT executed" / "do not copy"


# ---------------------------------------------------------------------------
# (2) the legacy marker wording is also caught (structure, not wording)
# ---------------------------------------------------------------------------


async def test_legacy_marker_wording_is_also_rejected():
    loop = _make_loop()
    action = _write_action("call_w2", path="src/foo.py", content=_LEGACY_MARKER)
    events = await _drive_execute(loop, action)
    assert len(loop.executor.calls) == 0
    assert len([e for e in events if isinstance(e, AgentErrorEvent)]) == 1


# ---------------------------------------------------------------------------
# (3) a clean argument executes normally (the guard is invisible)
# ---------------------------------------------------------------------------


async def test_clean_write_executes_normally():
    loop = _make_loop()
    action = _write_action(
        "call_w3", path="src/foo.py", content="export const x = 1; // real content\n"
    )
    events = await _drive_execute(loop, action)
    # Executor called exactly once; a success observation, no error.
    assert len(loop.executor.calls) == 1
    assert len([e for e in events if isinstance(e, ObservationEvent)]) == 1
    assert not [e for e in events if isinstance(e, AgentErrorEvent)]


# ---------------------------------------------------------------------------
# (4) a benign marker-shaped string without the signature word → no false positive
# ---------------------------------------------------------------------------


async def test_benign_anglebracket_string_is_not_rejected():
    # A real file body can legitimately contain "<5 chars>" or "<input>"; the
    # detector requires the signature word ("elided"/"full content"), so these
    # must execute normally.
    loop = _make_loop()
    body = "if (s.length < 5) {} // a <5 chars> guard and an <input> element\n"
    action = _write_action("call_w4", path="src/foo.js", content=body)
    events = await _drive_execute(loop, action)
    assert len(loop.executor.calls) == 1, (
        "a benign string resembling the marker shape but lacking the signature "
        "word must NOT be rejected (no false positive)"
    )
    assert not [e for e in events if isinstance(e, AgentErrorEvent)]


# ---------------------------------------------------------------------------
# (5) pure-function contracts
# ---------------------------------------------------------------------------


def test_find_elided_arg_markers_reports_offending_keys():
    marker = _snip_args({"content": "y" * 3000})["content"]
    # Only the marker-bearing key is reported.
    assert find_elided_arg_markers({"path": "a.py", "content": marker}) == ["content"]
    # Legacy form too.
    assert find_elided_arg_markers({"body": _LEGACY_MARKER}) == ["body"]
    # Clean args → empty.
    assert find_elided_arg_markers({"path": "a.py", "content": "real"}) == []
    # Non-string values are ignored (no crash).
    assert find_elided_arg_markers({"index": 3, "state": "done"}) == []
    # Multiple offenders → all reported, in arg order.
    multi = {"a": marker, "b": "ok", "c": _LEGACY_MARKER}
    assert find_elided_arg_markers(multi) == ["a", "c"]


def test_snip_args_rewords_to_snapshot_pointer_and_round_trips():
    out = _snip_args({"content": "z" * 2000})["content"]
    # The reworded marker points at the workspace snapshot, NOT "use file_read".
    assert "CURRENT WORKSPACE" in out
    assert "file_read" not in out
    assert "do not copy" in out
    # Round-trip: the shaper's own output is detected by the guard (so a copied
    # placeholder is always caught regardless of future rewording).
    assert find_elided_arg_markers({"content": out}) == ["content"]
    # Short args are passed through untouched (no marker, no false positive).
    short = _snip_args({"content": "small"})
    assert short["content"] == "small"
    assert find_elided_arg_markers(short) == []
