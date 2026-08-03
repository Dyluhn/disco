"""DC-05b — resume-path context reconstruction unit tests (DEFECT-4 root cause).

Covers:
  - dangling action → exactly one synthesized ObservationEvent, correctly paired
  - no dangling action → no synthetic observation
  - reality block: restore-files list, sessions line, sandbox reality sentence
  - plan restatement: 2 done of 4 → names step 3; no plan → line absent
  - DEFECT-4 replay against the archived real event log
  - integration: reconstruction events land in the store before the RUNNING flip
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from disco.agent_server import ConversationRuntime
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    event_from_json_dict,
)
from disco.core.llm import (
    CompletionResponse,
    ConfigStore,
    DefaultLLMRouter,
    ProjectStorageSettings,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from disco.core.migration import migrate_event
from disco.tools import ProcessSandboxService

CID = "test-rc-cid"

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "events-conv_c1b4675689484c63b1f45d92d45b95be-attempt3-loop-snapshot.json"
)

# action id of the in-flight shell_exec at seq=20 in the fixture
FIXTURE_DANGLING_ACTION_ID = "evt_3403bf01550d49fcae08af46bc548aed"
# title of step 2 ("Scaffold Vite + React frontend") — first undone step at PAUSED
FIXTURE_FIRST_UNDONE_STEP = "Scaffold Vite + React frontend"


# ---- shared helpers ----------------------------------------------------------


class _FakeProvider:
    name = "fake"

    async def complete(self, req, *, model):
        return CompletionResponse(
            text="done",
            tool_calls=[],
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


def _runtime(store: SqliteEventStore, *, projects_root: str | None = None) -> ConversationRuntime:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    router = DefaultLLMRouter(cfg, {"fake": _FakeProvider()})
    if projects_root is not None:
        cfg = cfg.model_copy(
            update={"projects": ProjectStorageSettings(projects_root=projects_root)}
        )
        cfg_store = ConfigStore(path=Path("/dev/null"))
        cfg_store.load = lambda: cfg  # type: ignore[method-assign]
        return ConversationRuntime(
            store,
            router=router,
            sandbox_service=ProcessSandboxService(),
            config_store=cfg_store,
        )
    return ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())


def _action(tool_name: str = "shell_exec", **kwargs) -> ActionEvent:
    return ActionEvent(
        source=EventSource.AGENT,
        thought="doing stuff",
        tool_call=ToolCall(tool_name=tool_name, arguments=kwargs),
    )


def _observation_for(action: ActionEvent) -> ObservationEvent:
    return ObservationEvent(
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=action.tool_call.tool_name,
            success=True,
            content="ok",
        ),
    )


def _agent_error_for(action: ActionEvent) -> AgentErrorEvent:
    return AgentErrorEvent(
        error="failed",
        action_id=action.id,
        tool_call_id=action.tool_call.call_id,
    )


def _plan(steps: list[str], revision: int = 1) -> PlanEvent:
    return PlanEvent(
        summary="test plan",
        steps=[PlanStep(title=t) for t in steps],
        revision=revision,
    )


def _plan_step(idx: int, state: str) -> ActionEvent:
    return ActionEvent(
        source=EventSource.AGENT,
        thought="updating step",
        tool_call=ToolCall(tool_name="plan_step", arguments={"index": idx, "state": state}),
    )


def _upp(steps: list[dict]) -> ActionEvent:
    """The declarative update_plan_progress full-state snapshot (capable models)."""
    return ActionEvent(
        source=EventSource.AGENT,
        thought="progress snapshot",
        tool_call=ToolCall(tool_name="update_plan_progress", arguments={"steps": steps}),
    )


async def _cancel_task(rt: ConversationRuntime) -> None:
    task = rt._run_registry.task(CID)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ---- dangling-action tests ---------------------------------------------------


async def test_dangling_action_gets_synthesized_observation():
    """An ActionEvent with no matching observation → one synthesized ObservationEvent."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    action = _action("npm_install", cmd="npm install")
    new_events = await rt._resume._reconstruct_resume_context(CID, [action])

    obs = [e for e in new_events if isinstance(e, ObservationEvent)]
    assert len(obs) == 1
    assert obs[0].action_id == action.id
    assert obs[0].tool_result.call_id == action.tool_call.call_id
    assert obs[0].tool_result.tool_name == action.tool_call.tool_name
    assert "interrupted by a server restart" in obs[0].tool_result.content

    # synthetic observation must precede the reality MessageEvent
    msg_idx = next(i for i, e in enumerate(new_events) if isinstance(e, MessageEvent))
    obs_idx = new_events.index(obs[0])
    assert obs_idx < msg_idx


async def test_no_dangling_action_means_no_synthetic_observation():
    """ActionEvent that already has an ObservationEvent → no synthetic event added."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    action = _action("shell_exec", command="echo hello")
    obs = _observation_for(action)
    new_events = await rt._resume._reconstruct_resume_context(CID, [action, obs])

    assert not any(isinstance(e, ObservationEvent) for e in new_events)


async def test_agent_error_resolves_dangling_action():
    """ActionEvent paired with an AgentErrorEvent is not considered dangling."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    action = _action("risky_op")
    err = _agent_error_for(action)
    new_events = await rt._resume._reconstruct_resume_context(CID, [action, err])

    assert not any(isinstance(e, ObservationEvent) for e in new_events)


# ---- reality-block tests -----------------------------------------------------


async def test_reality_block_contains_sandbox_sentence():
    """Reality block always includes the sandbox-was-reclaimed sentence."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    new_events = await rt._resume._reconstruct_resume_context(CID, [])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    assert "sandbox was reclaimed" in msg.message.content
    assert "Resumed by user." in msg.message.content


async def test_reality_block_contains_restore_files(tmp_path: Path):
    """When the project store has a snapshot, the file list appears in the block."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")

    # Seed a fake project workspace under tmp_path/<CID>/workspace/
    workspace = tmp_path / CID / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("print('hello')")
    subdir = workspace / "frontend" / "src"
    subdir.mkdir(parents=True)
    (subdir / "App.jsx").write_text("export default () => null")
    # Write a minimal manifest so ProjectStore.get() finds the project
    manifest = tmp_path / CID / "manifest.json"
    manifest.write_text(json.dumps({"file_count": 2, "total_bytes": 100}))

    rt = _runtime(store, projects_root=str(tmp_path))
    new_events = await rt._resume._reconstruct_resume_context(CID, [])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    content = msg.message.content
    assert "app.py" in content
    assert "App.jsx" in content
    assert "No saved files" not in content


async def test_reality_block_no_snapshot_shows_empty_message():
    """When there is no project-store snapshot, the 'no saved files' line appears."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)  # no projects_root → no snapshot

    new_events = await rt._resume._reconstruct_resume_context(CID, [])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    assert "No saved files" in msg.message.content


async def test_reality_block_live_sandbox_not_described_as_reclaimed():
    """A valve-PAUSED resume (executor still alive, dc-05a breakers) must NOT claim
    the sandbox was reclaimed — the reality block has to match actual liveness."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt._run_resources.set_executor(
        CID,
        cast(Any, SimpleNamespace(sandbox=None)),
    )  # liveness probe is membership, same as suspend's

    new_events = await rt._resume._reconstruct_resume_context(CID, [])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    assert "sandbox was reclaimed" not in msg.message.content
    assert "sandbox is still running" in msg.message.content


async def test_reality_block_sessions_line_no_sandbox():
    """No live sandbox → 'No shell sessions are running.' line appears."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    new_events = await rt._resume._reconstruct_resume_context(CID, [])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    assert "No shell sessions are running." in msg.message.content


# ---- plan-restatement tests --------------------------------------------------


async def test_plan_restatement_names_first_undone_step():
    """Steps 1-2 done of 4 → reality block names step 3 verbatim."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    plan = _plan(["Step A", "Step B", "Step C", "Step D"])
    done1 = _plan_step(1, "done")
    done2 = _plan_step(2, "done")

    new_events = await rt._resume._reconstruct_resume_context(CID, [plan, done1, done2])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    content = msg.message.content
    assert "Step C" in content
    assert "(3)" in content
    assert "Do not re-plan" in content


async def test_plan_restatement_reads_update_plan_progress():
    """Steps 1-2 marked done via the DECLARATIVE update_plan_progress snapshot (capable
    models, not plan_step) → the resume reality block must still name step 3 as next. Before
    the unify fix, resume read only plan_step and would tell the agent to redo step 1."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    plan = _plan(["Step A", "Step B", "Step C", "Step D"])
    snapshot = _upp([{"index": 1, "state": "done"}, {"index": 2, "state": "done"}])

    new_events = await rt._resume._reconstruct_resume_context(CID, [plan, snapshot])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    content = msg.message.content
    assert "Step C" in content
    assert "(3)" in content


async def test_plan_restatement_absent_when_no_plan():
    """No plan in the event log → 'Next actionable step' line is omitted."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    new_events = await rt._resume._reconstruct_resume_context(CID, [])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    assert "Next actionable step" not in msg.message.content


async def test_plan_restatement_absent_when_all_steps_done():
    """All plan steps marked done → restatement line is omitted."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    plan = _plan(["Step A", "Step B"])
    done1 = _plan_step(1, "done")
    done2 = _plan_step(2, "done")

    new_events = await rt._resume._reconstruct_resume_context(CID, [plan, done1, done2])

    msg = next(e for e in new_events if isinstance(e, MessageEvent))
    assert "Next actionable step" not in msg.message.content


# ---- DEFECT-4 replay ---------------------------------------------------------


async def test_defect4_replay():
    """Regression: archived real event log (attempt3) yields correct reconstruction.

    Verifies:
    - The seq-20 shell_exec (npm create vite) gets a synthesized ObservationEvent
    - The reality block is present with the sandbox sentence
    - The plan restatement points at step 2 "Scaffold Vite + React frontend"
    """
    raw_events = json.loads(FIXTURE_PATH.read_text())

    # Slice up to and including the first PAUSED StatusEvent
    first_paused_idx = next(
        i
        for i, e in enumerate(raw_events)
        if e.get("kind") == "status" and e.get("status") == "PAUSED"
    )
    events = [event_from_json_dict(migrate_event(e)) for e in raw_events[: first_paused_idx + 1]]

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)

    new_events = await rt._resume._reconstruct_resume_context(CID, events)

    # seq-20 action must get a synthetic observation
    obs = [e for e in new_events if isinstance(e, ObservationEvent)]
    assert len(obs) == 1, f"expected 1 synthetic observation, got {len(obs)}"
    assert obs[0].action_id == FIXTURE_DANGLING_ACTION_ID
    assert "interrupted by a server restart" in obs[0].tool_result.content

    # Reality block is present
    msgs = [e for e in new_events if isinstance(e, MessageEvent)]
    assert len(msgs) == 1
    assert "sandbox was reclaimed" in msgs[0].message.content

    # Plan restatement names step 2 (first undone step after step 1 done)
    content = msgs[0].message.content
    assert FIXTURE_FIRST_UNDONE_STEP in content, (
        f"Expected '{FIXTURE_FIRST_UNDONE_STEP}' in content:\n{content}"
    )
    assert "(2)" in content


# ---- integration: resume_conversation wires it all together ------------------


async def test_reconstruction_events_land_before_running_flip():
    """Reconstruction events (obs + reality msg) appear in the store before RUNNING."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")

    # Seed a dangling action followed by a PAUSED status
    action = _action("shell_exec", command="npm install")
    await store.append(CID, action)
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    result = await rt.conversation_control.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True

    all_events = await store.get_events(CID)

    # Locate the RUNNING flip (the one appended by resume_conversation)
    running_idx = next(
        i
        for i, e in enumerate(all_events)
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.RUNNING
    )

    # Synthetic observation must exist and precede RUNNING
    obs_events = [e for e in all_events if isinstance(e, ObservationEvent)]
    assert len(obs_events) >= 1
    obs_idx = all_events.index(obs_events[0])
    assert obs_idx < running_idx, "synthetic observation must land before RUNNING flip"

    # Reality message must exist and precede RUNNING
    reality_msgs = [
        e
        for e in all_events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "Resumed by user." in e.message.content
    ]
    assert len(reality_msgs) == 1
    msg_idx = all_events.index(reality_msgs[0])
    assert msg_idx < running_idx, "reality message must land before RUNNING flip"
