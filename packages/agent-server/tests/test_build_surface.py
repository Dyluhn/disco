"""The Build (Agent) surface composed in the runtime — hermetic, scripted model.

Proves the gate policy on a state-changing surface (the headline, DC-03 contract):
a risky action that runs INSIDE the sandbox auto-approves (confinement is the blast
radius) and is stamped `auto_approved: sandboxed`; a publish-class action (deploy/
publish/release) ALWAYS pauses at WAITING_FOR_CONFIRMATION regardless of scope —
confirm runs exactly it, reject denies without executing. Plus: the kill switch
revokes caps + tears down the sandbox, and the Research surface stays ungated.
Real composition (RouterAgent + DefaultToolExecutor + agent tools + ProcessSandbox
+ BlastRadiusConfirm + RuleBasedAnalyzer), fake MODEL only.
"""

from __future__ import annotations

import asyncio
import contextlib

from disco.agent_server import ConversationRuntime
from disco.agent_server.run_supervisor import _CONCLUDED_STATUSES, _RUN_PARKED_STATUSES
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SecurityRisk,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    ModelRole,
    OperatingMode,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from disco.tools import ProcessSandboxService, ToolDef
from disco.tools.behavior import OPAQUE_MCP_BEHAVIOR
from pydantic import BaseModel

CID = "c1"
_PUBLISH_TOOL = "mcp__publish_srv__deploy_site"


class _NoArgs(BaseModel):
    pass


class _PublishMcpClient:
    def __init__(self) -> None:
        self.calls = 0
        self._url = "https://mcp.example.test"
        self.allowed_hosts: tuple[str, ...] = ()

    async def call_tool(self, tool: str, arguments: dict) -> dict:
        self.calls += 1
        return {
            "content": [{"type": "text", "text": f"published via {tool}"}],
            "isError": False,
        }


class _ScriptedProvider:
    """A model double whose response varies per call. `steps`: list of
    (text, [ProposedToolCall]); an empty tool-call list means 'finish'.

    Role-aware: a SUMMARIZER-role request answers with a canned title and does
    NOT advance the positional step counter. The runtime's `kick` fires the
    async auto-titler (`title_service.schedule` → `router.complete` with the
    SUMMARIZER role) through this same shared provider; if it consumed a step it
    would silently eat the scripted build steps (notably step 0 = submit_plan),
    desyncing the build driver. A real stateless LLM wouldn't desync either, so
    making the double role-aware keeps it faithful and immunizes EVERY positional
    test in this file at once."""

    name = "fake"

    def __init__(self, steps) -> None:
        self._steps = list(steps)
        self.calls = 0

    async def complete(self, req, *, model):
        if req.profile.role == ModelRole.SUMMARIZER:
            # The auto-titler — answer out-of-band; don't touch the step counter.
            return CompletionResponse(
                text="Scripted Build Task",
                tool_calls=[],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="stop",
                model_used=model,
                request_id=req.request_id,
                routing=None,
            )
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


def _finish(summary: str = "done") -> ProposedToolCall:
    """The affirmative terminal move (GAP B). In execution mode a tool-less prose
    turn no longer ends the run; only `finish` does."""
    return ProposedToolCall(tool_name="finish", arguments={"summary": summary})


def _plan_step_done(idx: int) -> ProposedToolCall:
    """Mark a plan step done via the current progress tool.

    Without this, the loop refuses to land in FINISHED (plan has incomplete steps)
    and falls back to STUCK. The scripted scenarios that should end FINISHED need
    to walk the plan to completion before declaring done."""
    return ProposedToolCall(
        tool_name="update_plan_progress",
        arguments={"steps": [{"index": idx, "state": "done"}]},
    )


def _plan(steps: list[str]) -> ProposedToolCall:
    """A scripted `submit_plan` call — the entry to plan-first Build."""
    return ProposedToolCall(
        tool_name="submit_plan",
        arguments={"summary": "scripted", "steps": [{"title": s} for s in steps]},
    )


def _runtime(store: SqliteEventStore, steps) -> ConversationRuntime:
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _ScriptedProvider(steps)})
    runtime = ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())
    # Re-aim the publish-gate integration at a REGISTERED host tool. Unknown or
    # out-of-scope names intentionally bypass confirmation since 09a1f127 and
    # go straight to the canonical refusal; using one here would test obsolete
    # gate order instead of confirmation semantics.
    if any(tc.tool_name == _PUBLISH_TOOL for _, tool_calls in steps for tc in tool_calls):
        runtime.mcp._http_tools[_PUBLISH_TOOL] = ToolDef(
            name=_PUBLISH_TOOL,
            description="Publish a test site through an approved MCP server",
            args_model=_NoArgs,
            base_risk=SecurityRisk.HIGH,
            runs_in="in_process",
            read_only=False,
            behavior=OPAQUE_MCP_BEHAVIOR,
        )
        runtime.mcp._http_clients["publish_srv"] = _PublishMcpClient()
    return runtime


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


async def _run_to_rest(runtime: ConversationRuntime) -> None:
    """Kick the loop and await it to a terminal-for-now status (WAITING/FINISHED)."""
    runtime.run_controller.kick(CID)
    task = runtime._run_registry.task(CID)
    if task is not None:
        await task


async def _build_convo(store, steps) -> ConversationRuntime:
    store.create_conversation(CID, owner_id="local")
    runtime = _runtime(store, steps)
    runtime.set_surface(CID, "build")
    await store.append(CID, _user("delete the temp directory"))
    return runtime


# Plan-first lifecycle: PLANNING propose → APPROVE flips to execution → risky shell
# (sandboxed → auto-approved under BlastRadiusConfirm) → finish. Build now starts in
# PLANNING mode, so the first scripted step must be a `submit_plan` call.
_RISKY = [
    ("here's the plan", [_plan(["remove the dir"])]),
    ("removing the dir", [_shell("rm -rf doomed")]),
    ("step 1 complete", [_plan_step_done(1)]),
    ("done", [_finish()]),
]

# Publish-class lifecycle: a registered MCP host tool trips both the configured
# HIGH risk gate and BlastRadiusConfirm's publish-name guard.
_PUBLISH = [
    ("here's the plan", [_plan(["deploy the site"])]),
    ("deploying", [ProposedToolCall(tool_name=_PUBLISH_TOOL, arguments={})]),
    # [REL-RC-J] a genuinely SUCCESSFUL productive action: the finish gate now counts
    # outcomes (successful observations), not attempts — without this, a script whose
    # deploy is rejected/failed has done no real work and FINISHED is correctly refused.
    (
        "noting outcome",
        [
            ProposedToolCall(
                tool_name="file_write",
                arguments={"path": "publish-note.txt", "content": "deploy attempted"},
            )
        ],
    ),
    ("step 1 complete", [_plan_step_done(1)]),
    ("done", [_finish()]),
]


async def _await_task(runtime: ConversationRuntime) -> None:
    """Drain the runtime loop for CID to a stable resting state.

    The supervisor's silent-stall recovery (`_on_run_task_done` → schedules
    `_finalize_clean_return` → which may `kick` again) can REPLACE `_tasks[CID]`
    with a fresh task AFTER the original resolves — and it does so via chained
    `asyncio.create_task` callbacks, so right after `await task` returns there may
    briefly be NO live task while a successor is still being scheduled. Awaiting a
    single snapshot handle therefore returns mid-flight, letting assertions run
    while a re-kicked task is still terminalizing (the STUCK flake). Instead: await
    whatever handle is current, yield so pending done-callbacks/re-kicks can install
    a successor, and stop only once the conversation has settled at a concluded or
    parked status with no live task left."""
    settled = _CONCLUDED_STATUSES | _RUN_PARKED_STATUSES
    for _ in range(500):  # generous bound; each pass awaits a task or yields one tick
        task = runtime._run_registry.task(CID)
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await task
            continue
        # No live task right now — let any scheduled done-callback / re-kick run,
        # then re-check (a successor task may appear, or the status may settle).
        await asyncio.sleep(0)
        if runtime._run_registry.task(CID) is not None:
            continue  # a re-kick landed; loop back to await it
        try:
            status = (await runtime._store.get_state(CID)).execution_status
        except Exception:  # noqa: BLE001 — a test-helper drain, never surface
            return
        if status in settled:
            return


async def _approve_plan_and_run(runtime: ConversationRuntime) -> None:
    """Advance past the plan-approval gate into the per-action build phase. Used by the
    tests that target the per-action gate (the plan gate isn't their subject)."""
    await runtime.approve_plan(CID)
    await _await_task(runtime)


# ---- plan-first lifecycle: the plan gate, then the per-action gate ----------


async def test_build_starts_in_planning_and_pauses_for_plan_approval():
    """Submission proposes a plan and HALTS for approval — no work yet."""
    from disco.core.events import PlanEvent

    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _RISKY)
    await _run_to_rest(runtime)

    state = await store.get_state(CID)
    events = await store.get_events(CID)
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert state.pending_plan_id is not None
    plan = next(e for e in events if isinstance(e, PlanEvent))
    assert plan.id == state.pending_plan_id
    assert plan.steps and plan.steps[0].title == "remove the dir"
    # nothing else executed (no actions, no observations).
    assert not any(isinstance(e, ActionEvent) for e in events)
    assert not any(isinstance(e, ObservationEvent) for e in events)


async def test_sandboxed_risky_action_auto_approves():
    """DC-03: a HIGH-risk action that runs inside the sandbox does NOT pause — the
    run goes straight to FINISHED, the action executes, and its meta carries both
    the risk assessment and the `auto_approved: sandboxed` stamp (so the UI can
    show the muted badge instead of an approval prompt)."""
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _RISKY)
    await _run_to_rest(runtime)  # → AWAITING_PLAN_APPROVAL
    await _approve_plan_and_run(runtime)  # approve → build runs to the end

    state = await store.get_state(CID)
    events = await store.get_events(CID)
    # never paused for per-action confirmation, anywhere in the run
    assert state.execution_status == ConversationStatus.FINISHED
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.WAITING_FOR_CONFIRMATION
        for e in events
    )
    # the risky shell action executed (observation exists)...
    shell_observations = [
        e for e in events if isinstance(e, ObservationEvent) and e.tool_result.tool_name == "shell"
    ]
    assert len(shell_observations) == 1
    # ...and was assessed + stamped, not silently waved through
    action = next(
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "shell"
    )
    assert "risk_assessment" in action.meta
    assert action.meta.get("auto_approved") == "sandboxed"


async def test_publish_action_pauses_and_confirm_executes_exactly_it():
    """The publish guard still BITES on the composed surface, and confirm executes
    exactly the pending registered host action once (reject leaves no call)."""
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _PUBLISH)
    await _run_to_rest(runtime)
    await _approve_plan_and_run(runtime)
    gated = await store.get_state(CID)
    assert gated.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION
    assert gated.pending_action_id is not None
    # gated, not auto-approved: the publish guard pre-empts the sandbox waiver
    pre_events = await store.get_events(CID)
    pending = next(
        e
        for e in pre_events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == _PUBLISH_TOOL
    )
    assert pending.meta.get("auto_approved") is None

    await runtime.confirm(CID)  # execute exactly it, then resume
    await _await_task(runtime)

    events = await store.get_events(CID)
    # The confirmed action was dispatched exactly once through the MCP client.
    executions = [
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.tool_name == _PUBLISH_TOOL
    ]
    assert len(executions) == 1
    assert runtime.mcp._http_clients["publish_srv"].calls == 1
    assert (await store.get_state(CID)).execution_status == ConversationStatus.FINISHED


async def test_reject_denies_without_executing():
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, _PUBLISH)
    await _run_to_rest(runtime)
    await _approve_plan_and_run(runtime)

    # Grab the proposed action so we can verify the rejection is paired by call_id.
    pre_events = await store.get_events(CID)
    proposed = next(
        e
        for e in pre_events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == _PUBLISH_TOOL
    )

    await runtime.reject(CID)  # deny, resume without executing
    await _await_task(runtime)

    events = await store.get_events(CID)
    # The deploy action must NEVER have reached the registered MCP client.
    assert runtime.mcp._http_clients["publish_srv"].calls == 0
    rejection = next(e for e in events if isinstance(e, AgentErrorEvent))
    # Rejection is framed as an implicit system-reminder, not a user-tone error.
    assert "<system-reminder>" in rejection.error
    assert "did not approve" in rejection.error.lower()
    # And it's paired with the proposed action's call_id so the provider adapter
    # sees a properly-correlated tool result for the dangling assistant tool_call.
    assert rejection.tool_call_id == proposed.tool_call.call_id
    # The scripted scenario keeps running after rejection (next scripted step
    # marks plan step 1 done — an artifact of deterministic scripting, not a
    # claim about real-agent behavior post-reject). So the completeness gate is
    # satisfied and the loop lands in FINISHED.
    assert (await store.get_state(CID)).execution_status == ConversationStatus.FINISHED


async def test_sandbox_restart_emits_implicit_system_reminder():
    """If the sandbox's generation grows during a tool call (mid-session death,
    transparent recreate), the loop appends a system-reminder so the model knows
    files-on-disk remain but in-memory state was lost. Idempotent when no restart."""
    from disco.core import ToolCall, ToolResult
    from disco.core.loop.boundaries import ToolExecutor
    from disco.core.loop.engine import AgentLoop

    class _FakeSandbox:
        def __init__(self):
            self.generation = 1  # already alive
            self.id = "sbx-fake"

    class _FakeExecutor(ToolExecutor):
        def __init__(self, sandbox):
            self.sandbox = sandbox

        def available_tools(self):
            return []

        async def execute(self, tool_call):
            # simulate a mid-call sandbox restart
            self.sandbox.generation += 1
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                success=True,
                content="ok",
            )

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    sbx = _FakeSandbox()
    loop = AgentLoop(
        conversation_id=CID,
        store=store,
        agent=None,  # not exercised here
        executor=_FakeExecutor(sbx),
        router=None,
        analyzer=None,
        policy=None,
        condenser=None,
        summarizer=None,
        mode=OperatingMode.INTERACTIVE,
    )
    action = ActionEvent(
        thought="",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "true"}),
    )
    await loop._execute_and_observe(action)

    events = await store.get_events(CID)
    reminders = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and "<system-reminder>" in e.message.content
        and "sandbox container was restarted" in e.message.content
    ]
    assert len(reminders) == 1, (
        "expected exactly one sandbox-restart reminder after a generation bump"
    )
    # And the observation still landed — the reminder ACCOMPANIES the result, not replaces it
    assert any(isinstance(e, ObservationEvent) for e in events)


async def test_execution_gate_refuses_finish_without_productive_action():
    """The forcing function: in execution mode, FINISHED is refused while no
    productive ActionEvent has occurred since plan_approved — instead, an implicit
    system-reminder is appended and the loop re-enters. The reminder fires every
    time, with no cap (no ErrorEvent, no FINISHED-without-work)."""
    # [REL-RC-J] unique path: "f.txt" had accumulated in the DURABLE per-CID ProjectStore
    # (~/.local/share/disco/projects/c1/) across historical runs, so this write was silently
    # REFUSED (read_before_write) and only attempt-counting let the old test pass. The
    # conftest _isolated_projects_root fixture now isolates the store per test; the unique
    # name is belt-and-braces.
    safe = ProposedToolCall(
        tool_name="file_write", arguments={"path": "gate-productive-out.txt", "content": "ok"}
    )
    # Steps: plan #1 → approve → FIRST execution turn tries to finish immediately
    # → gate intercepts, appends system-reminder, retries → model now writes a file
    # and finishes for real.
    steps = [
        ("here's the plan", [_plan(["do the thing"])]),  # plan
        ("done!", [_finish()]),  # tries to finish immediately (no action) → gate fires
        ("ok ok writing", [safe]),  # complies on the next turn
        ("marking done", [_plan_step_done(1)]),  # plan-completeness gate
        ("done", [_finish()]),  # finishes after producing real work
    ]
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, steps)
    await _run_to_rest(runtime)  # → AWAITING_PLAN_APPROVAL
    await _approve_plan_and_run(runtime)

    state = await store.get_state(CID)
    events = await store.get_events(CID)

    # the loop reached FINISHED — but only after the model produced an action
    assert state.execution_status == ConversationStatus.FINISHED
    productive = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name not in ("submit_plan", "update_plan_progress")
    ]
    assert len(productive) >= 1  # the gate forced at least one real action

    # the implicit reminder was appended (system-reminder tag → ambient framing)
    reminders = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and "<system-reminder>" in e.message.content
        and "approved plan has not been executed" in e.message.content
    ]
    assert len(reminders) >= 1  # the gate fired at least once

    # NO ErrorEvent was emitted — "don't error out, send implicit reminders"
    assert not any(isinstance(e, ErrorEvent) for e in events), (
        "the gate must not error out; it nudges and re-enters"
    )


async def test_request_plan_after_finish_reopens_plan_mode_with_a_new_revision():
    """Re-entering plan mode after a build proposes a NEW plan (revision 2) and
    halts again for approval — the foundation of the diff-style change flow."""
    from disco.core.events import PlanEvent

    # A scripted lifecycle: plan #1 → approve → safe write → finish → request_plan(...) → plan #2.
    safe = ProposedToolCall(
        tool_name="file_write", arguments={"path": "reopen-plan-out.txt", "content": "ok"}
    )
    steps = [
        ("plan one", [_plan(["do the thing"])]),
        ("doing", [safe]),
        ("marking done", [_plan_step_done(1)]),  # plan-completeness gate
        ("done", [_finish()]),
        ("plan two", [_plan(["do another thing"])]),
    ]
    store = SqliteEventStore(":memory:")
    runtime = await _build_convo(store, steps)
    await _run_to_rest(runtime)  # → AWAITING_PLAN_APPROVAL (#1)
    await _approve_plan_and_run(runtime)  # build runs the safe write to FINISHED
    assert (await store.get_state(CID)).execution_status == ConversationStatus.FINISHED

    await runtime.request_plan(CID, "add a reset button")
    await _await_task(runtime)

    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    plans = [e for e in await store.get_events(CID) if isinstance(e, PlanEvent)]
    assert len(plans) == 2 and plans[-1].revision == 2  # the re-plan is revision 2


async def test_request_plan_on_a_fresh_conversation_composes_the_loop():
    """Regression: `request_plan` on a conversation with NO live loop (fresh, or any
    conversation after a server restart) must lazily compose one — the old
    `_loops.get()` guard silently dropped the frame, leaving the UI's optimistic
    echo at "sending…" forever while the server stayed IDLE at seq 0."""
    from disco.core.events import PlanEvent

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    runtime = _runtime(store, [("here's the plan", [_plan(["build it"])])])
    runtime.set_surface(CID, "build")
    assert runtime._loop_registry.loop(CID) is None  # never kicked — the no-op precondition

    await runtime.request_plan(CID, "build an express server")
    await _await_task(runtime)

    events = await store.get_events(CID)
    # the user's instruction was echoed to the log (what un-sticks "sending…")
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and e.message.content == "build an express server"
        for e in events
    )
    # and the loop actually ran planning: a revision-1 plan, halted for approval
    state = await store.get_state(CID)
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert len(plans) == 1 and plans[0].revision == 1


# ---- BlastRadiusConfirm vs NeverConfirm: the surfaces differ ----------------


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
    await _run_to_rest(runtime)  # composes the loop + executor (paused at the plan gate)

    executor = runtime._run_resources.executor(CID)
    assert executor is not None
    await runtime.kill(CID)

    assert executor._killed is True  # the executor's kill ran
    assert executor._broker._revoked is True  # capabilities revoked (§6.4)
    assert executor._sandbox._closed is True  # the sandbox session torn down
    # a terminal stop is recorded so subscribers (the UI) see it
    events = await store.get_events(CID)
    assert any(isinstance(e, StatusEvent) and e.detail == "killed" for e in events)
    # and the executor refuses further work after the kill
    from disco.core import ToolCall

    res = await executor.execute(ToolCall(tool_name="shell", arguments={"command": "echo hi"}))
    assert res.success is False and res.structured["kind"] == "sandbox_error"


# ---- W4 (§10.8): capability-gated exact_replace at the build-loop compose ----
#
# _compose_build_loop passes the BUILD LOOP's driver caps to agent_scope(model_policy),
# so exact_replace (the anchored-edit tool) is ADVERTISED only to a driver whose
# ModelEntry declares ANCHORED_EDIT; non-anchored drivers still don't see it (but it stays
# callable by qualified name). Caps are resolved from the persisted config store the
# same way production does — routing is pinned by the injected scripted router.

from disco.core.llm import ConfigStore, Requirement  # noqa: E402


def _caps_runtime(store, *, anchored: bool, tmp_path) -> ConversationRuntime:
    caps = {Requirement.TOOL_CALLING}
    if anchored:
        caps.add(Requirement.ANCHORED_EDIT)
    cfg = RouterConfig(
        models={
            "drv": ModelEntry(
                model_id="drv",
                provider="fake",
                base_url="http://driver.invalid/v1",
                context_window=8192,
                capabilities=frozenset(caps),
            )
        },
        default_model="drv",
    )
    config_store = ConfigStore(tmp_path / "cfg.json", base_factory=lambda: cfg)
    router = DefaultLLMRouter(cfg, {"fake": _ScriptedProvider([("plan", [_plan(["do it"])])])})
    return ConversationRuntime(
        store,
        router=router,
        config_store=config_store,
        sandbox_service=ProcessSandboxService(),
    )


async def _compose_and_get_executor(runtime: ConversationRuntime, store):
    """Kick a build conversation to the plan gate so _compose_build_loop runs and
    stores the executor; return it for advertised-set inspection."""
    store.create_conversation(CID, owner_id="local")
    runtime.set_surface(CID, "build")
    await store.append(CID, _user("build a thing"))
    await _run_to_rest(runtime)
    executor = runtime._run_resources.executor(CID)
    assert executor is not None
    return executor


async def test_build_loop_advertises_exact_replace_to_anchored_edit_driver(tmp_path):
    """A driver whose ModelEntry declares ANCHORED_EDIT gets exact_replace in the
    executor's ADVERTISED set (the capable tier)."""
    store = SqliteEventStore(":memory:")
    runtime = _caps_runtime(store, anchored=True, tmp_path=tmp_path)
    executor = await _compose_and_get_executor(runtime, store)

    advertised = {t.name for t in executor.available_tools()}
    assert "exact_replace" in advertised


async def test_build_loop_withholds_exact_replace_from_non_anchored_driver(tmp_path):
    """A driver WITHOUT ANCHORED_EDIT never sees exact_replace advertised (weak/
    default tier) — but it stays callable by qualified name (advertising is gated,
    allowed_tools is not)."""
    store = SqliteEventStore(":memory:")
    runtime = _caps_runtime(store, anchored=False, tmp_path=tmp_path)
    executor = await _compose_and_get_executor(runtime, store)

    advertised = {t.name for t in executor.available_tools()}
    assert "exact_replace" not in advertised
    assert "exact_replace" in executor._scope.allowed_tools
