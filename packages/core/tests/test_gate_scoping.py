"""DC-03 — blast-radius gate scoping: BlastRadiusConfirm + engine integration.

Tests follow test_security_policy.py's stub patterns. The acceptance ladder
items are tested directly against the policy and via the engine (meta stamping).
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ConversationStatus,
    SecurityRisk,
)
from disco.core.loop import (
    AlwaysConfirm,
    BlastRadiusConfirm,
    ConfirmRisky,
    NeverConfirm,
)
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

U, L, M, H = (
    SecurityRisk.UNKNOWN,
    SecurityRisk.LOW,
    SecurityRisk.MEDIUM,
    SecurityRisk.HIGH,
)
ALL = [U, L, M, H]


# ---- 1. BlastRadiusConfirm policy unit tests ---------------------------------


def test_sandboxed_high_does_not_gate():
    """Sandboxed HIGH-risk → auto-approve (confinement is the guarantee)."""
    p = BlastRadiusConfirm()
    assert p.should_confirm_action(H, scope="sandbox", tool_name="shell") is False


def test_sandboxed_unknown_does_not_gate():
    """Sandboxed UNKNOWN → auto-approve (confinement, not scoring)."""
    p = BlastRadiusConfirm()
    assert p.should_confirm_action(U, scope="sandbox", tool_name="file_write") is False


def test_sandboxed_low_does_not_gate():
    p = BlastRadiusConfirm()
    assert p.should_confirm_action(L, scope="sandbox", tool_name="shell") is False


def test_in_process_high_gates():
    """in_process HIGH → ConfirmRisky semantics → gates."""
    p = BlastRadiusConfirm()
    assert p.should_confirm_action(H, scope="in_process", tool_name="some_tool") is True


def test_in_process_unknown_gates():
    """in_process UNKNOWN → ConfirmRisky semantics → gates (confirm-on-UNKNOWN)."""
    p = BlastRadiusConfirm()
    assert p.should_confirm_action(U, scope="in_process", tool_name="some_tool") is True


def test_in_process_low_does_not_gate():
    """in_process LOW → below ConfirmRisky threshold → no gate."""
    p = BlastRadiusConfirm()
    assert p.should_confirm_action(L, scope="in_process", tool_name="some_tool") is False


def test_unknown_scope_gates_like_confirm_risky():
    """scope == 'unknown' (executor without tool_scope) → conservative fallback."""
    p = BlastRadiusConfirm()
    assert p.should_confirm_action(H, scope="unknown", tool_name="shell") is True
    assert p.should_confirm_action(U, scope="unknown", tool_name="shell") is True
    assert p.should_confirm_action(L, scope="unknown", tool_name="shell") is False


def test_publish_guard_gates_regardless_of_scope():
    """deploy/publish/release in tool_name → always gate, even in sandbox."""
    p = BlastRadiusConfirm()
    for kw in ("deploy_site", "publish_artifact", "release_package"):
        assert p.should_confirm_action(L, scope="sandbox", tool_name=kw) is True
        assert p.should_confirm_action(H, scope="sandbox", tool_name=kw) is True


# ---- 2. Default should_confirm_action delegation (existing policies unchanged) ---


def test_never_confirm_action_delegates_correctly():
    """NeverConfirm.should_confirm_action → always False for all risks/scopes."""
    p = NeverConfirm()
    for risk in ALL:
        for scope in ("sandbox", "in_process", "unknown"):
            assert p.should_confirm_action(risk, scope=scope, tool_name="x") is False


def test_always_confirm_action_delegates_correctly():
    """AlwaysConfirm.should_confirm_action → always True for all risks/scopes."""
    p = AlwaysConfirm()
    for risk in ALL:
        for scope in ("sandbox", "in_process", "unknown"):
            assert p.should_confirm_action(risk, scope=scope, tool_name="x") is True


def test_confirm_risky_action_delegates_correctly():
    """ConfirmRisky.should_confirm_action mirrors should_confirm — scope ignored."""
    p = ConfirmRisky(H, confirm_unknown=True)
    for scope in ("sandbox", "in_process", "unknown"):
        assert p.should_confirm_action(H, scope=scope, tool_name="x") is True
        assert p.should_confirm_action(U, scope=scope, tool_name="x") is True
        assert p.should_confirm_action(M, scope=scope, tool_name="x") is False
        assert p.should_confirm_action(L, scope=scope, tool_name="x") is False


# ---- 3. Hard-deny still fires before any policy (pre-gate) -------------------


async def test_hard_deny_refuses_sandboxed_rm_rf_before_policy():
    """rm -rf / is hard-denied before BlastRadiusConfirm can auto-approve it.
    Even sandboxed, catastrophic commands are refused at the pre-gate layer."""
    agent = ScriptedAgent([action_step(tool="shell", args={"command": "rm -rf /"}), finish_step()])
    loop, store = build_loop(
        agent,
        analyzer=FakeAnalyzer(H),
        policy=BlastRadiusConfirm(),
    )
    await loop.send_message("destroy everything")
    await loop.run()
    # The loop continues (hard-deny produces an AgentError, loop recovers to FINISHED
    # since the scripted agent emits a finish_step next). Critically: no WAITING_FOR_CONFIRMATION.
    events = await store.get_events("conv")
    statuses = [e.status for e in events if hasattr(e, "status")]
    assert ConversationStatus.WAITING_FOR_CONFIRMATION not in statuses
    from disco.core import AgentErrorEvent
    errors = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert errors, "hard-deny must emit an AgentErrorEvent"
    assert any("hard-denied" in e.error or "REFUSED" in e.error for e in errors)


# ---- 4. Engine: meta["auto_approved"] stamped on auto-approved sandboxed ops ---


class _SandboxScopedExecutor(FakeExecutor):
    """A FakeExecutor that reports every tool as sandboxed via tool_scope()."""

    def tool_scope(self, tool_name: str) -> str:
        return "sandbox"


async def test_sandboxed_high_meta_stamped_auto_approved():
    """Engine stamps auto_approved='sandboxed' when BlastRadiusConfirm skips a gate
    that ConfirmRisky would have fired (sandboxed HIGH-risk action)."""
    agent = ScriptedAgent(
        [action_step(tool="shell", args={"command": "rm -rf /workspace"}), finish_step()]
    )
    loop, store = build_loop(
        agent,
        executor=_SandboxScopedExecutor(),
        analyzer=FakeAnalyzer(H),
        policy=BlastRadiusConfirm(),
    )
    await loop.send_message("clean workspace")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert action_events, "action must be emitted"
    executed_action = action_events[0]
    assert executed_action.meta.get("auto_approved") == "sandboxed"


async def test_sandboxed_no_gate_no_waiting_for_confirmation():
    """Sandboxed HIGH under BlastRadiusConfirm → no WAITING_FOR_CONFIRMATION gate."""
    agent = ScriptedAgent(
        [action_step(tool="shell", args={"command": "npm install"}), finish_step()]
    )
    loop, store = build_loop(
        agent,
        executor=_SandboxScopedExecutor(),
        analyzer=FakeAnalyzer(H),
        policy=BlastRadiusConfirm(),
    )
    await loop.send_message("install deps")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    statuses = [e.status for e in events if hasattr(e, "status")]
    assert ConversationStatus.WAITING_FOR_CONFIRMATION not in statuses


class _InProcessScopedExecutor(FakeExecutor):
    """A FakeExecutor that reports every tool as in_process."""

    def tool_scope(self, tool_name: str) -> str:
        return "in_process"


async def test_in_process_high_still_gates_under_blast_radius():
    """in_process HIGH under BlastRadiusConfirm → gate fires (host-scope op)."""
    agent = ScriptedAgent([action_step(tool="shell", args={"command": "curl host"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=_InProcessScopedExecutor(),
        analyzer=FakeAnalyzer(H),
        policy=BlastRadiusConfirm(),
    )
    await loop.send_message("do host op")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION


async def test_auto_approved_not_stamped_when_base_would_not_gate():
    """auto_approved is NOT stamped for LOW-risk sandboxed ops — ConfirmRisky
    would not have gated these anyway, so the stamp would be misleading."""
    agent = ScriptedAgent(
        [action_step(tool="file_read", args={"path": "README.md"}), finish_step()]
    )
    # rp-12 registry-aware requery: a tool absent from available_tools() is
    # treated as unknown and requeried — the test executor must register what
    # the script calls, like a real registry would.
    from disco.core.llm.types import ToolSpec
    loop, store = build_loop(
        agent,
        executor=_SandboxScopedExecutor(
            tools=[
                ToolSpec(name="shell", description="run a shell command", parameters_schema={}),
                ToolSpec(name="file_read", description="read a file", parameters_schema={}),
            ]
        ),
        analyzer=FakeAnalyzer(L),
        policy=BlastRadiusConfirm(),
    )
    await loop.send_message("read a file")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events("conv")
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    assert action_events
    assert action_events[0].meta.get("auto_approved") is None


async def test_executor_without_tool_scope_falls_back_gracefully():
    """When executor has no tool_scope() (old-style FakeExecutor), the gate falls
    back to scope='unknown' → BlastRadiusConfirm uses ConfirmRisky semantics."""
    agent = ScriptedAgent([action_step(tool="shell", args={"command": "echo hi"}), finish_step()])
    loop, store = build_loop(
        agent,
        executor=FakeExecutor(),  # no tool_scope method
        analyzer=FakeAnalyzer(H),
        policy=BlastRadiusConfirm(),
    )
    await loop.send_message("go")
    state = await loop.run()
    # scope=unknown + HIGH risk → ConfirmRisky semantics → gate fires
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION
