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

import pytest
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
from disco.core.loop.engine import AgentLoop
from disco.core.loop.view_render import ViewBuilder
from disco.core.view import NoOpCondenser

CID = "conv"

pytestmark = pytest.mark.asyncio


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
                call_id=call.call_id, tool_name="file_read", success=True,
                content=self._read_body,
            )
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True,
            content=f"wrote to {call.arguments.get('path')}",
        )


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

    def _gate_recitation(self, view, events):
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
        (m for m in view.messages if m.role == "user" and m.content.startswith(
            WORKSPACE_SNAPSHOT_SENTINEL)),
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


async def test_k1_recovery_reexpands_elided_file_write():
    """The model copies the elision placeholder back as `content`. The real content
    is recovered from the prior successful file_write in the event log, the call is
    re-expanded + grounded, and it EXECUTES — no rejection, no loop."""
    ex = _SpyExecutor()
    loop = _make_loop(ex, model_policy=ModelExecutionPolicy.standard())  # assist OFF
    real_content = "A" * 23_462  # the original large body
    marker = _marker_for(real_content)
    assert marker != real_content and find_elided_arg_markers({"content": marker})
    # History: a prior REAL file_write of app.js (full content in the event log).
    await loop.store.append(CID, _write("w_orig", real_content, path="app.js"))
    # Now the copy-back: content is PURELY the placeholder.
    events = await _drive(loop, _write("w_copy", marker, path="app.js"))
    # No K1 rejection was emitted for the copy-back action.
    errs = [
        e for e in events
        if isinstance(e, AgentErrorEvent) and e.tool_call_id == "w_copy"
    ]
    assert errs == [], f"K1 should have RECOVERED, not rejected: {errs}"
    # The executor ran the write with the RE-EXPANDED real content.
    write_calls = [c for c in ex.calls if c.tool_name == "file_write"]
    assert len(write_calls) == 1
    assert write_calls[0].arguments["content"] == real_content
    # And the path was grounded so the gate would allow it.
    assert "app.js" in ex.grounded
    # A success observation exists for the recovered write.
    obs = [
        e for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.call_id == "w_copy"
    ]
    assert obs and obs[0].tool_result.success is True


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
        tool_call=ToolCall(
            tool_name="shell", call_id="sh1", arguments={"command": marker}
        ),
    )
    events = await _drive(loop, action)
    errs = [e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "sh1"]
    assert len(errs) == 1
    assert ex.calls == []
