"""Blocked-question answers resume the current blocked landing.

These tests drive the production DiscoKernel ingress plus the real plan-gated
AgentLoop with a scripted provider. The regression was that a blocked-landing
answer sent as a steer was stamped as a revision and forced a fresh planning
gate instead of letting the approved execution segment continue.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

from disco.agent_server.build_kernel.disco_kernel import DiscoKernel
from disco.core import (
    ActionEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    NoOpCondenser,
    PlanEvent,
    SqliteEventStore,
    StatusEvent,
    ToolResult,
    WorkspaceMutationEvent,
)
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    DefaultLLMRouter,
    DriverPrompts,
    ModelEntry,
    OperatingMode,
    ProposedToolCall,
    Requirement,
    RouterConfig,
    StreamChunk,
    TokenUsage,
    ToolSpec,
)
from disco.core.loop import AgentLoop, BuildAgent, NeverConfirm, NullSecurityAnalyzer

CID = "blocked-answer-resume"
PLANNING_TOOLS = frozenset({"submit_plan", "file_list", "file_read", "search", "extract", "think"})


class _SequenceProvider:
    name = "seq"

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.calls = 0
        self.seen: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        self.seen.append(req)
        i = self.calls
        self.calls += 1
        spec = self._script[min(i, len(self._script) - 1)]
        tool_calls = spec.get("tool_calls", [])
        return CompletionResponse(
            text=spec.get("text", ""),
            tool_calls=tool_calls,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="tool_calls" if tool_calls else "stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req: CompletionRequest, *, model: str):
        final = await self.complete(req, model=model)
        yield StreamChunk(delta_text=final.text)
        yield StreamChunk(done=True, final=final)

    def supports(self, requirement: Requirement, *, model: str) -> bool:
        return True


class _BuildExecutor:
    def __init__(self) -> None:
        names = [
            "submit_plan",
            "file_read",
            "file_list",
            "search",
            "extract",
            "think",
            "file_write",
            "plan_step",
            "shell",
        ]
        self._tools = [
            ToolSpec(name=name, description=name, parameters_schema={}) for name in names
        ]
        self.calls = []
        self.world: dict[str, str] = {}

    def available_tools(self):
        return self._tools

    async def execute(self, call) -> ToolResult:
        self.calls.append(call)
        if call.tool_name == "file_write":
            path = str(call.arguments.get("path") or "")
            if path:
                self.world[path] = str(call.arguments.get("content") or "")
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="ok",
        )


class _Summarizer:
    async def summarize(self, messages):
        return "summary"


def _router(provider: _SequenceProvider) -> DefaultLLMRouter:
    config = RouterConfig(
        models={
            "local": ModelEntry(
                model_id="local-driver-q4",
                provider="ollama",
                context_window=65_536,
                capabilities=frozenset({Requirement.TOOL_CALLING, Requirement.JSON_MODE}),
                family="qwen",
            )
        },
        default_model="local",
        assignments={},
    )
    return DefaultLLMRouter(
        config,
        {"ollama": provider},
        prompt_provider=DriverPrompts(),
    )


def _tool(name: str, arguments: dict) -> ProposedToolCall:
    return ProposedToolCall(tool_name=name, arguments=arguments)


def _submit_plan(title: str = "Create index.html") -> ProposedToolCall:
    return _tool(
        "submit_plan",
        {"summary": "Build the page.", "steps": [{"title": title}]},
    )


def _write_index() -> ProposedToolCall:
    return _tool("file_write", {"path": "index.html", "content": "<main>ok</main>"})


def _mark_step_done() -> ProposedToolCall:
    return _tool("plan_step", {"index": 1, "state": "done"})


def _finish() -> ProposedToolCall:
    return _tool("finish", {"summary": "done"})


def _propose_plan_update() -> ProposedToolCall:
    return _tool(
        "propose_plan_update",
        {"summary": "Revised page.", "steps": [{"title": "Revise index.html"}]},
    )


def _make_loop(cid: str, script: list[dict]):
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local")
    provider = _SequenceProvider(script)
    executor = _BuildExecutor()
    loop = AgentLoop(
        cid,
        store,
        BuildAgent(_router(provider), conversation_id=cid),
        executor,
        None,
        NullSecurityAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        _Summarizer(),
        mode=OperatingMode.PLANNING,
        planning_tools=PLANNING_TOOLS,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    return loop, store, provider, executor


async def _plan_approve_and_land_blocked(loop: AgentLoop) -> None:
    await loop.send_message("build a page")
    assert (await loop.run()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    await loop._land_blocked(
        reason="actionless",
        legacy_status=ConversationStatus.PAUSED,
        legacy_detail="actionless",
    )


async def _answer_with_kernel(
    store: SqliteEventStore, cid: str, text: str, *, steer: bool = True
) -> None:
    lock = asyncio.Lock()
    runtime = types.SimpleNamespace(
        _store=store,
        kick=MagicMock(),
        workspace_lock=lambda _conversation_id: lock,
        _workspace=MagicMock(),
    )

    @asynccontextmanager
    async def process_fence(_conversation_id: str) -> AsyncIterator[None]:
        yield

    async def append_run_ingress(
        conversation_id: str,
        events: list[Event],
        source: str,
    ) -> list[Event]:
        return await store.append_many(
            conversation_id,
            [*events, WorkspaceMutationEvent(operation=f"agent.run-intent.{source}")],
        )

    async def append_transition_batch_locked(
        conversation_id: str,
        events: list[Event],
        **_kwargs,
    ) -> list[Event]:
        return await store.append_many(conversation_id, events)

    runtime._workspace.interprocess_mutation_fence = process_fence
    runtime._workspace.append_run_ingress_locked = append_run_ingress
    runtime._lifecycle_commands = MagicMock()
    runtime._lifecycle_commands.append_transition_batch_locked = append_transition_batch_locked
    await DiscoKernel(runtime).send_user_turn(cid, text, steer=steer)


def _statuses(events, *, detail: str | None = None, status: ConversationStatus | None = None):
    result = [e for e in events if isinstance(e, StatusEvent)]
    if detail is not None:
        result = [e for e in result if e.detail == detail]
    if status is not None:
        result = [e for e in result if e.status == status]
    return result


def _plans(events) -> list[PlanEvent]:
    return [e for e in events if isinstance(e, PlanEvent)]


def _actions(events) -> list[ActionEvent]:
    return [e for e in events if isinstance(e, ActionEvent)]


def _request_contains(req: CompletionRequest, text: str) -> bool:
    return any(text in (message.content or "") for message in req.messages)


async def test_blocked_landing_answer_resumes_execution_with_answer_in_context():
    cid = f"{CID}-exec"
    answer = "Continue with the approved plan and build step 1."
    loop, store, provider, executor = _make_loop(
        cid,
        [
            {"text": "plan", "tool_calls": [_submit_plan()]},
            {"text": "write", "tool_calls": [_write_index()]},
            {"text": "done", "tool_calls": [_mark_step_done()]},
            {"text": "finish", "tool_calls": [_finish()]},
        ],
    )
    await _plan_approve_and_land_blocked(loop)

    before = await store.get_events(cid)
    blocked_seq = _statuses(before, status=ConversationStatus.AWAITING_USER_QUESTION)[-1].seq
    plan_count = len(_plans(before))
    approval_gate_count = len(_statuses(before, status=ConversationStatus.AWAITING_PLAN_APPROVAL))

    await _answer_with_kernel(store, cid, answer)
    after_answer = await store.get_events(cid)
    assert not _statuses(after_answer, detail="revision_steer_pending")

    state = await loop.run()
    events = await store.get_events(cid)

    first_resume_request = provider.seen[1]
    assert first_resume_request.profile.mode == OperatingMode.LONG_HORIZON
    assert _request_contains(first_resume_request, answer)
    assert "index.html" in executor.world
    assert any(a.tool_call and a.tool_call.tool_name == "file_write" for a in _actions(events))
    assert state.execution_status == ConversationStatus.FINISHED
    assert len(_plans(events)) == plan_count
    assert len(_statuses(events, status=ConversationStatus.AWAITING_PLAN_APPROVAL)) == (
        approval_gate_count
    )
    assert not [
        e
        for e in _statuses(events, detail="planning")
        if blocked_seq is not None and (e.seq or 0) > blocked_seq
    ]


async def test_blocked_answer_still_allows_model_initiated_plan_update():
    cid = f"{CID}-model-plan-update"
    answer = "Continue with the approved plan and build step 1."
    loop, store, provider, _executor = _make_loop(
        cid,
        [
            {"text": "plan", "tool_calls": [_submit_plan()]},
            {"text": "inspect", "tool_calls": [_tool("file_read", {"path": "index.html"})]},
            {"text": "revise", "tool_calls": [_propose_plan_update()]},
        ],
    )
    await _plan_approve_and_land_blocked(loop)

    await _answer_with_kernel(store, cid, answer)
    state = await loop.run()
    events = await store.get_events(cid)

    assert provider.seen[1].profile.mode == OperatingMode.LONG_HORIZON
    assert _request_contains(provider.seen[1], answer)
    assert not _statuses(events, detail="revision_steer_pending")
    assert [plan.revision for plan in _plans(events)] == [1, 2]
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_non_blocked_user_question_answer_keeps_revision_steer_marker():
    cid = f"{CID}-plain-question"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid, owner_id="local")
    question = await store.append(
        cid,
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content="What should I change?"),
        ),
    )
    await store.append(
        cid,
        StatusEvent(
            status=ConversationStatus.AWAITING_USER_QUESTION,
            detail=question.id,
        ),
    )

    await _answer_with_kernel(
        store,
        cid,
        "Continue with the approved plan and build step 1.",
    )

    assert _statuses(await store.get_events(cid), detail="revision_steer_pending")


async def test_blocked_answer_does_not_override_pending_revision_planning():
    cid = f"{CID}-pending-revision"
    answer = "Continue with the approved plan and build step 1."
    loop, store, provider, _executor = _make_loop(
        cid,
        [
            {"text": "plan", "tool_calls": [_submit_plan()]},
            {"text": "revised plan", "tool_calls": [_submit_plan("Revise index.html")]},
        ],
    )
    await loop.send_message("build a page")
    assert (await loop.run()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    await loop.enter_planning("add a dark mode toggle")
    await loop._land_blocked(
        reason="actionless",
        legacy_status=ConversationStatus.PAUSED,
        legacy_detail="actionless",
    )

    await _answer_with_kernel(store, cid, answer)
    state = await loop.run()
    events = await store.get_events(cid)

    first_resume_request = provider.seen[1]
    assert first_resume_request.profile.mode == OperatingMode.PLANNING
    assert _request_contains(first_resume_request, answer)
    assert not _statuses(events, detail="revision_steer_pending")
    assert [plan.revision for plan in _plans(events)] == [1, 2]
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
