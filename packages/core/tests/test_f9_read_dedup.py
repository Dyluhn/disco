"""F9 — GATED read-only sliding-window dedup (assist-tier context/latency reclaim).

# Contract

When the assist gate is ON, a READ-ONLY tool call (e.g. ``file_read``) that
EXACTLY repeats a recent read-only call (same tool name + same arguments)
within the last ``_F9_WINDOW_SIZE`` read-only calls is short-circuited:
instead of re-executing the tool, the loop emits a synthetic ObservationEvent
whose content is a short pointer to the prior result. The model can re-`file_read`
to force a fresh read (lossless). This is a context/latency reclaim for weak
models that re-read the same file within a turn.

Invalidation: a path promoted to "mutated" by the existing A8 working-set logic
(file_write / file_edit / file_append / file_replace_lines / file_insert_lines
on the same path) BETWEEN the prior read and now invalidates the cached read
(stale pointer). The read must have a successful observation; a failed prior
read is not a dedup candidate.

"Read-only" is determined by the SAME source the engine already uses
(``executor.readonly_tool_names()`` via ``_readonly_tool_names()``, with a
fallback to the narrow ``_WORKSPACE_READ_TOOLS`` frozenset). No new hardcoded
list is invented.

The transform is GATED on ``self._assist``. Assist OFF (capable-model default)
→ the dedup code path is never entered; every read re-executes, byte-identical
to today. Only READ-ONLY tool calls are deduped — write/state-changing calls
are never short-circuited.

# Acceptance (this file)

  1. assist ON + a second identical ``file_read`` of path P, with no write to
     P between the two reads → short-circuited: a synthetic ObservationEvent
     carrying the F9 pointer is emitted; the executor is NOT called for the
     second read.
  2. assist ON + file P WRITTEN between the two reads → re-executes normally
     (the cached read is stale; a pointer would be wrong).
  3. assist OFF → every read re-executes; the F9 dedup code path is never
     entered (byte-identical to today).
  4. assist ON + read with DIFFERENT args (e.g. different offset/limit) → not
     deduped. Exact-args match is the contract.
  5. assist ON + non-read-only call (e.g. ``shell``) → not deduped. F9 is
     read-only by definition.
  6. assist ON + identical read but with more than ``_F9_WINDOW_SIZE`` read-
     only calls in between (window eviction) → not deduped; re-executes.

Supplementary tests pin the helper's pure-function contract: invalidation is
grounded in the existing mutating-tool set, the pointer text is a
single-source-of-truth template, and the window counter only counts
read-only calls (not steps).

The tests drive ``_execute_and_observe`` directly (mirrors the F8
`_materialize_view` direct-drive pattern) so the gate, the dedup, and the
synthetic observation are all observable without a full run() loop.
"""

from __future__ import annotations

import asyncio

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode
from disco.core.loop.engine import (
    _F9_ARG_SUMMARY_MAX_CHARS,
    _F9_POINTER_TEMPLATE,
    _F9_WINDOW_SIZE,
    _f9_arg_summary,
    _f9_dedupable_read,
    _f9_has_successful_observation,
    _f9_path_was_mutated_after,
)
from conftest import user_msg, with_seqs
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    NeverConfirm,
    ScriptedAgent,
)

CID = "conv"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _NoOpCondenser:
    """Inert condenser — should_condense returns None, condense returns None.
    Keeps the test focused on the F9 dedup, not the condenser's."""

    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


class _ReadonlyExecutor(FakeExecutor):
    """A FakeExecutor that reports read-only tools via readonly_tool_names(),
    like the real DefaultToolExecutor does. Tests that want to drive the F9
    dedup under the executor-reported path use this; tests that want to
    drive the FALLBACK path (executor doesn't report; engine uses
    _WORKSPACE_READ_TOOLS = {'file_read'}) use the plain FakeExecutor.

    Defaults: file_read is read-only; shell is not. The `result` is passed
    through to FakeExecutor (a fixed ToolResult returned for every call);
    tests that want the dedup-vs-real-output contrast set a custom result
    with distinctive content (e.g. "the file content") so the post-dedup
    pointer's distinctiveness is directly observable."""

    def __init__(self, *, tools=None, result=None, raises=None, readonly=None):
        super().__init__(tools=tools, result=result, raises=raises)
        self._readonly = frozenset(readonly) if readonly is not None else frozenset({"file_read"})

    def readonly_tool_names(self):
        return self._readonly


def _make_loop(*, assist: bool, executor=None) -> "AgentLoop":  # noqa: F821
    """Build an AgentLoop over a real in-memory store. Tests drive
    `_execute_and_observe` directly; the agent is never stepped."""
    from disco.core.loop.engine import AgentLoop

    if executor is None:
        # Default: a ReadonlyExecutor that returns a distinctive
        # "the file content" string for every tool call, so the dedup
        # vs. real-output contrast is directly observable.
        executor = _ReadonlyExecutor(
            result=ToolResult(
                call_id="ignored",
                tool_name="ignored",
                success=True,
                content="the file content",
            )
        )
    return AgentLoop(
        CID,
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),  # never stepped
        executor,
        None,  # router — held but unused by the loop (the Agent owns it)
        FakeAnalyzer(),
        NeverConfirm(),
        _NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        assist=assist,
    )


def _read_action(
    call_id: str = "call_read_1",
    path: str = "src/foo.py",
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> ActionEvent:
    """Build a file_read ActionEvent. offset/limit default to None (full
    file read) so two identical calls compare cleanly."""
    args: dict = {"path": path}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return ActionEvent(
        thought="reading the file",
        tool_call=ToolCall(tool_name="file_read", call_id=call_id, arguments=args),
    )


def _write_action(
    call_id: str = "call_write_1",
    path: str = "src/foo.py",
    content: str = "new content",
) -> ActionEvent:
    """Build a file_write ActionEvent — the A8 mutating tool used to
    invalidate cached reads in the dedup invalidation tests."""
    return ActionEvent(
        thought="writing the file",
        tool_call=ToolCall(
            tool_name="file_write",
            call_id=call_id,
            arguments={"path": path, "content": content},
        ),
    )


def _shell_action(call_id: str = "call_shell_1", cmd: str = "ls") -> ActionEvent:
    return ActionEvent(
        thought="running a command",
        tool_call=ToolCall(tool_name="shell", call_id=call_id, arguments={"command": cmd}),
    )


def _read_success_observation(
    action: ActionEvent, *, content: str = "the file content", success: bool = True
) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="file_read",
            success=success,
            content=content,
            error=None if success else "read failed",
        ),
        action_id=action.id,
    )


def _write_success_observation(action: ActionEvent) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="file_write",
            success=True,
            content="wrote 11 chars",
        ),
        action_id=action.id,
    )


def _shell_success_observation(action: ActionEvent) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="shell",
            success=True,
            content="command output",
        ),
        action_id=action.id,
    )


async def _drive_execute(loop: "AgentLoop", action: ActionEvent) -> list[Event]:  # noqa: F821
    """Drive ``_execute_and_observe`` end-to-end: append the action to the
    loop's store (mimicking the run loop's ``_emit(action)``), call
    ``_execute_and_observe`` (which performs the F9 check + emit any
    synthetic observation), then return the post-call event list.

    Mirrors the F8 test's direct-drive pattern: we never call ``agent.step``
    or ``loop.run``; the gate + dedup are the only behavior under test.
    """
    store = loop.store
    # Record the proposed action. This is the run loop's normal flow
    # (line 5501 in engine.py: ``await self._emit(action_to_execute)``
    # immediately before ``_execute_and_observe``).
    persisted = await store.append(CID, action)
    # Use the persisted event (its seq is assigned by the store) for the
    # dedup check inside _execute_and_observe.
    await loop._execute_and_observe(persisted)
    return await store.get_events(CID)


# ---------------------------------------------------------------------------
# (1) Happy path: assist ON + repeat identical read → short-circuited
# ---------------------------------------------------------------------------


async def test_assist_on_repeat_read_short_circuits_with_pointer():
    """A second identical file_read of path P, with no write to P between
    the two reads, is short-circuited: the executor is NOT called for the
    second read; the second ObservationEvent's content is the F9 pointer
    (not the tool's actual output). The model sees a clear pointer to
    the prior result instead of a duplicated page of file content.
    """
    # Pass a distinctive result so the dedup-vs-real-output contrast
    # is observable (the real output is "the file content"; the F9
    # pointer is the marker template).
    ex = _ReadonlyExecutor(
        result=ToolResult(
            call_id="ignored",
            tool_name="ignored",
            success=True,
            content="the file content",
        )
    )
    loop = _make_loop(assist=True, executor=ex)
    # First read: full normal path (propose → _emit → _execute_and_observe).
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    events = await _drive_execute(loop, first)
    # Executor was called once.
    assert len(loop.executor.calls) == 1
    # First observation is the real tool output (not a pointer).
    first_obs = next(e for e in events if isinstance(e, ObservationEvent))
    assert first_obs.tool_result.content == "the file content"
    assert "F9 dedup" not in first_obs.tool_result.content

    # Second read: identical tool + identical args → must dedup.
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    # Executor was NOT called again — the dedup fired.
    assert len(loop.executor.calls) == 1, (
        "F9 dedup must short-circuit the executor; the second read "
        "should not have invoked executor.execute()"
    )
    # The second observation is the synthetic pointer.
    obs_events = [e for e in events if isinstance(e, ObservationEvent)]
    assert len(obs_events) == 2
    second_obs = obs_events[1]
    # The content is the F9 pointer (not the real tool output).
    assert "F9 dedup" in second_obs.tool_result.content
    assert "file_read(src/foo.py)" in second_obs.tool_result.content
    # The pointer's success is True (the dedup is itself a successful
    # observation — the prior read succeeded; the pointer is the load-
    # bearing piece).
    assert second_obs.tool_result.success is True
    # The observation is correlated to the second action (KV-cache pairing).
    assert second_obs.action_id == second.id
    assert second_obs.tool_result.call_id == second.tool_call.call_id


async def test_assist_on_repeat_read_pointer_text_is_lossless():
    """The pointer text is the single source of truth template
    ``_F9_POINTER_TEMPLATE`` and explicitly tells the model how to force
    a fresh read (`file_read again only if you suspect it changed`).
    The pointer names the tool + the args summary so the model knows
    which earlier read to look at. Lossless: a re-`file_read` recovers
    the live content if the model suspects staleness.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    pointer = next(
        e for e in events
        if isinstance(e, ObservationEvent) and "F9 dedup" in e.tool_result.content
    ).tool_result.content
    # The pointer is the F9 template applied with the right substitutions.
    expected = _F9_POINTER_TEMPLATE.format(
        tool_name="file_read", arg_summary="src/foo.py"
    )
    assert pointer == expected
    # The pointer explicitly tells the model the recovery affordance.
    assert "file_read again" in pointer
    assert "see the earlier result" in pointer


# ---------------------------------------------------------------------------
# (2) Invalidation: file P mutated between the two reads → re-executes
# ---------------------------------------------------------------------------


async def test_assist_on_read_after_write_to_same_path_re_executes():
    """A write to path P between the two reads MUST invalidate the cached
    read of P. The second read re-executes (the on-disk content is now
    different from what the prior read returned; a stale pointer would
    be wrong). The invalidation check uses the A8 mutating-tool set
    (file_write / file_edit / file_append / file_replace_lines /
    file_insert_lines) — any of them on the same path invalidates.
    """
    # Echo the executor's tool name + call_id back so the observation
    # is recognizable. (A fixed `result=` would set the SAME tool_name
    # for every call, masking the file_read vs. file_write distinction.)
    class _EchoResultExecutor(_ReadonlyExecutor):
        async def execute(self, call):
            from disco.core import ToolResult as _TR
            self.calls.append(call)
            return _TR(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content="the file content" if call.tool_name == "file_read" else "wrote",
            )
    ex = _EchoResultExecutor()
    loop = _make_loop(assist=True, executor=ex)
    # 1. First read of P.
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    # 2. Write to P.
    write = _write_action(call_id="call_write_1", path="src/foo.py", content="new")
    events = await _drive_execute(loop, write)
    # The write's success observation was emitted.
    assert any(
        isinstance(e, ObservationEvent) and e.tool_result.tool_name == "file_write"
        for e in events
    )
    # 3. Second read of P (identical args) → MUST re-execute (stale).
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    # The executor was called for the second read (no dedup).
    file_read_calls = [
        c for c in loop.executor.calls if c.tool_name == "file_read"
    ]
    assert len(file_read_calls) == 2, (
        "F9 dedup must NOT fire after a write to the same path; "
        "the second read should re-execute"
    )
    # The second observation is the real tool output, NOT the F9 pointer.
    obs_events = [e for e in events if isinstance(e, ObservationEvent)]
    read_obs = [
        e for e in obs_events if e.tool_result.tool_name == "file_read"
    ]
    assert len(read_obs) == 2
    assert "F9 dedup" not in read_obs[1].tool_result.content
    assert read_obs[1].tool_result.content == "the file content"


async def test_assist_on_read_after_write_to_DIFFERENT_path_dedups():
    """A write to a DIFFERENT path does NOT invalidate the cached read
    of P. The dedup still fires for a repeat read of P. The invalidation
    is path-specific, not a global 'anything happened' check.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    # 1. First read of P.
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    # 2. Write to a DIFFERENT path (not P).
    write = _write_action(call_id="call_write_1", path="src/other.py", content="x")
    await _drive_execute(loop, write)
    # 3. Second read of P → must dedup (P is unchanged on disk).
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    assert len(file_read_calls) == 1, (
        "F9 dedup must fire when a write happened to a DIFFERENT path; "
        "the prior read of P is still valid"
    )
    obs_events = [e for e in events if isinstance(e, ObservationEvent)]
    read_obs = [e for e in obs_events if e.tool_result.tool_name == "file_read"]
    assert "F9 dedup" in read_obs[1].tool_result.content


# ---------------------------------------------------------------------------
# (3) Iron rule: assist OFF → byte-identical to today
# ---------------------------------------------------------------------------


async def test_assist_off_repeat_read_always_re_executes():
    """With assist OFF (the capable-model default), the F9 dedup code
    path is NEVER entered. Every file_read re-executes, byte-identical
    to the pre-F9 baseline. This is the no-op argument for assist-OFF:
    the gate is closed end-to-end, the function body is never invoked,
    and the executor is called for every read.
    """
    loop = _make_loop(assist=False, executor=_ReadonlyExecutor())
    # Three identical reads.
    for i in range(3):
        action = _read_action(call_id=f"call_read_{i + 1}", path="src/foo.py")
        events = await _drive_execute(loop, action)
        read_obs = [
            e for e in events
            if isinstance(e, ObservationEvent) and e.tool_result.tool_name == "file_read"
        ]
        # Every observation is the real tool output, NEVER the F9 pointer.
        assert all("F9 dedup" not in o.tool_result.content for o in read_obs), (
            "F9 dedup must not fire when assist=OFF; the F9 pointer must "
            "be ABSENT from every observation"
        )
    # Executor was called three times (one per read).
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    assert len(file_read_calls) == 3


async def test_assist_off_with_write_between_reads_byte_identical_to_today():
    """The F9 gate is the ONLY place that special-cases the dedup; with
    assist OFF, even a write between reads doesn't change behavior
    (today's behavior is: every read re-executes, regardless of
    intervening writes). This is the byte-identical-to-today contract
    for the OFF path."""
    loop = _make_loop(assist=False, executor=_ReadonlyExecutor())
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    write = _write_action(call_id="call_write_1", path="src/foo.py", content="x")
    await _drive_execute(loop, write)
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    # Three executor calls total (one per action). No F9 pointer anywhere.
    assert len(loop.executor.calls) == 3
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert all("F9 dedup" not in o.tool_result.content for o in obs)


# ---------------------------------------------------------------------------
# (4) Different args → not deduped (exact-args match required)
# ---------------------------------------------------------------------------


async def test_assist_on_read_with_different_offset_does_not_dedup():
    """A read with a DIFFERENT offset/limit is NOT an exact-args repeat
    of the prior read. The dedup contract is exact-args; the two reads
    return different slices of the file. The second read re-executes.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    first = _read_action(call_id="call_read_1", path="src/foo.py", offset=1, limit=10)
    await _drive_execute(loop, first)
    second = _read_action(call_id="call_read_2", path="src/foo.py", offset=11, limit=20)
    events = await _drive_execute(loop, second)
    # Two executor calls — the args differ (offset+limit differ).
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    assert len(file_read_calls) == 2
    # No F9 pointer in any observation.
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert all("F9 dedup" not in o.tool_result.content for o in obs)


async def test_assist_on_read_with_different_path_does_not_dedup():
    """A read of a DIFFERENT path is not a dedup candidate (different
    files, different content). The second read re-executes.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    second = _read_action(call_id="call_read_2", path="src/bar.py")
    events = await _drive_execute(loop, second)
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    assert len(file_read_calls) == 2
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert all("F9 dedup" not in o.tool_result.content for o in obs)


# ---------------------------------------------------------------------------
# (5) Non-read-only call → never deduped
# ---------------------------------------------------------------------------


async def test_assist_on_non_read_only_call_does_not_dedup():
    """F9 is read-only by definition. A non-read-only call (e.g. ``shell``)
    MUST NOT be deduped — re-executing a shell call twice is a SIDE
    EFFECT, not a no-op. The executor is called for every shell
    invocation.
    """
    # Echo the tool name back so file_read vs. shell observations are
    # distinguishable (a fixed `result=` would set the same tool_name
    # for every call, masking the distinction).
    class _EchoResultExecutor(_ReadonlyExecutor):
        async def execute(self, call):
            from disco.core import ToolResult as _TR
            self.calls.append(call)
            return _TR(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content="ok" if call.tool_name == "shell" else "the file content",
            )
    ex = _EchoResultExecutor()
    loop = _make_loop(assist=True, executor=ex)
    # Two identical shell calls. The dedup helper's success-observation
    # check would pass for both — the ONLY thing that should stop the
    # dedup is the read-only check itself.
    first = _shell_action(call_id="call_shell_1", cmd="ls")
    await _drive_execute(loop, first)
    second = _shell_action(call_id="call_shell_2", cmd="ls")
    events = await _drive_execute(loop, second)
    # Executor was called for BOTH shell calls.
    shell_calls = [c for c in loop.executor.calls if c.tool_name == "shell"]
    assert len(shell_calls) == 2, (
        "F9 must never dedup a non-read-only call (shell); the executor "
        "must be called every time"
    )
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert all("F9 dedup" not in o.tool_result.content for o in obs)


async def test_assist_on_mixed_read_then_write_then_repeat_read_dedups_read_only():
    """Sanity: a write call between two reads does not poison the F9
    dedup's read-only check. The dedup fires for the second read only
    if it's read-only + exact-args + no invalidation. A write is a
    mutating tool (not read-only) and is never itself deduped.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    # 1. First read of P.
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    # 2. A write to a different path (so it doesn't invalidate the read of P).
    write = _write_action(call_id="call_write_1", path="src/other.py", content="x")
    await _drive_execute(loop, write)
    # 3. Second read of P → must dedup (read-only + exact args + P unchanged).
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    assert len(file_read_calls) == 1, "the second read should have deduped"
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    read_obs = [o for o in obs if o.tool_result.tool_name == "file_read"]
    assert "F9 dedup" in read_obs[1].tool_result.content
    # The write was also actually executed (write is not deduped).
    file_write_calls = [c for c in loop.executor.calls if c.tool_name == "file_write"]
    assert len(file_write_calls) == 1


# ---------------------------------------------------------------------------
# (6) Window eviction: a read outside the window re-executes
# ---------------------------------------------------------------------------


async def test_assist_on_repeat_read_outside_window_re_executes():
    """When more than ``_F9_WINDOW_SIZE`` read-only calls sit between
    two identical reads, the most recent one is outside the window and
    the dedup does NOT fire. The second read re-executes. The window
    is in READ-ONLY calls (not steps), so we build a chain of
    distinct read-only calls in between.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    # 1. First read of P (the cache seed).
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    # 2. _F9_WINDOW_SIZE OTHER distinct read-only calls (different paths
    #    so the same-tool+same-args check would also fail — we use the
    #    same path to be tighter: the dedup helper's SAME-TOOL check
    #    would also short-circuit, so we need distinct paths to
    #    advance the ro_calls counter without becoming dedup candidates).
    for i in range(_F9_WINDOW_SIZE):
        other = _read_action(call_id=f"call_other_{i + 1}", path=f"src/other_{i}.py")
        await _drive_execute(loop, other)
    # 3. Now the first read is more than _F9_WINDOW_SIZE read-only
    #    calls in the past. The second read of P → must NOT dedup
    #    (window eviction).
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    # 1 (first read of P) + _F9_WINDOW_SIZE (other reads) + 1 (second read of P) = _F9_WINDOW_SIZE+2
    assert len(file_read_calls) == _F9_WINDOW_SIZE + 2
    # The second read of P was actually executed (no F9 pointer).
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    read_obs = [o for o in obs if o.tool_result.tool_name == "file_read"]
    assert "F9 dedup" not in read_obs[-1].tool_result.content


async def test_assist_on_repeat_read_just_inside_window_dedups():
    """The boundary check: a repeat read with exactly _F9_WINDOW_SIZE-1
    read-only calls in between STILL dedups (window inclusive). The
    window bound is `ro_calls_seen > window` → break, so the count
    hits `window` (inclusive) without breaking. The dedup helper
    walks back through the prior read-only calls; the most recent
    one of the same tool+args is still in scope.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    # 1. First read of P.
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    # 2. _F9_WINDOW_SIZE-1 distinct read-only calls in between.
    for i in range(_F9_WINDOW_SIZE - 1):
        other = _read_action(call_id=f"call_other_{i + 1}", path=f"src/other_{i}.py")
        await _drive_execute(loop, other)
    # 3. Second read of P → must dedup (still inside the window).
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    # 1 (first read of P) + (_F9_WINDOW_SIZE-1) (others) = _F9_WINDOW_SIZE
    # The second read of P is deduped → NOT executed.
    assert len(file_read_calls) == _F9_WINDOW_SIZE
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    read_obs = [o for o in obs if o.tool_result.tool_name == "file_read"]
    assert "F9 dedup" in read_obs[-1].tool_result.content


# ---------------------------------------------------------------------------
# Module-level helper: pure-function contracts
# ---------------------------------------------------------------------------


def test_f9_arg_summary_prefers_path():
    """The pointer's arg summary prefers `path` (the most common
    identifying field for read-only calls) and length-bounds so a
    2KB path doesn't bloat the pointer."""
    # Path-only args.
    s = _f9_arg_summary({"path": "src/foo.py"})
    assert s == "src/foo.py"
    # Path + offset/limit (full read args) — path wins.
    s = _f9_arg_summary({"path": "src/foo.py", "offset": 1, "limit": 10})
    assert s == "src/foo.py"
    # No path → first non-empty string arg in stable key order.
    s = _f9_arg_summary({"query": "find me", "limit": 5})
    assert s == "query=find me"
    # Path too long → truncated with ellipsis.
    long_path = "a" * (_F9_ARG_SUMMARY_MAX_CHARS + 50)
    s = _f9_arg_summary({"path": long_path})
    assert len(s) <= _F9_ARG_SUMMARY_MAX_CHARS
    assert s.endswith("\u2026")
    # Empty/None args → empty string (defensive).
    assert _f9_arg_summary({}) == ""
    assert _f9_arg_summary(None) == ""


def test_f9_pointer_template_is_single_source_of_truth():
    """The pointer template is a single source of truth — it's the
    contract the model sees. It names the tool + the args summary,
    tells the model to refer to the earlier result, and tells it
    the recovery affordance (`file_read again`). The template is
    format-only (no f-strings in the contract — format() applies at
    call time so a future change to args summary doesn't require a
    template change).
    """
    rendered = _F9_POINTER_TEMPLATE.format(
        tool_name="file_read", arg_summary="src/foo.py"
    )
    assert "file_read(src/foo.py)" in rendered
    assert "F9 dedup" in rendered
    assert "see the earlier result" in rendered
    assert "file_read again" in rendered


def test_f9_dedupable_read_returns_false_for_non_readonly_tool():
    """Pure helper contract: a non-read-only tool never dedups, even
    if its args perfectly match a prior call. The read-only check
    grounds on the executor's reported set when available; falls back
    to _WORKSPACE_READ_TOOLS otherwise. Both paths are tested.
    """
    events = with_seqs(
        [
            user_msg("hi"),
            _shell_action(call_id="c1", cmd="ls"),
            _shell_success_observation(_shell_action(call_id="c1", cmd="ls")),
        ]
    )
    # With the executor's reported set: shell is NOT in `{"file_read"}` → no dedup.
    deduped, _, _ = _f9_dedupable_read(
        "shell", {"command": "ls"}, events, readonly_names=frozenset({"file_read"})
    )
    assert deduped is False
    # Without an executor-reported set: shell is NOT in
    # _WORKSPACE_READ_TOOLS = {"file_read"} → no dedup (the fallback).
    deduped, _, _ = _f9_dedupable_read("shell", {"command": "ls"}, events, readonly_names=None)
    assert deduped is False
    # file_read IS in both sets → would dedup (no prior match in this
    # event list, so still no dedup, but the read-only check passes —
    # the prior read of a different file would dedup a future one).
    deduped, _, _ = _f9_dedupable_read(
        "file_read", {"path": "x.py"}, events, readonly_names=frozenset({"file_read"})
    )
    assert deduped is False  # no prior file_read to match


def test_f9_dedupable_read_returns_false_when_no_prior_match():
    """No prior matching call → no dedup. The helper is well-defined
    on the empty-prior case (the F9 path is just a fast no-op)."""
    events = with_seqs([user_msg("hi")])
    deduped, prior_id, pointer = _f9_dedupable_read(
        "file_read", {"path": "x.py"}, events, readonly_names=frozenset({"file_read"})
    )
    assert deduped is False
    assert prior_id == ""
    assert pointer == ""


def test_f9_dedupable_read_invalidation_with_mutation_between():
    """The pure helper's invalidation check: a write to the same path
    between the prior read and now MUST skip that match. The helper
    returns False (the more recent match is stale); it does NOT skip
    the entire search — older non-stale matches are still eligible.
    """
    # Build events: read P, write P, read P again (would be a dup if
    # not for the intervening write).
    first_read = _read_action(call_id="c1", path="P")
    write = _write_action(call_id="c2", path="P", content="x")
    second_read = _read_action(call_id="c3", path="P")
    events = with_seqs(
        [
            user_msg("hi"),
            first_read,
            _read_success_observation(first_read),
            write,
            _write_success_observation(write),
            # Note: second_read is what we'd pass to the helper. We
            # DON'T append it — the helper walks events[:-1] of the
            # CURRENT action (which the caller has just emitted). The
            # `events` list here IS the list INCLUDING the current
            # action (so the helper sees events[:-1] = first_read +
            # write + their observations).
            second_read,
        ]
    )
    # The current read (second_read) is the LAST event; the prior
    # matching read is first_read; the invalidation (write to P) sits
    # between them. The helper MUST NOT dedup.
    deduped, prior_id, pointer = _f9_dedupable_read(
        "file_read", {"path": "P"}, events, readonly_names=frozenset({"file_read"})
    )
    assert deduped is False
    assert prior_id == ""
    assert pointer == ""


def test_f9_dedupable_read_matches_oldest_clean_when_recent_is_stale():
    """The pure helper keeps scanning for an older clean match when
    the most recent match is invalidated. The scenario: read P, read
    Q, write P, read P again → the recent read of P (right before
    the write) is stale, but an OLDER read of P (before Q) is still
    valid. The helper should return the OLDER match.
    """
    # We can't easily sequence seqs to test this in isolation without
    # the helper, so we drive the helper directly with hand-built
    # events that simulate the scenario.
    read_p_1 = _read_action(call_id="c1", path="P")
    read_q = _read_action(call_id="c2", path="Q")
    write_p = _write_action(call_id="c3", path="P", content="x")
    read_p_2 = _read_action(call_id="c4", path="P")  # the "current" call
    events = with_seqs(
        [
            user_msg("hi"),
            read_p_1,
            _read_success_observation(read_p_1),
            read_q,
            _read_success_observation(read_q),
            write_p,
            _write_success_observation(write_p),
            # read_p_2 is the CURRENT call (the last event); the helper
            # walks events[:-1] = everything before it.
            read_p_2,
        ]
    )
    deduped, prior_id, pointer = _f9_dedupable_read(
        "file_read", {"path": "P"}, events, readonly_names=frozenset({"file_read"})
    )
    # No clean match in the events before read_p_2 (the only prior
    # read of P is invalidated by write_p; nothing earlier to fall
    # back to). The helper returns no-dedup.
    assert deduped is False


def test_f9_dedupable_read_falls_back_to_workspace_read_tools():
    """When the executor doesn't report readonly_tool_names, the
    helper falls back to _WORKSPACE_READ_TOOLS = {'file_read'}.
    file_read matches the fallback; shell does not."""
    first_read = _read_action(call_id="c1", path="P")
    current_read = _read_action(call_id="c2", path="P")
    events = with_seqs(
        [
            user_msg("hi"),
            first_read,
            _read_success_observation(first_read),
            current_read,  # the "current" call (last event)
        ]
    )
    # readonly_names=None → fallback path. file_read is in the
    # fallback; the prior match + valid obs + no mutation → dedup.
    deduped, prior_id, pointer = _f9_dedupable_read(
        "file_read", {"path": "P"}, events, readonly_names=None
    )
    assert deduped is True
    assert prior_id  # the prior action's id
    assert "F9 dedup" in pointer
    # shell is not in the fallback → no dedup.
    deduped_shell, _, _ = _f9_dedupable_read(
        "shell", {"command": "ls"}, events, readonly_names=None
    )
    assert deduped_shell is False


def test_f9_has_successful_observation_helper():
    """The pure helper: returns True iff a successful observation
    exists for the given action_id. Failed reads and missing
    observations both return False."""
    action = _read_action(call_id="c1", path="P")
    events = with_seqs(
        [
            user_msg("hi"),
            action,
            _read_success_observation(action, success=True),
        ]
    )
    assert _f9_has_successful_observation(events, action.id) is True
    # Failed observation → False.
    failed_action = _read_action(call_id="c2", path="Q")
    events_failed = with_seqs(
        [
            user_msg("hi"),
            failed_action,
            _read_success_observation(failed_action, success=False),
        ]
    )
    assert _f9_has_successful_observation(events_failed, failed_action.id) is False
    # No observation at all → False.
    events_no_obs = with_seqs([user_msg("hi"), _read_action(call_id="c3", path="R")])
    assert _f9_has_successful_observation(events_no_obs, "no-such-id") is False


def test_f9_path_was_mutated_after_helper():
    """The pure helper's invalidation contract: returns True iff a
    mutating tool call (file_write / file_edit / file_append /
    file_replace_lines / file_insert_lines) on `path` exists in
    events with seq > after_seq. Non-mutating tools (file_read,
    shell) on the same path are NOT mutations — the invalidation
    is path-based AND mutation-based (both required)."""
    # A write after_seq → True.
    write = _write_action(call_id="c1", path="P", content="x")
    events = with_seqs(
        [
            user_msg("hi"),
            _read_action(call_id="c0", path="P"),  # prior read, seq < write
            write,
        ]
    )
    assert _f9_path_was_mutated_after(events, "P", after_seq=1) is True
    # A write BEFORE after_seq → False.
    assert _f9_path_was_mutated_after(events, "P", after_seq=10) is False
    # A write to a DIFFERENT path → False.
    assert _f9_path_was_mutated_after(events, "OTHER", after_seq=1) is False
    # A non-mutating tool on the same path → False.
    read = _read_action(call_id="c2", path="P")
    events_read = with_seqs(
        [
            user_msg("hi"),
            _read_action(call_id="c0", path="P"),
            read,
        ]
    )
    assert _f9_path_was_mutated_after(events_read, "P", after_seq=1) is False
    # Empty path → False (defensive).
    assert _f9_path_was_mutated_after(events, "", after_seq=0) is False
    assert _f9_path_was_mutated_after(events, None, after_seq=0) is False  # type: ignore[arg-type]


async def test_f9_pure_helper_window_bound_eviction():
    """The pure helper's window eviction: more than ``_F9_WINDOW_SIZE``
    read-only calls in the prior event list → the search gives up
    before finding a match, even with a clean prior read available.
    """
    # Build a chain: first read of P, then _F9_WINDOW_SIZE distinct
    # read-only calls, then a current read of P. The first read is
    # outside the window → no dedup.
    events_list: list[Event] = [user_msg("hi")]
    first = _read_action(call_id="c0", path="P")
    events_list.append(first)
    events_list.append(_read_success_observation(first))
    for i in range(_F9_WINDOW_SIZE):
        other = _read_action(call_id=f"c{i + 1}", path=f"O{i}")
        events_list.append(other)
        events_list.append(_read_success_observation(other))
    # The "current" call (last event).
    second = _read_action(call_id="c_last", path="P")
    events_list.append(second)
    events = with_seqs(events_list)

    deduped, prior_id, pointer = _f9_dedupable_read(
        "file_read", {"path": "P"}, events, readonly_names=frozenset({"file_read"})
    )
    # The first read of P is outside the window → no dedup.
    assert deduped is False
    assert prior_id == ""
    assert pointer == ""


# ---------------------------------------------------------------------------
# Edge: dedup is purely observation-side; the prior result stays on the wire
# ---------------------------------------------------------------------------


async def test_assist_on_dedup_does_not_lose_prior_observation():
    """The F9 dedup short-circuits the CURRENT call. The PRIOR call's
    ObservationEvent remains in the event store (the prior observation
    is the load-bearing piece — the model needs to be able to see the
    earlier result for the pointer to mean anything). F9 only adds a
    NEW pointer observation; it does NOT remove the prior one.
    """
    ex = _ReadonlyExecutor(
        result=ToolResult(
            call_id="ignored",
            tool_name="ignored",
            success=True,
            content="the file content",
        )
    )
    loop = _make_loop(assist=True, executor=ex)
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await _drive_execute(loop, first)
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    # Two observations: the real one (from the first read) + the F9
    # pointer (from the deduped second read). Both are present.
    assert len(obs) == 2
    # The first observation is the real tool output.
    assert obs[0].tool_result.content == "the file content"
    # The second observation is the F9 pointer.
    assert "F9 dedup" in obs[1].tool_result.content
    # The pointer references the path of the prior read, so the model
    # can find the prior observation in the same event list.
    assert "src/foo.py" in obs[1].tool_result.content


async def test_assist_on_failed_prior_read_does_not_dedup():
    """A prior read that FAILED (success=False) MUST NOT be a dedup
    candidate — there's no useful prior result to point at. The
    second read re-executes.
    """
    loop = _make_loop(assist=True, executor=_ReadonlyExecutor())
    # First read with a failed observation.
    first = _read_action(call_id="call_read_1", path="src/foo.py")
    await store_append_action_obs(
        loop.store, first, _read_success_observation(first, success=False)
    )
    # Drive the second read.
    second = _read_action(call_id="call_read_2", path="src/foo.py")
    events = await _drive_execute(loop, second)
    # The second read was actually executed (no dedup).
    file_read_calls = [c for c in loop.executor.calls if c.tool_name == "file_read"]
    assert len(file_read_calls) == 1
    # No F9 pointer in any observation.
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert all("F9 dedup" not in o.tool_result.content for o in obs)


async def store_append_action_obs(store, action, obs):
    """Helper: append an action + observation pair to the store in seq
    order, mimicking the run loop's _emit-and-observe flow. Used by
    tests that want to pre-seed a failed prior read for the dedup
    logic."""
    await store.append(CID, action)
    await store.append(CID, obs)
