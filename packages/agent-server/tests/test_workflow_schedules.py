"""WF-6 sealed scheduled workflow runs."""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlsplit

import httpx
import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.routes.schedules import make_schedules_router
from disco.agent_server.workflow_schedule import WorkflowScheduleManager
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
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
from disco.tools import ProcessSandboxService
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


def _runtime(steps: list[tuple[str, list[ProposedToolCall]]]) -> tuple[ConversationRuntime, SqliteEventStore]:
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
        sandbox_service=ProcessSandboxService(),
    )
    return runtime, store


def _workflow_definition(*, tools: tuple[str, ...]) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="scheduled_workflow",
        card="Produce the scheduled workflow output in the workspace.",
        params_model_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        tools=tools,
        policies=WorkflowPolicies(allows_writes=True),
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
) -> WorkflowInstance:
    defn = _workflow_definition(tools=tools)
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
    root = runtime.project_store().root
    assert root is not None
    workflows = root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / f"{instance_id}.json").write_text(
        json.dumps(instance.model_dump(mode="json")),
        encoding="utf-8",
    )
    return instance


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
        manager = WorkflowScheduleManager(runtime)
        row = manager.create_schedule(_spec("wf_ok", instance))

        record = await manager.fire_now(row.schedule_id)

        assert record is not None
        assert record.terminal_state == ConversationStatus.FINISHED.value
        assert record.output_path == "outputs/result.md"
        assert record.verify_verdict == "pass"
        assert manager.list_runs(schedule_id=row.schedule_id)[0].run_cid == record.run_cid
        assert runtime.project_store().get(record.run_cid) is not None

        executor = runtime._executors[record.run_cid]
        callable_names = executor.callable_tool_names()
        assert {"file_write", "skip", "needs_input"} <= callable_names
        assert callable_names.isdisjoint(
            {"list_workflows", "read_workflow_card", "enter_workflow", "draft_workflow"}
        )
        assert callable_names.isdisjoint({"ask_user", "clarify", "questions_v2"})
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_sealed_workflow_schedule_needs_input_lands_terminal_explanation() -> None:
    runtime, _store = _runtime([("blocked", [_needs_input()])])
    try:
        instance = _save_instance(runtime, instance_id="wf_needs_connector", tools=())
        manager = WorkflowScheduleManager(runtime)
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
            status.status != ConversationStatus.AWAITING_USER_QUESTION
            for status in statuses
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
        manager = WorkflowScheduleManager(runtime)
        instance = _save_instance(runtime, instance_id="wf_route", tools=())
        row = manager.create_schedule(_spec("wf_route", instance))
        root = runtime.project_store().root
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
