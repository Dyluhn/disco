"""Notify completion preserves host failures independently of shell diagnostics."""

from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    StatusEvent,
    VerifierVerdictEvent,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop.turn_control import Valve
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop
from test_check_ledger import CID, _edit, _pure


@pytest.mark.parametrize("shell_stale", [False, True])
async def test_notify_completion_keeps_actual_browser_unverified_release(shell_stale):
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("file_write", {"path": "index.html", "content": "<html></html>"}),
            action_step("plan_step", {"index": 1, "state": "done"}),
            *[
                action_step("notify_user", {"message": "ALL TESTS PASSED. The site is complete."})
                for _ in range(3)
            ],
        ]
    )
    executor = FakeExecutor(
        tools=[
            ToolSpec(name=name, description=name, parameters_schema={})
            for name in ("file_write", "notify_user", "file_read")
        ]
    )
    loop, store = build_loop(
        executor=executor, agent=agent, planning_tools=frozenset({"file_read"})
    )
    loop.mode = OperatingMode.PLANNING
    await loop.send_message("Build a page")
    await loop.run()
    await loop.approve_plan()
    if shell_stale:
        for event in _pure("check", "bash smoke.sh") + _edit("edit", "src/a.js"):
            await loop._emit(event)
    state = await loop.run()
    assert state.execution_status is ConversationStatus.FINISHED
    events = await store.get_events(CID)
    releases = [
        e for e in events if isinstance(e, StatusEvent) and e.detail == "unverified_release"
    ]
    assert len(releases) == 1
    assert events[-1].detail == "completed_via_notify"
    final = [e for e in events if isinstance(e, MessageEvent)][-1]
    assert final.source is EventSource.AGENT
    assert final.message.role == "assistant"
    assert (
        final
        == [e for e in events if isinstance(e, MessageEvent) and e.source is EventSource.AGENT][-1]
    )
    assert final.meta["host_owned_terminal_warning"] is True
    assert final.meta["unverified_release"] is True
    assert "UNVERIFIED FINAL RESULT" in final.message.content
    assert "gates passed" not in final.message.content
    assert "ALL TESTS PASSED" not in final.message.content
    assert ("Shell diagnostics remain unresolved" in final.message.content) is shell_stale
    assert final.meta.get("shell_diagnostics_advisory", False) is shell_stale


@pytest.mark.parametrize("shell_stale", [False, True])
async def test_notify_later_host_pass_clears_only_host_failure(shell_stale):
    loop, store = build_loop(ScriptedAgent([]))
    events = _pure("check", "bash smoke.sh")
    if shell_stale:
        events += _edit("edit", "src/a.js")
    events += [
        VerifierVerdictEvent(verified=False, verdict="fail"),
        StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_release"),
        VerifierVerdictEvent(verified=True, verdict="pass"),
    ]
    for event in events:
        await loop._emit(event)
    assert await Valve(loop)._completed_via_notify_finish(await loop._events())
    events = await store.get_events(CID)
    final = [e for e in events if isinstance(e, MessageEvent)][-1]
    assert "UNVERIFIED FINAL RESULT" not in final.message.content
    assert not final.meta.get("unverified_release")
    assert ("Shell diagnostics remain unresolved" in final.message.content) is shell_stale
    assert final.meta.get("shell_diagnostics_advisory", False) is shell_stale
    if shell_stale:
        assert final.source is EventSource.AGENT
        assert final.message.role == "assistant"
    if not shell_stale:
        assert "gates passed" in final.message.content
        assert not final.meta.get("host_owned_terminal_warning")
