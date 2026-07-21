"""CXT live wiring: ContextPack prompt insertion, snip-first pressure, marks."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    CondensationRequest,
    ContextResolvedEvent,
    ContextSummaryEvent,
    Event,
    EventSource,
    LLMMessage,
    NoOpCondenser,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    SqliteEventStore,
    ToolCall,
    ToolResult,
    View,
)
from disco.core.context import (
    ArtifactMemoryStore,
    CompactionPolicy,
    Severity,
    VerifierFailureRef,
    context_mark_resolved,
    context_write_summary,
)
from disco.core.inspect import install as install_inspect
from disco.core.inspect import registry as inspect_registry
from disco.core.llm import OperatingMode
from disco.core.loop.engine import AgentLoop
from disco.core.loop.view_render import ViewBuilder
from event_fakes import action, observation, user_msg, with_seqs
from loop_fakes import FakeAnalyzer, FakeExecutor, FakeSummarizer, NeverConfirm, ScriptedAgent

CID = "conv"


class _MemFS:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _make_loop(*, sandbox: _MemFS | None = None, cadence: int = 1) -> AgentLoop:
    executor = FakeExecutor()
    if sandbox is not None:
        executor.sandbox = sandbox
    return AgentLoop(
        CID,
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        recitation_cadence=cadence,
    )


def _plan(summary: str = "Ship newest") -> PlanEvent:
    return PlanEvent(summary=summary, steps=[PlanStep(title="Add auth")], revision=2)


@pytest.mark.asyncio
async def test_context_pack_flag_off_is_viewbuilder_byte_identical(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "off")
    monkeypatch.delenv("PMX_CONTEXT_PACK", raising=False)
    events = with_seqs([user_msg("build it"), _plan()])

    loop = _make_loop(cadence=1)
    # These are pure ViewBuilder projection tests.  The loop-level
    # `_materialize_view` boundary now intentionally re-reads the authoritative
    # event store under the workspace fence, so synthetic event lists belong at
    # the projection seam instead of that admission seam.
    view = await ViewBuilder(loop).build(events)

    expected_loop = _make_loop(cadence=1)
    expected = expected_loop._gate_recitation(View.of(events), events)
    assert view.fingerprint() == expected.fingerprint()
    assert "<context-pack>" not in "\n".join(m.content for m in view.messages)


@pytest.mark.asyncio
async def test_context_pack_flag_on_prefix_and_narrow_recitation(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "on")
    fs = _MemFS()
    store = ArtifactMemoryStore(fs)
    await store.seed_todo("- [ ] 1. Add auth")
    await store.write_design_direction("## Design Direction: Dark Glass\n- ID: dark-glass")
    await store.record_verifier_failures(
        (
            VerifierFailureRef(
                kind="verify_web_app",
                message="blank render",
                severity=Severity.BLOCKER,
            ),
        )
    )
    events = with_seqs([user_msg("build it"), _plan()])

    loop = _make_loop(sandbox=fs, cadence=1)
    view = await ViewBuilder(loop).build(events)
    contents = [m.content for m in view.messages]
    blob = "\n".join(contents)

    assert blob.count("<context-pack>") == 1
    pack_idx = next(i for i, c in enumerate(contents) if c.startswith("<context-pack>"))
    assert pack_idx == 1
    assert "Goal (v2): Ship newest" in contents[pack_idx]
    assert contents[pack_idx].count("## Design Direction: Dark Glass") == 1
    assert "Todo:" in contents[pack_idx]
    assert "blank render" in contents[pack_idx]
    assert blob.count("Ship newest") == 1

    recaps = [c for c in contents if c.startswith("<current-objective>")]
    assert len(recaps) == 1
    assert "Current step: 1. Add auth" in recaps[0]
    assert "Goal:" not in recaps[0]
    assert "Plan progress" not in recaps[0]


@pytest.mark.asyncio
async def test_context_pack_inclusion_is_visible_to_inspect(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "on")
    monkeypatch.setenv("DISCO_INSPECT", "1")
    reg = install_inspect(inspect_registry())
    reg.clear()
    events = with_seqs([user_msg("build it"), _plan()])

    loop = _make_loop(cadence=1)
    await ViewBuilder(loop).build(events)

    trace = reg.snapshot(CID)
    assert trace is not None
    assert any(
        span.get("span") == "context_pack" and span.get("included") is True
        for span in trace["spans"]
    )


class _SnipFirstCondenser:
    def __init__(self) -> None:
        self.should_calls = 0
        self.condense_calls = 0
        self.forgotten_seen: list[int] = []

    def should_condense(self, view: View, *, token_count: int | None):
        self.should_calls += 1
        self.forgotten_seen.append(view.forgotten_count)
        if view.forgotten_count:
            return None
        return CondensationRequest(soft=True, reason="tokens")

    async def condense(
        self,
        events: list[Event],
        view: View,
        *,
        summarizer,
        reason: str = "tokens",
        artifact_paths: list[str] | None = None,
    ):
        self.condense_calls += 1
        raise AssertionError("snips should free enough context before condenser fallback")


class _ViewLoop:
    def __init__(self, events: list[Event], condenser: _SnipFirstCondenser) -> None:
        self._log = list(events)
        self._assist = False
        self.executor = SimpleNamespace(sandbox=None)
        self.condenser = condenser
        self.summarizer = FakeSummarizer()
        self.conversation_id = "snips"
        self._context_compaction_policy = CompactionPolicy(max_history_chars=100)

    async def _emit(self, event: Event):
        persisted = event.model_copy(update={"seq": len(self._log) + 1})
        self._log.append(persisted)
        return persisted

    async def _events(self) -> list[Event]:
        return list(self._log)

    def _driver_context_window(self) -> None:
        return None

    def _gate_recitation(
        self, view: View, events: list[Event], *, context_pack_active: bool = False
    ) -> View:
        return view

    def _f8_shrink_file_write_args(
        self, messages: list[LLMMessage], events: list[Event]
    ) -> list[LLMMessage]:
        return messages


@pytest.mark.asyncio
async def test_context_snips_fire_before_condenser_and_are_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "on")
    current = action(tool="shell", args={"command": "echo keep"})
    current_obs = observation(action_id=current.id, content="ok")
    base = with_seqs(
        [
            user_msg("head"),
            user_msg("drop " + ("x" * 600)),
            user_msg("drop " + ("y" * 600)),
            current,
            current_obs,
        ]
    )
    events = [
        *base,
        context_mark_resolved(2, 3, range_id="old"),
        context_write_summary("old", ".disco/context/summary/old.md", "old summary"),
        context_mark_resolved(4, 5, range_id="current"),
        context_write_summary("current", ".disco/context/summary/current.md", "current summary"),
    ]
    cond = _SnipFirstCondenser()
    loop = _ViewLoop(events, cond)

    await ViewBuilder(cast(AgentLoop, loop)).build(loop._log)
    tombs = [e for e in loop._log if isinstance(e, CondensationEvent)]
    assert [(t.forgotten_start_seq, t.forgotten_end_seq) for t in tombs] == [(2, 3)]
    assert cond.forgotten_seen == [0, 2]
    assert cond.condense_calls == 0

    await ViewBuilder(cast(AgentLoop, loop)).build(loop._log)
    tombs_again = [e for e in loop._log if isinstance(e, CondensationEvent)]
    assert tombs_again == tombs


async def _seed_progress_events(
    store: SqliteEventStore, *, with_failure: bool = False
) -> tuple[AgentLoop, ActionEvent, _MemFS]:
    fs = _MemFS()
    loop = _make_loop(sandbox=fs)
    loop.store = store
    await store.append(CID, user_msg("build"))
    await store.append(CID, _plan(summary="Build auth"))
    write = await store.append(
        CID,
        ActionEvent(
            thought="write auth",
            tool_call=ToolCall(
                tool_name="file_write",
                arguments={"path": "auth.py", "content": "ok"},
            ),
        ),
    )
    if with_failure:
        await store.append(CID, AgentErrorEvent(error="failed", action_id=write.id))
    else:
        await store.append(
            CID,
            ObservationEvent(
                action_id=write.id,
                tool_result=ToolResult(
                    call_id=write.tool_call.call_id,
                    tool_name="file_write",
                    success=True,
                    content="wrote auth.py",
                ),
            ),
        )
    done = await store.append(
        CID,
        ActionEvent(
            thought="done",
            tool_call=ToolCall(
                tool_name="update_plan_progress",
                arguments={"steps": [{"index": 1, "state": "done"}]},
            ),
        ),
    )
    await store.append(
        CID,
        ObservationEvent(
            action_id=done.id,
            tool_result=ToolResult(
                call_id=done.tool_call.call_id,
                tool_name="update_plan_progress",
                success=True,
                content="ok",
            ),
        ),
    )
    return loop, done, fs


@pytest.mark.asyncio
async def test_plan_step_transition_emits_mark_and_summary(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "on")
    store = SqliteEventStore(":memory:")
    loop, done, fs = await _seed_progress_events(store)

    await loop._maybe_emit_plan_step_done_condition_note(done)
    events = await store.get_events(CID)
    marks = [e for e in events if isinstance(e, ContextResolvedEvent)]
    summaries = [e for e in events if isinstance(e, ContextSummaryEvent)]

    assert len(marks) == 1
    assert marks[0].reason == "plan_step_done"
    assert marks[0].source == EventSource.SYSTEM
    assert len(summaries) == 1
    assert summaries[0].rel_path.startswith(".disco/context/summary/")
    assert (
        "Step 'Add auth' completed; 1 tool call, last: file_write auth.py." in summaries[0].summary
    )
    assert fs.files[summaries[0].rel_path].decode("utf-8") == summaries[0].summary + "\n"


@pytest.mark.asyncio
async def test_plan_step_transition_does_not_mark_failure_spans(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "on")
    store = SqliteEventStore(":memory:")
    loop, done, _fs = await _seed_progress_events(store, with_failure=True)

    await loop._maybe_emit_plan_step_done_condition_note(done)
    events = await store.get_events(CID)
    assert not any(isinstance(e, ContextResolvedEvent) for e in events)
    assert not any(isinstance(e, ContextSummaryEvent) for e in events)


@pytest.mark.asyncio
async def test_plan_step_transition_marks_with_unnumbered_in_memory_action(
    monkeypatch,
) -> None:
    """Live shape: the loop passes the IN-MEMORY action (seq=None — the store
    assigns seq on append). The hook must resolve the persisted copy by id or
    every mark is silently skipped (the bug the first pack-on soak caught)."""
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "on")
    store = SqliteEventStore(":memory:")
    loop, done, _fs = await _seed_progress_events(store)

    unnumbered = done.model_copy(update={"seq": None})
    assert unnumbered.seq is None

    await loop._maybe_emit_plan_step_done_condition_note(unnumbered)
    events = await store.get_events(CID)
    assert any(isinstance(e, ContextResolvedEvent) for e in events)
    assert any(isinstance(e, ContextSummaryEvent) for e in events)
