"""The Build (Agent) surface composed in the runtime — hermetic, scripted model.

Proves the security gate BITES on a state-changing surface (the headline): a risky
action pauses at WAITING_FOR_CONFIRMATION and does NOT execute until confirmed; confirm
runs exactly it; reject denies without executing. Plus: the kill switch revokes caps +
tears down the sandbox, and the Research surface stays ungated (the ConfirmRisky vs
NeverConfirm difference). Real composition (RouterAgent + DefaultToolExecutor + agent
tools + ProcessSandbox + ConfirmRisky + RuleBasedAnalyzer), fake MODEL only.
"""

from __future__ import annotations

from perpleximanus.agent_server import ConversationRuntime
from perpleximanus.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
)
from perpleximanus.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from perpleximanus.tools import ProcessSandboxService

CID = "c1"


class _ScriptedProvider:
    """A model double whose response varies per call. `steps`: list of
    (text, [ProposedToolCall]); an empty tool-call list means 'finish'."""

    name = "fake"

    def __init__(self, steps) -> None:
        self._steps = list(steps)
        self.calls = 0

    async def complete(self, req, *, model):
        i = min(self.calls, len(self._steps) - 1)
        self.calls += 1
        text, tcs = self._steps[i]
        return CompletionResponse(
            text=text,
            tool_calls=list(tcs),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


def _shell(command: str) -> ProposedToolCall:
    return ProposedToolCall(tool_name="shell", arguments={"command": command})


def _runtime(store: SqliteEventStore, steps) -> ConversationRuntime:
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _ScriptedProvider(steps)})
    return ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


async def _run_to_rest(runtime: ConversationRuntime) -> None:
    """Kick the loop and await it to a terminal-for-now status (WAITING/FINISHED)."""
    runtime.kick(CID)
    task = runtime._tasks.get(CID)
    if task is not None:
        await task


async def _build_convo(store, steps) -> ConversationRuntime:
    store.create_conversation(CID, owner_id="local")
    runtime = _runtime(store, steps)
    runtime.set_surface(CID, "build")
    await store.append(CID, _user("delete the temp directory"))
    return runtime


# A HIGH-risk action (rm -rf → gated) then a finish.
_RISKY = [("removing the dir", [_shell("rm -rf doomed")]), ("done", [])]


# ---- the gate bites (the headline) ------------------------------------------


async def test_risky_action_pauses_and_does_not_execute():
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _RISKY)
    await _run_to_rest(runtime)

    state = await store.get_state(CID)
    events = await store.get_events(CID)
    # Phase 1: proposed + waiting, the action recorded but NOT run.
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION
    assert state.pending_action_id is not None
    assert any(isinstance(e, ActionEvent) for e in events)
    assert not any(isinstance(e, ObservationEvent) for e in events)  # nothing executed
    # the analyzer's rationale rode along on the proposed action (for the UI, Prompt 4).
    action = next(e for e in events if isinstance(e, ActionEvent))
    assert "risk_assessment" in action.meta


async def test_confirm_executes_exactly_the_pending_action():
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _RISKY)
    await _run_to_rest(runtime)
    gated = await store.get_state(CID)
    assert gated.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION

    await runtime.confirm(CID)  # execute exactly it, then resume
    task = runtime._tasks.get(CID)
    if task is not None:
        await task

    events = await store.get_events(CID)
    observations = [e for e in events if isinstance(e, ObservationEvent)]
    assert len(observations) == 1  # exactly the pending action ran, once
    assert (await store.get_state(CID)).execution_status == ConversationStatus.FINISHED


async def test_reject_denies_without_executing():
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _RISKY)
    await _run_to_rest(runtime)

    await runtime.reject(CID)  # deny, resume without executing
    task = runtime._tasks.get(CID)
    if task is not None:
        await task

    events = await store.get_events(CID)
    assert not any(isinstance(e, ObservationEvent) for e in events)  # never executed
    assert any(isinstance(e, AgentErrorEvent) for e in events)  # denial recorded
    assert (await store.get_state(CID)).execution_status == ConversationStatus.FINISHED


# ---- ConfirmRisky vs NeverConfirm: the surfaces differ ----------------------


async def test_research_surface_is_ungated():
    # Same risky proposal, but a Research conversation never gates (NeverConfirm) and
    # has no tools — so it does NOT pause at WAITING_FOR_CONFIRMATION.
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    runtime = _runtime(store, _RISKY)  # surface defaults to research
    await store.append(CID, _user("delete the temp directory"))
    await _run_to_rest(runtime)

    state = await store.get_state(CID)
    assert state.execution_status != ConversationStatus.WAITING_FOR_CONFIRMATION


# ---- the kill switch --------------------------------------------------------


async def test_kill_switch_revokes_caps_tears_down_sandbox_and_records_stop():
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _RISKY)
    await _run_to_rest(runtime)  # composes the loop + executor (now paused at the gate)

    executor = runtime._executors[CID]
    await runtime.kill(CID)

    assert executor._killed is True  # the executor's kill ran
    assert executor._broker._revoked is True  # capabilities revoked (§6.4)
    assert executor._sandbox._closed is True  # the sandbox session torn down
    # a terminal stop is recorded so subscribers (the UI) see it
    events = await store.get_events(CID)
    assert any(
        isinstance(e, StatusEvent) and e.detail == "killed" for e in events
    )
    # and the executor refuses further work after the kill
    from perpleximanus.core import ToolCall

    res = await executor.execute(ToolCall(tool_name="shell", arguments={"command": "echo hi"}))
    assert res.success is False and res.structured["kind"] == "sandbox_error"
