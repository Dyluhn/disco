"""PR S6 — fake-model / fake-tool simulator over the REAL loop, fed to the REAL
Build Soak classifier (guidelines §15.5, §27 S6).

These do NOT mock the loop: they drive the actual `AgentLoop` with a scripted model
+ an in-memory tool world (no live model, no sandbox), dump the REAL event log, and
run it through `harness.build_soak.classify`. The classifier must deterministically
code each simulated failure — wrong-tool-in-planning, no-replan-after-followup,
verifier(output)-failure — and accept a clean run. A malformed (stepless) plan must
classify without crashing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the in-repo `harness` package importable regardless of the runner PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from disco.core import ToolResult  # noqa: E402
from disco.core.events import event_to_json_dict  # noqa: E402
from disco.core.llm import OperatingMode, ToolSpec  # noqa: E402
from loop_fakes import (  # noqa: E402
    AgentStep,
    FakeExecutor,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

from harness.build_soak.classify import classify  # noqa: E402
from harness.build_soak.fixtures.fake_model import (  # noqa: E402
    malformed_plan,
    no_replan_followup,
    plan_then_build,
    wrong_tool_in_planning,
)
from harness.build_soak.fixtures.fake_tools import (  # noqa: E402
    FakeToolWorld,
    build_recording_executor,
    make_prose_step_builder,
)

_TOOLS = ["submit_plan", "file_read", "file_list", "search", "extract", "file_write", "shell"]
_PROD_PLANNING = frozenset({"submit_plan", "file_list", "file_read", "search", "extract"})

_PROSE = make_prose_step_builder(AgentStep)


def _executor(world: FakeToolWorld | None = None):
    return build_recording_executor(
        fake_executor_cls=FakeExecutor,
        tool_result_cls=ToolResult,
        tool_spec_cls=ToolSpec,
        tool_names=_TOOLS,
        world=world,
    )


def _steps(script):
    return script.agent_steps(action_step=action_step, finish_step=finish_step, prose_step=_PROSE)


async def _dump(store, cid):
    return [event_to_json_dict(e) for e in await store.get_events(cid)]


def _plan_scenario():
    return {"id": "sim", "assertions": {"event_chain": {"require_plan_before_execution": True}}}


async def test_wrong_tool_in_planning_is_gated():
    """REGRESSION (was ..._is_classified, which asserted the oracle CATCHES the bug): the
    engine's planning gate now PREVENTS a write attempted in PLANNING from executing, so the
    simulated product bug no longer occurs. Assert the FIX: the write does NOT execute and the
    oracle does NOT code WRITE_TOOL_ATTEMPTED_IN_PLANNING. (Oracle catch-capability for OTHER
    transcripts is covered by captured real-failure fixtures.)"""
    from disco.core import ObservationEvent

    cid = "sim-wrong-tool"
    agent = ScriptedAgent(_steps(wrong_tool_in_planning()))
    loop, store = build_loop(
        agent,
        conversation_id=cid,
        executor=_executor(),
        mode=OperatingMode.PLANNING,
        planning_tools=_PROD_PLANNING,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    await loop.send_message("create index.html")
    await loop.run()

    events = await store.get_events(cid)
    # The write in PLANNING was REJECTED by the gate — no SUCCESSFUL file_write observation.
    assert not [
        e
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "file_write"
        and e.tool_result.success
    ], "the planning gate must reject a write attempted in PLANNING (the fix)"
    c = classify(await _dump(store, cid), scenario=_plan_scenario())
    assert c["code"] != "WRITE_TOOL_ATTEMPTED_IN_PLANNING"


async def test_revision_followup_reenters_planning():
    """REGRESSION (was ..._is_classified): a revision follow-up via send_message now RE-ENTERS
    PLANNING (the ingest re-plan + marker fix 33a0cc43/77491570), so the loop no longer
    free-builds on the stale plan. Assert the FIX: a `planning` marker is emitted after the
    follow-up and no NO_REPLAN_AFTER_REVISION is produced."""
    from disco.core import EventSource, MessageEvent, StatusEvent

    cid = "sim-no-replan"
    agent = ScriptedAgent(_steps(plan_then_build()))
    loop, store = build_loop(
        agent,
        conversation_id=cid,
        executor=_executor(),
        mode=OperatingMode.PLANNING,
        planning_tools=_PROD_PLANNING,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    await loop.send_message("build a page")
    await loop.run()  # -> AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    await loop.run()  # execute + finish the first build

    def _seq_of_user(events, needle):
        return next(
            e.seq
            for e in events
            if isinstance(e, MessageEvent)
            and e.source == EventSource.USER
            and needle in (e.message.content or "")
        )

    loop.agent = ScriptedAgent(_steps(no_replan_followup()))
    await loop.send_message("also add a contact page")  # revision intent → ingest re-plan
    await loop.run()

    events = await store.get_events(cid)
    fseq = _seq_of_user(events, "contact page")
    # Re-entered PLANNING after the follow-up (the durable fix), so the stale free-build is gated.
    assert any(
        isinstance(e, StatusEvent) and e.detail == "planning" and (e.seq or 0) > fseq
        for e in events
    ), "a revision follow-up must re-enter PLANNING (the fix), not free-build"
    c = classify(await _dump(store, cid))
    assert c["code"] != "NO_REPLAN_AFTER_REVISION"


async def test_clean_run_passes_classifier():
    cid = "sim-clean"
    agent = ScriptedAgent(_steps(plan_then_build()))
    loop, store = build_loop(
        agent,
        conversation_id=cid,
        executor=_executor(),
        mode=OperatingMode.PLANNING,
        planning_tools=_PROD_PLANNING,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    await loop.send_message("build a page")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    c = classify(await _dump(store, cid), scenario=_plan_scenario())
    assert c["status"] == "PASS", c


async def test_verifier_output_failure_is_classified():
    """A finished build whose workspace lacks a required file -> FALSE_FINISH_NO_OUTPUT
    (the OutputTruthOracle over the captured fake tool world)."""
    cid = "sim-verifier"
    world = FakeToolWorld()
    executor = _executor(world)
    # the build writes other.html, but the scenario requires index.html
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "x"}]}),
            action_step("file_write", {"path": "other.html", "content": "hi"}),
            finish_step("done"),
        ]
    )
    loop, store = build_loop(
        agent,
        conversation_id=cid,
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=_PROD_PLANNING,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    await loop.send_message("build index.html")
    await loop.run()
    await loop.approve_plan()
    await loop.run()

    scenario = {
        "id": "sim-out",
        "assertions": {
            "event_chain": {"require_plan_before_execution": True},
            "workspace": {"files": [{"path": "index.html", "must_contain": ["hi"]}]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    c = classify(
        await _dump(store, cid),
        scenario=scenario,
        workspace_manifest=world.workspace_manifest(),
    )
    assert c["status"] == "FAIL"
    assert c["code"] == "FALSE_FINISH_NO_OUTPUT"


async def test_malformed_plan_classifies_without_crashing():
    """A stepless plan must not crash the classifier — it produces a deterministic
    outcome over a valid event log."""
    cid = "sim-malformed"
    agent = ScriptedAgent(_steps(malformed_plan()))
    loop, store = build_loop(
        agent,
        conversation_id=cid,
        executor=_executor(),
        mode=OperatingMode.PLANNING,
        planning_tools=_PROD_PLANNING,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    await loop.send_message("build")
    await loop.run()
    c = classify(await _dump(store, cid), scenario=_plan_scenario())
    assert c["status"] in {"PASS", "FAIL", "INVALID_RUN"}
    assert c["accepted_by"] == "oracle"
