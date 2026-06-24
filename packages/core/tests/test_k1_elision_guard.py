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
the argument is a placeholder, was NOT executed, and to resend the FULL content.
The RECOVERY WORDING is tier-gated — assist-ON keeps the pre-CW-3 directional
workspace-block pointer; assist-OFF uses a neutral file_read pointer (an elided arg
is a write body, not the file's current content, so a block claim would dangle).
It NEVER runs the tool with a marker.

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

# BW-02 (trace conv_20fa8482) — the model PARAPHRASED the neutral marker, dropping the
# leading "<N chars …>" count anchor while copying its stable tail prose verbatim. With
# no digit anchor the structural detector missed it; a 132-byte placeholder overwrote a
# real file. The separate paraphrase detector must catch this REJECTION-path case.
_PARAPHRASE_MARKER = (
    "<content elided — re-issue the call or file_read the path for the full "
    "content; do not copy this placeholder into a tool argument>"
)


class _NoOpCondenser:
    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


def _make_loop(*, executor=None, assist=False):  # noqa: ANN202
    from disco.core.llm.exec_policy import ModelExecutionPolicy
    from disco.core.loop.engine import AgentLoop

    if executor is None:
        executor = FakeExecutor(
            result=ToolResult(
                call_id="ignored", tool_name="ignored", success=True, content="ok"
            )
        )
    # K1 is tier-INDEPENDENT — the guard FIRES for every tier. Only the recovery WORDING
    # is tier-gated (assist-ON keeps the pre-CW-3 directional block pointer; assist-OFF
    # uses a neutral file_read pointer). assist defaults to False (standard policy).
    policy = (
        ModelExecutionPolicy(tier="weak", anchored_edit=True)
        if assist
        else ModelExecutionPolicy.standard()
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
        model_policy=policy,
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


def _replace_lines_action(call_id: str, *, path: str, new_text: str) -> ActionEvent:
    return ActionEvent(
        thought="patching the file",
        tool_call=ToolCall(
            tool_name="file_replace_lines",
            call_id=call_id,
            arguments={"path": path, "start_line": 1, "end_line": 5, "new_text": new_text},
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
    # The error tells the model what happened + how to recover. This loop is assist-OFF
    # (standard policy) → the recovery wording is NEUTRAL: it points at file_read, NOT at
    # the workspace block (an elided arg is a write body, not the file's current content,
    # so a "it's in the workspace block" claim would be a dangling pointer).
    msg = errs[0].error
    assert "content" in msg  # names the offending key
    assert "file_read" in msg
    assert "CURRENT WORKSPACE" not in msg  # neutral for assist-OFF — no block pointer
    assert "NOT" in msg  # "was NOT executed" / "do not copy"


# ---------------------------------------------------------------------------
# (1b) the recovery WORDING is tier-gated — assist-ON keeps the pre-CW-3 block pointer
# ---------------------------------------------------------------------------


async def test_rejection_wording_is_tier_gated():
    marker = _snip_args({"content": "x" * 5000})["content"]
    # assist-ON: the pre-CW-3 directional block pointer ("...block above (or call
    # file_read)..."), byte-identical to before CW-3.
    loop_on = _make_loop(assist=True)
    events_on = await _drive_execute(
        loop_on, _write_action("call_on", path="a.js", content=marker)
    )
    msg_on = [e for e in events_on if isinstance(e, AgentErrorEvent)][0].error
    assert "current content from the CURRENT WORKSPACE block above (or call file_read)" in msg_on
    # assist-OFF: the neutral file_read pointer, NO workspace-block claim.
    loop_off = _make_loop(assist=False)
    events_off = await _drive_execute(
        loop_off, _write_action("call_off", path="a.js", content=marker)
    )
    msg_off = [e for e in events_off if isinstance(e, AgentErrorEvent)][0].error
    assert "CURRENT WORKSPACE" not in msg_off
    assert "call file_read on the path for the authoritative content" in msg_off


# ---------------------------------------------------------------------------
# (1c) Bug 14 — a NON-file_write mutator (file_replace_lines) carrying the marker
#      in `new_text` is ALSO rejected before execution (the Observer guard is
#      tool-generic; the file_write recovery clause is the only file_write-specific
#      branch). Pairs with the executor-boundary guard test in the tools package
#      (test_executor_elision_guard) which covers the executor-DIRECT path.
# ---------------------------------------------------------------------------


async def test_marker_in_file_replace_lines_is_rejected_and_not_executed():
    marker = _snip_args({"new_text": "x" * 5000})["new_text"]
    loop = _make_loop()
    action = _replace_lines_action("call_rl1", path="src/app.js", new_text=marker)
    events = await _drive_execute(loop, action)

    # The mutator was NEVER reached — no placeholder could overwrite the file.
    assert len(loop.executor.calls) == 0, (
        "K1 must reject file_replace_lines carrying the elision marker BEFORE "
        "execution; there is no file_write-style recovery for a line-edit body"
    )
    errs = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(errs) == 1
    assert errs[0].action_id == action.id
    assert errs[0].tool_call_id == action.tool_call.call_id
    # No success observation (one observation per action — an error, not a run).
    assert not [e for e in events if isinstance(e, ObservationEvent)]
    # The error names the offending key (new_text), not content.
    assert "new_text" in errs[0].error


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
# (2b) BW-02 — a MODEL-PARAPHRASED placeholder (no count anchor) → also rejected
# ---------------------------------------------------------------------------


async def test_paraphrased_marker_without_count_is_rejected_and_not_executed():
    # The exact placeholder the model copied into js/shell.js in conv_20fa8482:
    # the count anchor was dropped ("<content elided …>"), so the structural
    # _ELISION_MARKER_RE (which requires "N chars") missed it. The separate
    # paraphrase detector catches it on the rejection path.
    loop = _make_loop()
    action = _write_action("call_para", path="js/shell.js", content=_PARAPHRASE_MARKER)
    events = await _drive_execute(loop, action)
    assert len(loop.executor.calls) == 0, (
        "K1 must reject the model-paraphrased placeholder BEFORE execution — this "
        "is the BW-02 data-loss path (132 bytes overwrote a real file)"
    )
    errs = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(errs) == 1
    assert errs[0].action_id == action.id
    # The reject copy is accurate for the paraphrased form too (no false "<N chars>").
    msg = errs[0].error
    assert "content" in msg  # names the offending key
    assert "<N chars" not in msg  # the old copy claimed this form — wrong for a paraphrase
    assert "placeholder" in msg


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
# (4b) BW-02 — a real file whose CONTENT merely says "elided" in prose → no false reject
# ---------------------------------------------------------------------------


async def test_prose_mentioning_elided_is_not_false_rejected():
    # The paraphrase detector requires BOTH the marker's signature tail phrase AND an
    # elision keyword inside one bounded <...> — so ordinary documentation that merely
    # uses the word "elided" (even inside angle brackets) executes normally.
    loop = _make_loop()
    body = (
        "# Notes\n"
        "The middle of the file was <elided> for brevity in the original PDF.\n"
        "See <appendix> for the full content of the report.\n"
    )
    action = _write_action("call_prose", path="docs/notes.md", content=body)
    events = await _drive_execute(loop, action)
    assert len(loop.executor.calls) == 1, (
        "a real file whose prose merely mentions 'elided'/'full content' must NOT be "
        "false-rejected — the paraphrase detector also requires the marker's tail phrase"
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


def test_find_elided_arg_markers_catches_paraphrase_but_not_prose():
    # BW-02 — the paraphrased placeholder (count anchor dropped) IS detected on the
    # rejection path...
    assert find_elided_arg_markers({"content": _PARAPHRASE_MARKER}) == ["content"]
    # ...while ordinary prose that merely contains "elided"/"full content" (even in
    # angle brackets) without the marker's signature tail phrase is NOT a false reject.
    assert find_elided_arg_markers({"c": "the section was <elided> for space"}) == []
    assert find_elided_arg_markers({"c": "<div>full content here</div>"}) == []
    assert find_elided_arg_markers({"c": "rows elided; see full content in appendix"}) == []


def test_retarget_preserved_for_count_markers_paraphrase_untouched():
    # The RETARGET path is unchanged by BW-02: it still reconstructs the neutral
    # marker from the char count (_ELISION_COUNT_RE) for a real count-bearing marker...
    from disco.core.events import (
        LLMMessage,
        _arg_snip_marker_below,
        retarget_elided_arg_markers,
    )

    below = _arg_snip_marker_below(4441)  # the directional/assist-ON marker (has count)
    msg = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[{"tool_name": "file_write", "call_id": "x", "arguments": {"content": below}}],
    )
    out = retarget_elided_arg_markers([msg])
    rewritten = out[0].tool_calls[0]["arguments"]["content"]
    assert "4,441 chars elided" in rewritten  # count preserved → neutral marker rebuilt
    # ...and a PARAPHRASE (no count to reconstruct) is left untouched by retarget — the
    # new broad detector is for REJECTION only, NOT retargeting.
    msgp = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[
            {
                "tool_name": "file_write",
                "call_id": "y",
                "arguments": {"content": _PARAPHRASE_MARKER},
            }
        ],
    )
    outp = retarget_elided_arg_markers([msgp])
    assert outp[0].tool_calls[0]["arguments"]["content"] == _PARAPHRASE_MARKER


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
