"""Cross-contract acceptance gate — security-analyzer-contract.md §9.6.

The real loop (loop §5) + fakes elsewhere: this contract's decision (analyzer
score → policy gate) drives the loop's confirmation machinery, and the
RiskAssessment is reconstructable from the log (audit §7).
"""

from __future__ import annotations

from disco.core import ActionEvent, AgentErrorEvent, ConversationStatus
from disco.core.loop import ConfirmRisky, NeverConfirm
from disco.core.security import RuleBasedAnalyzer
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"


def _agent(cmd: str) -> ScriptedAgent:
    return ScriptedAgent([action_step(tool="shell", args={"command": cmd}), finish_step()])


async def test_high_risk_action_gates_for_confirmation_and_is_audited():
    executor = FakeExecutor()
    loop, store = build_loop(
        _agent("rm -rf /tmp/data"),
        executor=executor,
        analyzer=RuleBasedAnalyzer(),
        policy=ConfirmRisky(),  # the Agent-surface default
    )
    await loop.send_message("clean up the temp data")
    state = await loop.run()

    # Gated: WAITING_FOR_CONFIRMATION, nothing executed.
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION
    assert executor.calls == []

    # Audit (§7): the proposed ActionEvent carries the RiskAssessment, queryable
    # from the log — final risk HIGH + the producing analyzer.
    events = await store.get_events(CID)
    action = next(e for e in events if isinstance(e, ActionEvent))
    ra = action.meta["risk_assessment"]
    assert ra["risk"] == "HIGH"
    assert ra["analyzer"] == "rule_based"

    # confirm → executes EXACTLY that pending action.
    await loop.confirm()
    assert len(executor.calls) == 1
    assert executor.calls[0].arguments["command"] == "rm -rf /tmp/data"


async def test_reject_records_denial_and_does_not_execute():
    executor = FakeExecutor()
    loop, store = build_loop(
        _agent("rm -rf /tmp/data"),
        executor=executor,
        analyzer=RuleBasedAnalyzer(),
        policy=ConfirmRisky(),
    )
    await loop.send_message("clean up")
    await loop.run()
    state = await loop.reject("rejected by user")

    assert state.execution_status == ConversationStatus.RUNNING
    assert executor.calls == []  # never executed
    # the denial is in the permanent record (the agent sees it next View)
    events = await store.get_events(CID)
    assert any(isinstance(e, AgentErrorEvent) and "reject" in e.error.lower() for e in events)


async def test_low_risk_action_executes_without_gating():
    executor = FakeExecutor()
    loop, _store = build_loop(
        _agent("ls -la"),
        executor=executor,
        analyzer=RuleBasedAnalyzer(),
        policy=ConfirmRisky(),
    )
    await loop.send_message("list the files")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert len(executor.calls) == 1
    assert executor.calls[0].arguments["command"] == "ls -la"


async def test_never_confirm_surface_does_not_gate_even_a_high_action():
    # Same analyzer, different policy — the policy is the only security-axis
    # difference between surfaces (one core, many surfaces).
    executor = FakeExecutor()
    loop, _store = build_loop(
        _agent("rm -rf /tmp/data"),
        executor=executor,
        analyzer=RuleBasedAnalyzer(),
        policy=NeverConfirm(),  # the Research-surface policy
    )
    await loop.send_message("go")
    await loop.run()
    # HIGH-scored but NeverConfirm never gates → it executed.
    assert len(executor.calls) == 1
    assert executor.calls[0].arguments["command"] == "rm -rf /tmp/data"
