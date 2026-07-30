"""WF-6 sealed scheduled workflow runs."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import disco.agent_server.build_loop_factory as build_loop_factory_mod
import httpx
import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.routes.schedules import make_schedules_router
from disco.agent_server.workflow_run_service import WorkflowRunService
from disco.agent_server.workflow_schedule import (
    WorkflowScheduleManager,
    WorkflowScheduleRunRecord,
)
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
    latest_workspace_run_intent,
)
from disco.core.llm import (
    CompletionResponse,
    ConfigStore,
    DefaultLLMRouter,
    ModelEntry,
    ModelRole,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from disco.core.workflow import (
    ScheduleSpec,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutputContract,
    WorkflowPolicies,
    WorkflowVerify,
)
from disco.tools import (
    Capability,
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
)
from disco.tools import (
    SandboxSession as RealSandboxSession,
)
from fastapi import FastAPI


class _RouteTestClient:
    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def get(self, url: str) -> httpx.Response:
        parsed = urlsplit(url)

        async def _send() -> httpx.Response:
            response_started: dict[str, object] = {}
            chunks: list[bytes] = []
            sent_body = False

            async def receive() -> dict[str, object]:
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                if message["type"] == "http.response.start":
                    response_started.update(message)
                elif message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))  # type: ignore[arg-type]

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": parsed.path,
                "raw_path": parsed.path.encode("ascii"),
                "query_string": parsed.query.encode("ascii"),
                "root_path": "",
                "headers": [],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
            await asyncio.wait_for(self._app(scope, receive, send), timeout=10)
            return httpx.Response(
                int(response_started.get("status", 500)),
                headers={
                    k.decode("latin-1"): v.decode("latin-1")
                    for k, v in response_started.get("headers", [])  # type: ignore[union-attr]
                },
                content=b"".join(chunks),
                request=httpx.Request("GET", f"http://testserver{url}"),
            )

        return asyncio.run(_send())


class _ScriptedProvider:
    name = "fake"

    def __init__(self, steps: list[tuple[str, list[ProposedToolCall]]]) -> None:
        self._steps = steps
        self.calls = 0

    async def complete(self, req, *, model):  # noqa: ANN001
        if req.profile.role == ModelRole.SUMMARIZER:
            return CompletionResponse(
                text="Workflow schedule",
                tool_calls=[],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="stop",
                model_used=model,
                request_id=req.request_id,
                routing=None,
            )
        idx = min(self.calls, len(self._steps) - 1)
        self.calls += 1
        text, calls = self._steps[idx]
        return CompletionResponse(
            text=text,
            tool_calls=list(calls),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):  # noqa: ANN001
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):  # noqa: ANN001
        return True


class _MemorySandboxInstance:
    owner_id: str
    conversation_id: str
    spec: SandboxSpec
    workspace_path: None = None

    def __init__(
        self,
        instance_id: str,
        *,
        owner_id: str,
        conversation_id: str,
        spec: SandboxSpec,
    ) -> None:
        self.id = instance_id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec
        self.files: dict[str, bytes] = {}
        self.destroyed = False

    def _check_live(self) -> None:
        if self.destroyed:
            raise SandboxError("memory sandbox is destroyed")

    @staticmethod
    def _clean(path: str) -> str:
        value = path.strip()
        for prefix in ("/workspace/", "workspace/"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
        if value in {"", ".", "/workspace", "workspace"}:
            return ""
        return value.strip("/")

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:  # noqa: ARG002
        self._check_live()
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> bytes:
        self._check_live()
        rel = self._clean(path)
        if rel not in self.files:
            raise FileNotFoundError(rel)
        return self.files[rel]

    async def write_file(self, path: str, data: bytes) -> None:
        self._check_live()
        self.files[self._clean(path)] = data

    async def list_dir(self, path: str) -> list[str]:
        self._check_live()
        rel = self._clean(path)
        if rel in self.files:
            raise NotADirectoryError(rel)
        prefix = f"{rel}/" if rel else ""
        children: set[str] = set()
        for file_path in self.files:
            if prefix and not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix) :] if prefix else file_path
            name = rest.split("/", 1)[0]
            if name:
                children.add(name)
        return sorted(children)

    async def file_exists(self, path: str) -> bool:
        self._check_live()
        return self._clean(path) in self.files

    async def resolve_relpath(self, path: str) -> str:
        self._check_live()
        return self._clean(path)

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:  # noqa: ARG002
        return None

    async def destroy(self) -> None:
        self.destroyed = True


class _MemorySandboxService:
    name = "memory"
    is_production_valid = False

    def __init__(self) -> None:
        self.created = 0
        self.instances: dict[str, _MemorySandboxInstance] = {}

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        self.created += 1
        instance = _MemorySandboxInstance(
            f"mem_{self.created}",
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
        )
        self.instances[instance.id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self.instances.get(instance_id)

    async def healthcheck(self) -> None:
        return None

    async def list_live_instances(self) -> list[str]:
        return []

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        for instance in self.instances.values():
            if instance.conversation_id == conversation_id:
                await instance.destroy()


def _runtime(
    steps: list[tuple[str, list[ProposedToolCall]]],
) -> tuple[ConversationRuntime, SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _ScriptedProvider(steps)})
    runtime = ConversationRuntime(
        store,
        config_store=ConfigStore(
            path=f"/tmp/disco-wf-schedule-test-{uuid.uuid4().hex}.json",
            base_factory=lambda: cfg,
        ),
        router=router,
        sandbox_service=_MemorySandboxService(),
    )
    return runtime, store


def _workflow_runs(runtime: ConversationRuntime) -> WorkflowRunService:
    return cast(WorkflowRunService, runtime._schedule._workflow_runs)


def _workflow_run_control(runtime: ConversationRuntime) -> Any:
    return _workflow_runs(runtime)._execution._run_control


def _workflow_manager(runtime: ConversationRuntime) -> WorkflowScheduleManager:
    manager_runtime = cast(ConversationRuntime, runtime._schedule._runtime_port)
    return WorkflowScheduleManager(manager_runtime)


def _workflow_definition(
    *,
    tools: tuple[str, ...],
    policies: WorkflowPolicies | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="scheduled_workflow",
        card="Produce the scheduled workflow output in the workspace.",
        params_model_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        tools=tools,
        policies=policies or WorkflowPolicies(allows_writes=True),
        output_contract=WorkflowOutputContract(
            path_template="outputs/result.md",
            format="markdown",
        ),
        verify=WorkflowVerify(
            checks=("output_exists",),
            finalizer="ready_for_workflow_output_verification",
        ),
    )


def _save_instance(
    runtime: ConversationRuntime,
    *,
    instance_id: str,
    tools: tuple[str, ...],
    policies: WorkflowPolicies | None = None,
) -> WorkflowInstance:
    defn = _workflow_definition(tools=tools, policies=policies)
    digest = defn.digest()
    instance = WorkflowInstance(
        definition_digest=digest,
        definition=defn,
        params={},
        enabled=True,
        approval=WorkflowApproval(
            approved_at="2026-07-04T12:00:00Z",
            approved_by="user_1",
            surface_shown_digest=digest,
        ),
    )
    root = runtime._projects.current_project_store().root
    assert root is not None
    workflows = root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / f"{instance_id}.json").write_text(
        json.dumps(instance.model_dump(mode="json")),
        encoding="utf-8",
    )
    return instance


def _sandbox_spec_for_run(runtime: ConversationRuntime, run_cid: str) -> SandboxSpec:
    service = cast(_MemorySandboxService, runtime._sandbox._injected_service)
    specs = [
        instance.spec
        for instance in service.instances.values()
        if instance.conversation_id == run_cid
    ]
    assert specs
    return specs[-1]


def _spec(instance_id: str, instance: WorkflowInstance) -> ScheduleSpec:
    return ScheduleSpec(
        instance_id=instance_id,
        instance_digest=instance.definition_digest,
        cron="* * * * *",
    )


def _file_write() -> ProposedToolCall:
    return ProposedToolCall(
        tool_name="file_write",
        arguments={"path": "outputs/result.md", "content": "ok"},
    )


def _submit_plan() -> ProposedToolCall:
    return ProposedToolCall(
        tool_name="submit_plan",
        arguments={
            "summary": "Produce the scheduled output.",
            "steps": [
                {
                    "title": "Write the result file",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "outputs/result.md",
                    },
                }
            ],
        },
    )


def _finish() -> ProposedToolCall:
    return ProposedToolCall(tool_name="finish", arguments={"summary": "done"})


def _needs_input() -> ProposedToolCall:
    return ProposedToolCall(
        tool_name="needs_input",
        arguments={
            "reason": "GitHub connector approval is missing",
            "required_action": "Approve the GitHub connector.",
        },
    )


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_fire_finishes_records_history_and_snapshots() -> None:
    runtime, store = _runtime(
        [
            ("writing output", [_file_write()]),
            ("done", [_finish()]),
        ]
    )
    try:
        instance = _save_instance(runtime, instance_id="wf_ok", tools=("file_write",))
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_ok", instance))

        record = await manager.fire_now(row.schedule_id)

        assert record is not None
        assert record.terminal_state == ConversationStatus.FINISHED.value
        assert record.output_path == "outputs/result.md"
        assert record.verify_verdict == "pass"
        assert manager.list_runs(schedule_id=row.schedule_id)[0].run_cid == record.run_cid
        assert runtime._projects.current_project_store().get(record.run_cid) is not None
        resolved = runtime._driver_contexts.resolved_snapshot(record.run_cid)
        assert resolved is not None
        assert resolved.context_window > 0
        assert runtime._driver_contexts.compose_snapshot(record.run_cid) is None

        executor = runtime._run_resources.executor(record.run_cid)
        assert executor is not None
        callable_names = executor.callable_tool_names()
        assert {"file_write", "skip", "needs_input"} <= callable_names
        assert callable_names.isdisjoint(
            {"list_workflows", "read_workflow_card", "enter_workflow", "draft_workflow"}
        )
        assert callable_names.isdisjoint({"ask_user", "clarify", "questions_v2"})
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_uses_declared_egress_allow() -> None:
    runtime, _store = _runtime(
        [
            ("writing output", [_file_write()]),
            ("done", [_finish()]),
        ]
    )
    try:
        instance = _save_instance(
            runtime,
            instance_id="wf_declared_egress",
            tools=("file_write",),
            policies=WorkflowPolicies(
                allows_writes=True,
                egress_allow=("example.com",),
            ),
        )
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_declared_egress", instance))

        record = await manager.fire_now(row.schedule_id)

        assert record is not None
        spec = _sandbox_spec_for_run(runtime, record.run_cid)
        assert spec.egress_allow == frozenset({"example.com"})
        assert spec.public_web is False
        assert Capability.NETWORK not in spec.permitted
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_empty_egress_stays_fully_denied() -> None:
    runtime, _store = _runtime(
        [
            ("writing output", [_file_write()]),
            ("done", [_finish()]),
        ]
    )
    try:
        instance = _save_instance(runtime, instance_id="wf_empty_egress", tools=("file_write",))
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_empty_egress", instance))

        record = await manager.fire_now(row.schedule_id)

        assert record is not None
        spec = _sandbox_spec_for_run(runtime, record.run_cid)
        assert spec.egress_allow == frozenset()
        assert spec.public_web is False
        assert Capability.NETWORK not in spec.permitted
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_kick_message_has_honest_outcome_rules() -> None:
    runtime, _store = _runtime(
        [
            ("writing output", [_file_write()]),
            ("done", [_finish()]),
        ]
    )
    try:
        instance = _save_instance(runtime, instance_id="wf_kick_rules", tools=("file_write",))
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_kick_rules", instance))

        record = await manager.fire_now(row.schedule_id)

        assert record is not None
        events = await runtime._store.get_events(record.run_cid)
        user_messages = [
            event.message.content
            for event in events
            if isinstance(event, MessageEvent) and event.source == EventSource.USER
        ]
        kick_message = next(
            message
            for message in user_messages
            if message.startswith("Run this sealed workflow schedule.")
        )
        assert "call needs_input with the question, or skip with the reason" in kick_message
        assert "NEVER fabricate results" in kick_message
        assert "NEVER finish with a failure narrative as if the task succeeded" in kick_message
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_schedule_claims_exact_ingress_before_task_can_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store = _runtime(
        [
            ("writing output", [_file_write()]),
            ("done", [_finish()]),
        ]
    )
    append_started = asyncio.Event()
    release_append = asyncio.Event()
    run_started = asyncio.Event()
    conversation: list[str] = []
    original_append = runtime._workspace.append_run_ingress_locked
    original_run = runtime._run_execution.run

    async def blocked_append(
        conversation_id: str,
        events: list[Any],
        source: str,
        *,
        intent: WorkspaceMutationEvent | None = None,
    ) -> list[Any]:
        conversation.append(conversation_id)
        append_started.set()
        await release_append.wait()
        return await original_append(
            conversation_id,
            events,
            source,
            intent=intent,
        )

    async def traced_run(conversation_id: str, loop: Any) -> Any:
        run_started.set()
        events = await runtime._store.get_events(conversation_id)
        user = next(
            event
            for event in events
            if isinstance(event, MessageEvent) and event.source is EventSource.USER
        )
        assert runtime._run_ingress.claimed_user_seq(conversation_id) == user.seq
        return await original_run(conversation_id, loop)

    monkeypatch.setattr(runtime._workspace, "append_run_ingress_locked", blocked_append)
    monkeypatch.setattr(runtime._run_execution, "run", traced_run)
    fire: asyncio.Task[Any] | None = None
    try:
        instance = _save_instance(runtime, instance_id="wf_atomic_ingress", tools=("file_write",))
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_atomic_ingress", instance))

        fire = asyncio.create_task(manager.fire_now(row.schedule_id))
        await asyncio.wait_for(append_started.wait(), timeout=10)
        cid = conversation[0]
        exact_task = runtime._run_registry.task(cid)
        assert exact_task is not None
        assert runtime._workspace.has_run_claim(cid)
        assert not run_started.is_set()

        # An ordinary kick during the blocked atomic append must observe the
        # registered claim/task and leave the sealed task intact.
        runtime.kick(cid)
        assert runtime._run_registry.task(cid) is exact_task
        assert not run_started.is_set()

        release_append.set()
        record = await asyncio.wait_for(fire, timeout=10)
        assert record is not None
        assert record.terminal_state == ConversationStatus.FINISHED.value
        assert run_started.is_set()
    finally:
        release_append.set()
        if fire is not None and not fire.done():
            fire.cancel()
            await asyncio.gather(fire, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_schedule_and_task_factory_never_overwrite_live_task() -> None:
    runtime, _store = _runtime([])
    release_incumbent = asyncio.Event()
    incumbent: asyncio.Task[Any] | None = None
    try:
        instance = _save_instance(runtime, instance_id="wf_incumbent", tools=())
        spec = _spec("wf_incumbent", instance)
        workflow_service = _workflow_runs(runtime)
        setup = await workflow_service._conversation_setup(
            schedule_id="sched_incumbent",
            spec=spec,
            coalesced=False,
            owner_id="local",
        )
        cid = setup.conversation_id
        assert setup.workflow_run is not None
        assert setup.message is not None
        assert setup.failure is None

        incumbent = asyncio.create_task(release_incumbent.wait())
        runtime._run_registry.register_task(cid, cast(Any, incumbent))
        prior_loop = cast(Any, object())
        runtime._loop_registry.bind(cid, prior_loop)

        with pytest.raises(RuntimeError, match="already has a live run task"):
            _workflow_run_control(runtime)._create_run_task(cid, cast(Any, object()))
        assert runtime._run_registry.task(cid) is incumbent

        result = await workflow_service._execution.execute(
            schedule_id="sched_incumbent",
            spec=spec,
            conversation_id=cid,
            fired_at=setup.fired_at,
            workflow_run=setup.workflow_run,
            output_path=setup.output_path,
            message=setup.message,
            coalesced=False,
        )
        assert isinstance(result, WorkflowScheduleRunRecord)
        assert result.terminal_state == ConversationStatus.ERROR.value
        assert runtime._run_registry.task(cid) is incumbent
        assert runtime._loop_registry.loop(cid) is prior_loop
        assert await runtime._store.get_events(cid) == []
    finally:
        release_incumbent.set()
        if incumbent is not None:
            await asyncio.gather(incumbent, return_exceptions=True)
            runtime._run_registry.detach_task_if_owned(cid, cast(Any, incumbent))
        await runtime.aclose()


@pytest.mark.parametrize("ahead_kind", ["host-mutation", "run-intent"])
@pytest.mark.asyncio
async def test_sealed_schedule_refuses_a_newer_durable_head(
    ahead_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store = _runtime([])
    try:
        instance = _save_instance(runtime, instance_id=f"wf_ahead_{ahead_kind}", tools=())
        spec = _spec(f"wf_ahead_{ahead_kind}", instance)
        workflow_service = _workflow_runs(runtime)
        setup = await workflow_service._conversation_setup(
            schedule_id="sched_ahead",
            spec=spec,
            coalesced=False,
            owner_id="local",
        )
        cid = setup.conversation_id
        assert setup.workflow_run is not None
        assert setup.message is not None
        assert setup.failure is None
        ahead = WorkspaceMutationEvent(
            operation=(
                "agent.run-intent.host-mutation"
                if ahead_kind == "run-intent"
                else "host.editor-write"
            ),
            run_protocol_version=1 if ahead_kind == "run-intent" else None,
        )
        await runtime._store.append(cid, ahead)
        run = AsyncMock()
        handoff = AsyncMock()
        monkeypatch.setattr(runtime._run_execution, "run", run)
        monkeypatch.setattr(
            _workflow_run_control(runtime),
            "_rekick_unadmitted_superseding_intent",
            handoff,
        )

        result = await workflow_service._execution.execute(
            schedule_id="sched_ahead",
            spec=spec,
            conversation_id=cid,
            fired_at=setup.fired_at,
            workflow_run=setup.workflow_run,
            output_path=setup.output_path,
            message=setup.message,
            coalesced=False,
        )

        assert isinstance(result, WorkflowScheduleRunRecord)
        assert "lost pristine ingress authority" in (result.error or "")
        run.assert_not_awaited()
        assert runtime._run_registry.task(cid) is None
        assert runtime._loop_registry.loop(cid) is None
        events = await runtime._store.get_events(cid)
        assert events == [ahead.model_copy(update={"seq": 1})]
        # The recovery helper itself decides whether the durable head contains
        # an unadmitted intent; invoking it is harmless for a bare host mutation.
        handoff.assert_awaited_once_with(cid)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_schedule_append_failure_cleans_exact_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store = _runtime([])
    claim_was_visible = False

    async def fail_append(conversation_id: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal claim_was_visible
        claim_was_visible = runtime._workspace.has_run_claim(conversation_id)
        raise RuntimeError("injected schedule append failure")

    monkeypatch.setattr(runtime._workspace, "append_run_ingress_locked", fail_append)
    try:
        instance = _save_instance(runtime, instance_id="wf_append_failure", tools=())
        record = await _workflow_runs(runtime).run(
            schedule_id="sched_append_failure",
            spec=_spec("wf_append_failure", instance),
        )

        assert record.terminal_state == ConversationStatus.ERROR.value
        assert record.error == "injected schedule append failure"
        assert claim_was_visible
        assert runtime._run_registry.task(record.run_cid) is None
        assert runtime._loop_registry.loop(record.run_cid) is None
        assert not runtime._run_resources.has_executor(record.run_cid)
        assert not runtime._workspace.has_run_claim(record.run_cid)
        state = await runtime._store.get_state(record.run_cid)
        assert state.execution_status == ConversationStatus.ERROR
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("authority", ["intent", "view"])
@pytest.mark.asyncio
async def test_post_ingress_schedule_failure_is_owned_by_exact_authority(
    authority: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store = _runtime([])
    admitted_view: list[str] = []

    async def fail_run(conversation_id: str, _loop: Any) -> Any:
        if authority == "view":
            events = await runtime._store.get_events(conversation_id)
            intent = latest_workspace_run_intent(events)
            assert intent is not None
            view_id = f"aview_{uuid.uuid4().hex}"
            await runtime._store.append(
                conversation_id,
                WorkspaceMutationEvent(
                    operation="agent.view-admitted",
                    agent_view_id=view_id,
                    run_intent_id=intent.id,
                    run_protocol_version=1,
                ),
            )
            task = asyncio.current_task()
            assert task is not None
            runtime._run_authorities.bind(
                cast(Any, task),
                agent_view_id=view_id,
                run_intent_id=intent.id,
            )
            admitted_view.append(view_id)
        raise RuntimeError("injected post-ingress failure")

    monkeypatch.setattr(runtime._run_execution, "run", fail_run)
    try:
        instance = _save_instance(runtime, instance_id="wf_typed_failure", tools=())
        record = await _workflow_runs(runtime).run(
            schedule_id="sched_typed_failure",
            spec=_spec("wf_typed_failure", instance),
        )

        events = await runtime._store.get_events(record.run_cid)
        intent = latest_workspace_run_intent(events)
        status = next(
            event
            for event in reversed(events)
            if isinstance(event, StatusEvent) and event.detail == "workflow_schedule_run_failed"
        )
        assert intent is not None
        if authority == "view":
            assert status.agent_view_id == admitted_view[0]
            assert status.run_intent_id is None
        else:
            assert status.run_intent_id == intent.id
            assert status.agent_view_id is None
        assert (await runtime._store.get_state(record.run_cid)).execution_status == (
            ConversationStatus.ERROR
        )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_stale_post_ingress_failure_cannot_poison_newer_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store = _runtime([])
    run_started = asyncio.Event()
    release_run = asyncio.Event()
    conversation: list[str] = []

    async def fail_late(conversation_id: str, _loop: Any) -> Any:
        conversation.append(conversation_id)
        run_started.set()
        await release_run.wait()
        raise RuntimeError("stale schedule task failed")

    handoff = AsyncMock()
    monkeypatch.setattr(runtime._run_execution, "run", fail_late)
    monkeypatch.setattr(
        _workflow_run_control(runtime),
        "_rekick_unadmitted_superseding_intent",
        handoff,
    )
    fire: asyncio.Task[Any] | None = None
    try:
        instance = _save_instance(runtime, instance_id="wf_stale_failure", tools=())
        fire = asyncio.create_task(
            _workflow_runs(runtime).run(
                schedule_id="sched_stale_failure",
                spec=_spec("wf_stale_failure", instance),
            )
        )
        await asyncio.wait_for(run_started.wait(), timeout=10)
        cid = conversation[0]
        newer = WorkspaceMutationEvent(
            operation="agent.run-intent.host-mutation",
            run_protocol_version=1,
        )
        await runtime._store.append(cid, newer)
        release_run.set()
        record = await asyncio.wait_for(fire, timeout=10)

        assert record.error == "stale schedule task failed"
        events = await runtime._store.get_events(cid)
        latest_intent = latest_workspace_run_intent(events)
        assert latest_intent is not None
        assert latest_intent.id == newer.id
        assert all(
            not isinstance(event, StatusEvent) or event.detail != "workflow_schedule_run_failed"
            for event in events
        )
        handoff.assert_awaited_once_with(cid)
    finally:
        release_run.set()
        if fire is not None and not fire.done():
            fire.cancel()
            await asyncio.gather(fire, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_fire_keeps_session_open_until_loop_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store = _runtime(
        [
            ("writing output", [_file_write()]),
            ("done", [_finish()]),
        ]
    )
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    class RecordingSandboxSession(RealSandboxSession):
        instances: list[RecordingSandboxSession] = []

        def __init__(
            self,
            service: SandboxService,
            spec: SandboxSpec | None = None,
            *,
            owner_id: str = "local",
            conversation_id: str = "conv",
            on_recreate: Callable[[], Awaitable[None]] | None = None,
            kernel_idle_timeout_s: float | None = None,
            legacy_auto_preview: bool = True,
        ) -> None:
            super().__init__(
                service,
                spec,
                owner_id=owner_id,
                conversation_id=conversation_id,
                on_recreate=on_recreate,
                kernel_idle_timeout_s=kernel_idle_timeout_s,
                legacy_auto_preview=legacy_auto_preview,
            )
            self.destroy_calls = 0
            RecordingSandboxSession.instances.append(self)

        async def write_file(self, path: str, data: bytes) -> None:
            if not write_started.is_set():
                write_started.set()
                await asyncio.wait_for(release_write.wait(), timeout=10)
            await super().write_file(path, data)

        async def atomic_write(self, path: str, data: bytes) -> None:
            if not write_started.is_set():
                write_started.set()
                await asyncio.wait_for(release_write.wait(), timeout=10)
            await super().atomic_write(path, data)

        async def destroy(self) -> None:
            self.destroy_calls += 1
            await super().destroy()

    monkeypatch.setattr(
        build_loop_factory_mod,
        "SandboxSession",
        RecordingSandboxSession,
    )
    monkeypatch.setenv("DISCO_IDLE_SUSPEND_S", "0")
    fire_task: asyncio.Task[object] | None = None
    try:
        instance = _save_instance(runtime, instance_id="wf_lifecycle", tools=("file_write",))
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_lifecycle", instance))

        fire_task = asyncio.create_task(manager.fire_now(row.schedule_id))
        await asyncio.wait_for(write_started.wait(), timeout=10)
        session = RecordingSandboxSession.instances[-1]

        await store.append(
            session.conversation_id,
            StatusEvent(status=ConversationStatus.IDLE, detail="test idle race"),
        )
        assert await runtime.sweep_idle_once() == 0
        assert session.destroy_calls == 0

        release_write.set()
        record = await asyncio.wait_for(fire_task, timeout=10)

        assert record is not None
        assert session.destroy_calls == 0
    finally:
        release_write.set()
        if fire_task is not None and not fire_task.done():
            fire_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await fire_task
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_autonomous_plan_auto_approves() -> None:
    runtime, _store = _runtime(
        [
            ("planning", [_submit_plan()]),
            ("writing output", [_file_write()]),
            ("done", [_finish()]),
        ]
    )
    try:
        instance = _save_instance(
            runtime,
            instance_id="wf_auto_plan",
            tools=("submit_plan", "file_write"),
        )
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_auto_plan", instance))

        record = await manager.fire_now(row.schedule_id)

        assert record is not None
        assert record.terminal_state == ConversationStatus.FINISHED.value
        events = await runtime._store.get_events(record.run_cid)
        statuses = [event for event in events if isinstance(event, StatusEvent)]
        assert any(status.detail == "plan_approved" for status in statuses)
        assert all(
            status.status != ConversationStatus.AWAITING_PLAN_APPROVAL for status in statuses
        )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_needs_input_lands_terminal_explanation() -> None:
    runtime, _store = _runtime([("blocked", [_needs_input()])])
    try:
        instance = _save_instance(runtime, instance_id="wf_needs_connector", tools=())
        manager = _workflow_manager(runtime)
        row = manager.create_schedule(_spec("wf_needs_connector", instance))

        record = await manager.fire_now(row.schedule_id)

        assert record is not None
        assert record.terminal_state == ConversationStatus.PAUSED.value
        assert record.verify_verdict == "unverified"

        events = await runtime._store.get_events(record.run_cid)
        statuses = [event for event in events if isinstance(event, StatusEvent)]
        assert statuses[-1].status == ConversationStatus.PAUSED
        assert statuses[-1].detail == "workflow_needs_input"
        assert all(
            status.status != ConversationStatus.AWAITING_USER_QUESTION for status in statuses
        )
        agent_messages = [
            event.message.content
            for event in events
            if isinstance(event, MessageEvent) and event.source == EventSource.AGENT
        ]
        assert any("Approve the GitHub connector." in message for message in agent_messages)
    finally:
        await runtime.aclose()


def test_workflow_schedule_runs_route_lists_history() -> None:
    runtime, store = _runtime([])
    try:
        manager = _workflow_manager(runtime)
        instance = _save_instance(runtime, instance_id="wf_route", tools=())
        row = manager.create_schedule(_spec("wf_route", instance))
        root = runtime._projects.current_project_store().root
        assert root is not None
        runs_path = root / "workflow_schedules" / "runs.json"
        runs_path.parent.mkdir(parents=True, exist_ok=True)
        runs_path.write_text(
            json.dumps(
                [
                    {
                        "run_id": "wfsrun_route",
                        "schedule_id": row.schedule_id,
                        "run_cid": "conv_route",
                        "fired_at": "2026-07-04T12:00:00+00:00",
                        "terminal_state": "FINISHED",
                        "output_path": "outputs/result.md",
                        "verify_verdict": "pass",
                        "coalesced": False,
                        "error": None,
                    }
                ]
            ),
            encoding="utf-8",
        )

        app = FastAPI()
        app.include_router(make_schedules_router(store, runtime))
        runs = _RouteTestClient(app).get("/api/workflows/schedules/runs").json()["runs"]

        assert runs[0]["schedule_id"] == row.schedule_id
        assert runs[0]["run_cid"] == "conv_route"
        assert runs[0]["verify_verdict"] == "pass"
    finally:
        asyncio.run(runtime.aclose())
