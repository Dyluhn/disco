"""Shared Agent/UI workflow invocation service tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from disco.agent_server.build_loop_factory import (
    BuildExecutorFactory,
    BuildLoopFactory,
    _workflow_resolution_needed,
)
from disco.agent_server.runtime import ConversationRuntime
from disco.agent_server.workflow_invocation import WorkflowInvocationService
from disco.core import ConversationStatus, StatusEvent, WorkflowInvocationEvent
from disco.core.workflow import (
    PinnedWorkflowInvocationState,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowHandoff,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowRun,
    WorkflowVerify,
    workflow_run_brief,
)


class _Store:
    def __init__(self, instance: WorkflowInstance) -> None:
        self.instance = instance
        self.calls = 0

    def get_instance(self, instance_id: str) -> WorkflowInstance | None:
        self.calls += 1
        return self.instance if instance_id == "wf_one" else None

    def list_instances(self):
        from disco.tools.builtin.workflow_tools import StoredWorkflowInstance

        return [StoredWorkflowInstance(instance_id="wf_one", instance=self.instance)]


def _instance() -> WorkflowInstance:
    definition = WorkflowDefinition(
        name="One",
        card="Do one bounded task.",
        params_model_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        tools=("file_read",),
        policies=WorkflowPolicies(),
        output_contract=WorkflowOutputContract(
            path_template="reports/{query}.md", format="markdown"
        ),
        verify=WorkflowVerify(checks=("file_exists",)),
    )
    return WorkflowInstance(
        owner_id="owner-a",
        definition_digest=definition.digest(),
        definition=definition,
        params={},
        enabled=True,
        approval=WorkflowApproval(
            approved_at="2026-08-25T00:00:00Z",
            approved_by="owner-a",
            surface_shown_digest="surface-1",
        ),
    )


def _service(
    store: _Store,
    *,
    connector_ids: frozenset[str] | None = None,
) -> WorkflowInvocationService:
    return WorkflowInvocationService(
        store,
        mcp_tool_names_getter=lambda: frozenset(),
        surface_digest_getter=lambda instance: "surface-1",
        connector_ids_getter=(lambda: connector_ids) if connector_ids is not None else None,
    )


def test_ready_descriptor_is_callable_without_router_turn() -> None:
    service = _service(_Store(_instance()))
    descriptors = service.ready_descriptors(owner_id="owner-a")
    assert len(descriptors) == 1
    assert descriptors[0].description == "Do one bounded task."
    assert descriptors[0].parameters_schema["required"] == ["query"]


def test_missing_connector_binding_is_not_ready_or_advertised() -> None:
    instance = _instance().model_copy(update={"connector_bindings": {"github": "conn-1"}})
    available = _service(_Store(instance), connector_ids=frozenset({"conn-1"}))
    assert len(available.ready_descriptors(owner_id="owner-a")) == 1

    service = _service(_Store(instance), connector_ids=frozenset())
    assert service.ready_descriptors(owner_id="owner-a") == ()
    readiness = service.readiness_for("wf_one", owner_id="owner-a")
    assert readiness.status == "needs_setup"
    assert "connector_bindings_unavailable" in readiness.blocking_findings

    async def start(_handoff):
        return "conv-1"

    outcome = asyncio.run(
        service.invoke("wf_one", params={"query": "x"}, owner_id="owner-a", start=start)
    )
    assert outcome.started is False
    assert outcome.reason == "workflow_not_ready"


def test_invalid_parameters_return_field_details_and_never_start() -> None:
    service = _service(_Store(_instance()))
    started: list[bool] = []

    async def start(_handoff):
        started.append(True)
        return "conv-1"

    import asyncio

    outcome = asyncio.run(service.invoke("wf_one", params={}, owner_id="owner-a", start=start))
    assert outcome.started is False
    assert outcome.reason == "invalid_parameters"
    assert outcome.parameter_issues[0].field == "query"
    assert started == []


@pytest.mark.asyncio
async def test_start_callback_conflict_is_not_reported_as_started() -> None:
    service = _service(_Store(_instance()))

    async def start(_handoff):
        return None

    outcome = await service.invoke(
        "wf_one",
        params={"query": "x"},
        owner_id="owner-a",
        start=start,
    )
    assert outcome.started is False
    assert outcome.reason == "workflow_start_conflict"
    assert outcome.conversation_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.RUNNING,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.AWAITING_USER_QUESTION,
    ],
)
async def test_clicked_workflow_never_overwrites_active_or_user_gate(status) -> None:
    class _StoreState:
        async def get_state(self, _conversation_id):
            return SimpleNamespace(execution_status=status)

    class _Loops:
        def workflow_invocation_service(self, _conversation_id):
            return object()

    runtime = object.__new__(ConversationRuntime)
    runtime._store = _StoreState()
    runtime._loop_factory = _Loops()

    result = await runtime.invoke_workflow(
        "conv-one",
        "wf_one",
        {"query": "x"},
        owner_id="owner-a",
    )

    assert result.started is False
    assert result.reason == "conversation_not_quiet"


@pytest.mark.asyncio
async def test_ui_and_agent_calls_share_same_handoff_and_brief() -> None:
    service = _service(_Store(_instance()))
    handoffs = []

    async def start(handoff):
        handoffs.append(handoff)
        return "conv-1"

    first = await service.invoke("wf_one", params={"query": "x"}, owner_id="owner-a", start=start)
    second = await service.invoke("wf_one", params={"query": "x"}, owner_id="owner-a", start=start)
    assert first.started and second.started
    assert first.brief == second.brief
    assert first.handoff is not None and first.handoff.stop_current_segment is True
    assert first.handoff.compose_fresh_sealed_workflow_run is True
    assert first.state is not None and first.state.validated_params == {"query": "x"}


def test_toctou_changed_surface_is_rejected_before_start() -> None:
    store = _Store(_instance())
    digest_calls = 0

    def surface_digest(_instance):
        nonlocal digest_calls
        digest_calls += 1
        return "surface-1" if digest_calls < 3 else "surface-2"

    service = WorkflowInvocationService(
        store,
        mcp_tool_names_getter=lambda: frozenset(),
        surface_digest_getter=surface_digest,
    )
    prepared = service.prepare("wf_one", params={"query": "x"}, owner_id="owner-a")
    assert prepared.accepted is False
    assert prepared.reason == "workflow_changed_before_start"


def test_output_path_and_brief_size_fail_before_handoff() -> None:
    service = _service(_Store(_instance()))
    escaped = service.prepare("wf_one", params={"query": "../escape"}, owner_id="owner-a")
    assert escaped.accepted is False
    assert escaped.parameter_issues[0].code == "output_path"

    oversized = service.prepare("wf_one", params={"query": "x" * 17_000}, owner_id="owner-a")
    assert oversized.accepted is False
    assert oversized.parameter_issues[0].code == "brief_size"


def _invocation(status: str) -> WorkflowInvocationEvent:
    instance = _instance()
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_one",
        instance=instance,
        params={"query": "x"},
        surface_digest="surface-1",
        run_id="run-one",
    ).model_copy(update={"status": status})
    return WorkflowInvocationEvent(state=state, status=status)


def test_terminal_workflow_history_does_not_recompose_future_general_turns() -> None:
    class _Loop:
        def __init__(self, workflow_run) -> None:
            self._workflow_run = workflow_run

    assert _workflow_resolution_needed(_invocation("queued"), None) is True
    assert _workflow_resolution_needed(_invocation("completed"), None) is False
    assert _workflow_resolution_needed(_invocation("completed"), _Loop(None)) is False
    assert _workflow_resolution_needed(_invocation("completed"), _Loop(object())) is True


@pytest.mark.asyncio
async def test_workflow_handoff_is_one_atomic_batch_and_preflights_brief() -> None:
    class _StoreSink:
        def __init__(self) -> None:
            self.batches = []
            self.events_by_conversation = {}

        async def append_many(self, conversation_id, events):
            self.batches.append((conversation_id, events))
            self.events_by_conversation.setdefault(conversation_id, []).extend(events)
            return events

        async def get_events(self, conversation_id):
            return list(self.events_by_conversation.get(conversation_id, ()))

        async def get_state(self, _conversation_id):
            return SimpleNamespace(execution_status=ConversationStatus.IDLE)

    class _Loop:
        def __init__(self) -> None:
            self._pause_requested = asyncio.Event()

    class _Loops:
        def __init__(self, loop) -> None:
            self._loop = loop

        def loop(self, _conversation_id):
            return self._loop

    class _Workspace:
        def __init__(self) -> None:
            self._locks = {}
            self.fence_entries = 0

        def lock(self, conversation_id):
            return self._locks.setdefault(conversation_id, asyncio.Lock())

        @asynccontextmanager
        async def fence(self, conversation_id):
            async with self.lock(conversation_id):
                self.fence_entries += 1
                yield

        async def append_transition_batch_locked(self, conversation_id, events, **_kwargs):
            return await sink.append_many(conversation_id, events)

        @asynccontextmanager
        async def interprocess_mutation_fence(self, _conversation_id):
            self.fence_entries += 1
            yield

    instance = _instance()
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_one",
        instance=instance,
        params={"query": "x"},
        surface_digest="surface-1",
        run_id="run-one",
    )
    handoff = WorkflowHandoff(
        run_id=state.run_id,
        instance_id="wf_one",
        state=state,
        workflow_run=WorkflowRun(
            run_id=state.run_id,
            definition=instance.definition,
            params=state.validated_params,
        ),
        brief=workflow_run_brief(instance.definition, state.validated_params),
    )
    sink, loop = _StoreSink(), _Loop()
    factory = object.__new__(BuildExecutorFactory)
    factory._event_store = sink
    factory._loops = _Loops(loop)
    factory._workspace = _Workspace()
    assert await factory._start_workflow_handoff("conv-one", handoff) == "conv-one"
    assert len(sink.batches) == 1
    _, events = sink.batches[0]
    assert len(events) == 3
    assert isinstance(events[0], WorkflowInvocationEvent)
    assert isinstance(events[-1], StatusEvent)
    assert events[-1].status is ConversationStatus.PAUSED
    assert events[-1].detail == "workflow_handoff"

    # The tab can target a quiet Agent conversation with no broad loop bound.
    # It persists the same boundary and lets the supervisor compose sealed.
    factory._loops = _Loops(None)
    assert (
        await factory._start_workflow_handoff(
            "conv-two",
            handoff,
            require_active_loop=False,
        )
        == "conv-two"
    )
    assert len(sink.batches) == 2

    # Same-conversation entry is serialized under the host's existing lock;
    # exactly one pinned handoff can win.
    results = await asyncio.gather(
        factory._start_workflow_handoff(
            "conv-three", handoff, require_active_loop=False
        ),
        factory._start_workflow_handoff(
            "conv-three", handoff, require_active_loop=False
        ),
    )
    assert sorted(result is None for result in results) == [False, True]
    assert [cid for cid, _events in sink.batches].count("conv-three") == 1
    assert factory._workspace.fence_entries == 4

    with pytest.raises(RuntimeError, match="no active Agent loop"):
        await factory._start_workflow_handoff("conv-two", handoff)
    assert len(sink.batches) == 3

    too_large_state = state.model_copy(update={"validated_params": {"query": "x" * 17_000}})
    too_large = handoff.model_copy(
        update={
            "state": too_large_state,
            "workflow_run": handoff.workflow_run.model_copy(
                update={"params": too_large_state.validated_params}
            ),
            "brief": workflow_run_brief(
                instance.definition,
                too_large_state.validated_params,
            ),
        }
    )
    with pytest.raises(ValueError, match="brief size limit"):
        await factory._start_workflow_handoff(
            "conv-one",
            too_large,
            require_active_loop=False,
        )
    assert len(sink.batches) == 3


@pytest.mark.asyncio
async def test_sealed_workflow_resolution_rearms_paused_boundary_atomically() -> None:
    instance = _instance()
    state = PinnedWorkflowInvocationState.create(
        instance_id="wf_one",
        instance=instance,
        params={"query": "x"},
        surface_digest="surface-1",
        run_id="run-one",
    )
    invocation = WorkflowInvocationEvent(state=state, status="queued")
    workflow_run = WorkflowRun(
        run_id=state.run_id,
        definition=instance.definition,
        params=state.validated_params,
    )

    class _Events:
        def __init__(self) -> None:
            self.batches: list[list[object]] = []

        async def get_events(self, _conversation_id):
            return [invocation]

        async def append_many(self, _conversation_id, events):
            self.batches.append(events)
            return events

    class _Contexts:
        def begin_compose(self, *_args):
            return None

        def bind_resolved(self, *_args):
            return None

        def end_compose(self, *_args):
            return None

    class _Loops:
        def bind(self, _conversation_id, loop):
            self.bound = loop

    class _Workspace:
        @asynccontextmanager
        async def fence(self, _conversation_id):
            yield

        async def append_transition_batch_locked(self, _conversation_id, events, **_kwargs):
            return await events_store.append_many(_conversation_id, events)

    events = _Events()
    factory = object.__new__(BuildLoopFactory)
    factory._event_store = events
    events_store = events
    factory._workspace = _Workspace()
    factory._composer = SimpleNamespace(
        _mcp=SimpleNamespace(_loop_snapshot=lambda: object()),
        workflow_projection=lambda *_args: SimpleNamespace(
            fail_closed=False,
            workflow_run=workflow_run,
        ),
    )
    factory._contexts = _Contexts()
    factory._settings = SimpleNamespace(
        _surface_of=lambda _cid: "agent",
        _effective_autonomous=lambda _cid: False,
    )
    factory._drivers = SimpleNamespace(router=lambda **_kwargs: object())
    factory._loops = _Loops()
    factory._agent = lambda *_args: object()
    sealed_loop = SimpleNamespace(stream_sink=None)
    factory._compose_surface = lambda *_args, **_kwargs: sealed_loop

    async def discard(_conversation_id):
        return None

    factory._discard_for_recompose = discard
    resolved = SimpleNamespace(model_key="driver", context_window=32_000)

    assert await factory.loop_for_workflow_resolved("conv-one", resolved) is sealed_loop
    assert len(events.batches) == 1
    assert isinstance(events.batches[0][0], WorkflowInvocationEvent)
    assert events.batches[0][0].status == "running"
    assert isinstance(events.batches[0][1], StatusEvent)
    assert events.batches[0][1].status is ConversationStatus.RUNNING
    assert events.batches[0][1].detail == "workflow_started"
