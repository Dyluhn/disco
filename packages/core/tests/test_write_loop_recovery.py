"""Loop-level write-loop recovery — the unrecoverable file_write loop is broken.

Reproduces the live failure (build agent rewriting app.js ~21KB, then thrashing
into a shell-exec heredoc fallback) at the loop seam, and pins the three fixes:

1. The F9 read-dedup short-circuit GROUNDS the read (executor.note_grounding_read)
   so a following file_write of the same path is not gate-refused — the deduped
   pointer IS the current content.
2. The CURRENT WORKSPACE snapshot grounds every path it pins IN FULL, so a write
   of an in-context file is allowed (it is grounded, not blind-from-memory).
3. The K1 elision guard RE-EXPANDS a file_write whose content is purely a
   copied-back elision placeholder, recovering the real content from the event log
   and executing it — instead of dead-ending the model into reproducing tens of KB
   from memory (which re-collides with elision → the loop).

These drive ``_execute_and_observe`` / ``ViewBuilder.build`` directly (the F8/F9
direct-drive pattern) with a spy executor that records grounding + execute calls.
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
from disco.core.events import (
    WORKSPACE_SNAPSHOT_SENTINEL,
    _arg_snip_marker_neutral,
    find_elided_arg_markers,
)
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.loop.dedup import _canonical_path, _f9_dedupable_read, _f9_path_was_mutated_after
from disco.core.loop.engine import AgentLoop
from disco.core.loop.view_render import ViewBuilder
from disco.core.view import NoOpCondenser

CID = "conv"

# asyncio_mode = "auto" (pyproject) auto-collects the `async def` tests; the sync
# P1 helper tests run as plain tests. No module-level asyncio mark (it would wrongly
# mark the sync tests).


# ---------------------------------------------------------------------------
# spies
# ---------------------------------------------------------------------------


class _SpyExecutor:
    """Records execute() calls AND note_grounding_read() calls. file_read returns a
    distinctive body; file_write reports success. Mirrors the FakeExecutor surface
    the loop touches in _execute_and_observe."""

    def __init__(self, *, read_body: str = "REAL FILE BODY") -> None:
        self.sandbox = None
        self.calls: list[ToolCall] = []
        self.grounded: list[str] = []
        self._read_body = read_body

    def available_tools(self):
        return []

    def readonly_tool_names(self):
        return frozenset({"file_read"})

    def note_grounding_read(self, path: str) -> None:
        self.grounded.append(path)

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        if call.tool_name == "file_read":
            return ToolResult(
                call_id=call.call_id,
                tool_name="file_read",
                success=True,
                content=self._read_body,
            )
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content=f"wrote to {call.arguments.get('path')}",
        )


class _PreparationFailExecutor(_SpyExecutor):
    async def prepare_for_events(self, events: list[Event]) -> None:
        raise RuntimeError("workspace authority could not be prepared")


class _NoOpCondenser:
    def should_condense(self, view, *, token_count):
        return None

    async def condense(self, events, view, *, summarizer, reason="tokens", artifact_paths=None):
        return None


class _ScriptedAgent:
    def __init__(self) -> None:
        self.steps: list = []

    async def step(self, *a, **k):  # never stepped in these tests
        raise AssertionError("agent should not be stepped")


class _FakeAnalyzer:
    def assess(self, action):
        from disco.core.security import SecurityRisk

        return SecurityRisk.UNKNOWN


class _NeverConfirm:
    async def confirm(self, *a, **k):
        return True


class _FakeSummarizer:
    async def summarize(self, messages):
        return "summary"


def _make_loop(executor, *, model_policy: ModelExecutionPolicy) -> AgentLoop:
    return AgentLoop(
        CID,
        SqliteEventStore(":memory:"),
        _ScriptedAgent(),
        executor,
        None,
        _FakeAnalyzer(),
        _NeverConfirm(),
        _NoOpCondenser(),
        _FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=model_policy,
    )


async def _drive(loop: AgentLoop, action: ActionEvent) -> list[Event]:
    persisted = await loop.store.append(CID, action)
    await loop._execute_and_observe(persisted)
    return await loop.store.get_events(CID)


async def test_prepare_failure_is_observable_and_prevents_effect() -> None:
    ex = _PreparationFailExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())
    events = await _drive(loop, _write("prepare-fail", "must not land"))
    assert ex.calls == []
    errors = [event for event in events if isinstance(event, AgentErrorEvent)]
    assert len(errors) == 1
    assert errors[0].tool_call_id == "prepare-fail"
    assert "workspace authority could not be prepared" in errors[0].error


def _read(call_id: str, path: str = "app.js") -> ActionEvent:
    return ActionEvent(
        thought="read",
        tool_call=ToolCall(tool_name="file_read", call_id=call_id, arguments={"path": path}),
    )


def _read_obs(action: ActionEvent, body: str = "REAL FILE BODY") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id, tool_name="file_read", success=True, content=body
        ),
        action_id=action.id,
    )


def _write(call_id: str, content: str, path: str = "app.js") -> ActionEvent:
    return ActionEvent(
        thought="write",
        tool_call=ToolCall(
            tool_name="file_write", call_id=call_id, arguments={"path": path, "content": content}
        ),
    )


# ===========================================================================
# (1) F9 dedup grounds the read
# ===========================================================================


async def test_f9_deduped_read_grounds_the_write_path():
    """assist-ON: a second identical file_read is short-circuited to a pointer
    (executor NOT called) — but the dedup now GROUNDS the path so a following
    file_write is not gate-refused. Without this the model can neither read (only a
    pointer) nor write (gated) → the unrecoverable loop."""
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy(tier="weak"))  # assist ON
    # First read executes for real.
    await _drive(loop, _read("r1"))
    assert len(ex.calls) == 1
    assert ex.grounded == []  # a real read sets the bit itself; no grounding needed
    # Second identical read → F9 dedup: executor NOT called again, BUT grounded.
    await _drive(loop, _read("r2"))
    assert len(ex.calls) == 1, "F9 must short-circuit the executor"
    assert ex.grounded == ["app.js"], "deduped read must ground the gate"


# ===========================================================================
# (2) snapshot pinned-in-full grounds writes
# ===========================================================================


class _SnapSandbox:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]


class _SnapExecutor:
    def __init__(self, sandbox) -> None:
        self.sandbox = sandbox
        self.grounded: list[str] = []

    def note_grounding_read(self, path: str) -> None:
        self.grounded.append(path)


class _SnapLoop:
    """Minimal ViewBuilder collaborator (mirrors test_cw4_cw5_truthful_pin)."""

    def __init__(self, *, assist: bool, executor, events: list) -> None:
        self._assist = assist
        self.executor = executor
        self.condenser = NoOpCondenser()
        self.summarizer = None
        self._log: list = []
        for e in events:
            self._append(e)

    def _append(self, event) -> None:
        next_seq = (self._log[-1].seq + 1) if self._log else 1
        self._log.append(event.model_copy(update={"seq": next_seq}))

    def _driver_context_window(self) -> int:
        return 192_000

    def _gate_recitation(self, view, events, *, context_pack_active=False):
        return view

    def _f8_shrink_file_write_args(self, messages, events):
        return messages

    async def _emit(self, event) -> None:
        self._append(event)

    async def _events(self) -> list:
        return list(self._log)


async def test_snapshot_pinned_full_grounds_writes():
    """assist-OFF: a small file the agent wrote is pinned IN FULL in the CURRENT
    WORKSPACE block → the loop grounds it so a re-write is allowed (it is in
    context, not blind-from-memory). A large/truncated file is NOT pinned → NOT
    grounded → the gate still requires a real read."""
    sbx = _SnapSandbox({"app.js": b"const x = 1;\n", "big.js": b"X" * 80_000})
    ex = _SnapExecutor(sbx)
    # The agent has touched both files (mutating actions put them in the working set).
    events = [
        _write("w_small", "const x = 1;\n", path="app.js"),
        _write("w_big", "X" * 80_000, path="big.js"),
    ]
    loop = _SnapLoop(assist=False, executor=ex, events=events)
    view = await ViewBuilder(loop).build(loop._log)
    # The snapshot is present and pins the small file in full.
    snap = next(
        (
            m
            for m in view.messages
            if m.role == "user" and m.content.startswith(WORKSPACE_SNAPSHOT_SENTINEL)
        ),
        None,
    )
    assert snap is not None
    # app.js is small → pinned in full → grounded. big.js is truncated → NOT grounded.
    assert "app.js" in ex.grounded
    assert "big.js" not in ex.grounded


# ===========================================================================
# (3) K1 recovery — re-expand an elided file_write content
# ===========================================================================


def _marker_for(content: str) -> str:
    """The neutral elision placeholder the assist-OFF model SEES (and copies back)
    for an over-long arg — the exact shape captured in the live failure."""
    return _arg_snip_marker_neutral(len(content))


async def test_k1_recovery_reexpands_accepted_prior_write():
    """The legit path: the prior file_write was ACCEPTED (executed → successful
    ObservationEvent). When the model copies the placeholder back, the real content
    is recovered from that accepted write, re-expanded + grounded, and EXECUTES —
    no rejection, no loop."""
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())  # assist OFF
    real_content = "A" * 23_462  # the original large body
    marker = _marker_for(real_content)
    assert marker != real_content and find_elided_arg_markers({"content": marker})
    # A prior ACCEPTED file_write of app.js: drive it so it gets a SUCCESS
    # observation (confirmed) — the only kind of write K1 may recover from.
    await _drive(loop, _write("w_orig", real_content, path="app.js"))
    # Now the copy-back: content is PURELY the placeholder.
    events = await _drive(loop, _write("w_copy", marker, path="app.js"))
    # No K1 rejection was emitted for the copy-back action.
    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "w_copy"]
    assert errs == [], f"K1 should have RECOVERED, not rejected: {errs}"
    # The executor ran the copy-back write with the RE-EXPANDED real content.
    copy_call = next(c for c in ex.calls if c.call_id == "w_copy")
    assert copy_call.tool_name == "file_write"
    assert copy_call.arguments["content"] == real_content
    # And the path was grounded so the gate would allow it.
    assert "app.js" in ex.grounded
    # A success observation exists for the recovered write.
    obs = [
        e for e in events if isinstance(e, ObservationEvent) and e.tool_result.call_id == "w_copy"
    ]
    assert obs and obs[0].tool_result.success is True


async def test_k1_recovery_refuses_to_resurrect_a_rejected_blind_write():
    """P0 (security, codex): a BLIND file_write the read-before-write gate REJECTED
    still sits in the log with its full `content`. A later pure-marker copy-back must
    NOT recover that never-read body — doing so would re-expand + ground + EXECUTE a
    blind clobber of unread content, defeating the gate. Recovery fails closed: the
    rejection stands and no write executes (the model must do a real file_read)."""
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())
    blind_body = "EVIL BLIND OVERWRITE\n" + "Z" * 9_000
    marker = _marker_for(blind_body)
    # Simulate the rejected blind write: its ActionEvent is in the log (full body),
    # followed by an AgentErrorEvent (read_before_write) — NO success observation.
    blind = _write("w_blind", blind_body, path="secret.js")
    await loop.store.append(CID, blind)
    await loop.store.append(
        CID,
        AgentErrorEvent(
            error="file_write refused: secret.js ... not been read since the last write",
            action_id=blind.id,
            tool_call_id="w_blind",
        ),
    )
    # Now the copy-back of the SAME path's marker.
    events = await _drive(loop, _write("w_copy", marker, path="secret.js"))
    # The blind body must NEVER have been executed.
    assert all(c.arguments.get("content") != blind_body for c in ex.calls), (
        "BLIND-CLOBBER BYPASS: a rejected write's content was re-expanded and executed"
    )
    assert ex.calls == [], "no write may execute — recovery must fail closed"
    assert "secret.js" not in ex.grounded, "a rejected write must not ground the gate"
    # The K1 rejection stands (recoverable only via a real file_read).
    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "w_copy"]
    assert len(errs) == 1
    assert "elision" in errs[0].error


async def test_k1_rejects_when_no_original_to_recover():
    """Pure marker copy-back but NO prior real write of the path in the log → the
    engine cannot recover the content → it falls back to the rejection (recoverable
    via the now-working file_read path), and does NOT execute a marker write."""
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())
    marker = _marker_for("Z" * 5_000)
    events = await _drive(loop, _write("w_copy", marker, path="orphan.js"))
    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "w_copy"]
    assert len(errs) == 1
    assert "elision" in errs[0].error
    assert ex.calls == [], "a marker write must never be executed"


async def test_k1_rejects_when_marker_embedded_in_real_text():
    """A marker EMBEDDED in real text (header + marker + footer) is NOT a clean
    copy-back; re-expansion would silently drop the model's surrounding edits, so
    the guard rejects rather than recover — never executes the marker-bearing write.
    """
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())
    await loop.store.append(CID, _write("w_orig", "B" * 9_000, path="app.js"))
    embedded = "import x\n" + _marker_for("B" * 9_000) + "\nexport default x\n"
    events = await _drive(loop, _write("w_copy", embedded, path="app.js"))
    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "w_copy"]
    assert len(errs) == 1, "embedded marker must be rejected, not silently re-expanded"
    assert ex.calls == []


async def test_k1_recovery_ignores_marker_in_non_file_write():
    """A non-file_write tool (e.g. shell) carrying a marker is rejected as before —
    re-expansion is scoped to file_write `content` only."""
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())
    marker = _marker_for("C" * 4_000)
    action = ActionEvent(
        thought="shell",
        tool_call=ToolCall(tool_name="shell", call_id="sh1", arguments={"command": marker}),
    )
    events = await _drive(loop, action)
    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "sh1"]
    assert len(errs) == 1
    assert ex.calls == []


# ===========================================================================
# (4) P1 — F9 invalidation/grounding uses the CANONICAL path (no stale grounding)
# ===========================================================================


def _seq(event: ActionEvent, n: int) -> ActionEvent:
    return event.model_copy(update={"seq": n})


def test_canonical_path_matches_the_gate_canonicalizer():
    """The core F9 canonicalizer must agree with the tools-layer gate
    canonicalizer (files._canonical) — they key the SAME file the same way."""
    from disco.tools.builtin.files import _canonical as _gate_canonical

    for spelling in ("x.py", "./x.py", "workspace/x.py", "/workspace/x.py", "a//b/../x.py"):
        assert _canonical_path(spelling) == _gate_canonical(spelling), spelling


def test_f9_invalidation_canonicalizes_path_spelling():
    """P1: a mutation recorded under a DIFFERENT spelling of the same file
    (canonically equal) MUST invalidate a read tracked under the canonical form —
    otherwise F9 would dedup a STALE read and ground a write against changed
    content."""
    read_path = "x.py"
    for mutated_spelling in ("./x.py", "workspace/x.py", "/workspace/x.py"):
        mut = _seq(_write("m", "new bytes", path=mutated_spelling), 2)
        assert _f9_path_was_mutated_after([mut], read_path, after_seq=1) is True, (
            f"mutation under {mutated_spelling!r} must invalidate a read of {read_path!r}"
        )
    # Control: an unrelated path does NOT invalidate.
    other = _seq(_write("m2", "z", path="y.py"), 2)
    assert _f9_path_was_mutated_after([other], read_path, after_seq=1) is False


def test_f9_dedup_suppressed_when_canonically_same_path_mutated():
    """End-to-end pure-helper proof: read 'x.py' → write './x.py' (same file) → read
    'x.py' again is NOT dedupable → it re-executes (a real read), so F9 never grounds
    a stale write. Raw '==' would have wrongly deduped + grounded."""
    first_read = _read("c1", path="x.py")
    write_diff_spelling = _write("c2", "changed", path="./x.py")
    second_read = _read("c3", path="x.py")  # the current action (last in the list)
    events = [
        _seq(first_read, 1),
        _read_obs(first_read).model_copy(update={"seq": 2}),
        _seq(write_diff_spelling, 3),
        second_read.model_copy(update={"seq": 4}),
    ]
    deduped, prior_id, pointer = _f9_dedupable_read(
        "file_read", {"path": "x.py"}, events, readonly_names=frozenset({"file_read"})
    )
    assert deduped is False, "a write under a canonically-same path must block the dedup"
    assert prior_id == "" and pointer == ""


async def test_k1_redirects_marker_write_to_scaffolded_read_only_path():
    """Counted seed 440023: the marker stood for an ELIDED READ RESULT of a
    scaffolded file (npm create vite authored it; the model only ever READ it).
    There is no authored content to re-expand and writing the file's own bytes
    back would fabricate a revision that never happened — so the guard REDIRECTS
    (message, no execution, no (action, error) pair) instead of marching a
    mid-recovery model to the frozen thrash boundary with repeated identical
    rejections."""
    from disco.core.events import EventSource, MessageEvent

    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())
    # A CONFIRMED prior read of the scaffolded file (action + success observation).
    read = ActionEvent(
        thought="read",
        tool_call=ToolCall(
            tool_name="file_read", call_id="r_scaffold", arguments={"path": "src/App.css"}
        ),
    )
    await loop.store.append(CID, read)
    await loop.store.append(
        CID,
        ObservationEvent(
            action_id=read.id,
            tool_result=ToolResult(
                call_id="r_scaffold", tool_name="file_read", success=True, content="B" * 2_891
            ),
        ),
    )
    marker = _marker_for("B" * 2_891)
    events = await _drive(loop, _write("w_copy", marker, path="src/App.css"))

    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "w_copy"]
    assert errs == [], "a read-provenance marker write must REDIRECT, not error"
    assert ex.calls == [], "a marker write must never execute"
    redirects = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "elision placeholder" in (e.message.content or "")
        and "src/App.css" in (e.message.content or "")
    ]
    assert redirects, "the redirect system-reminder must name the path and the recovery"


async def test_k1_marker_write_without_read_or_write_provenance_still_errors():
    """Fail-closed control (unchanged semantics): a marker write to a path the
    model neither wrote nor read keeps the hard rejection."""
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())
    marker = _marker_for("Z" * 4_000)
    events = await _drive(loop, _write("w_copy", marker, path="untouched.js"))
    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "w_copy"]
    assert len(errs) == 1
    assert "elision" in errs[0].error
    assert ex.calls == []
