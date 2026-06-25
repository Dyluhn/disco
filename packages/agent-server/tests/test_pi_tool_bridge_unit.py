"""EPIC D — the Pi tool bridge. Disco Pi Build Kernel Campaign.

Proves a Pi-ORIGINATED tool call is gated by EXACTLY the same safety wrapper the
Disco agent loop applies — NOT just ``DefaultToolExecutor.execute()``. Each test
drives one gate the codex EPIC-D inventory flagged as SAFETY-CRITICAL through
``PiToolBridge.route`` and asserts it fires identically to the Disco loop:

  * a write during PLANNING is REFUSED (no execution);
  * a hard-denied command is REFUSED outright;
  * a risky call HALTs at the confirmation gate (pending, not executed);
  * a sandbox-scoped risky call auto-approves;
  * a copied-back K1 elision marker is refused / recovered;
  * an unknown tool becomes a recoverable failure observation;
  * every executed action yields exactly one paired observation;
  * a raising tool still pairs (never dangling);
  * virtual/meta tools are NOT routed to the executor;
  * F9 read-dedup is assist-gated (OFF → executes; ON → pointer + grounds);
  * an interrupted (dangling) action is reconciled WITHOUT auto-replay.

Real ``signals.hard_deny_reason`` / ``RuleBasedAnalyzer`` / ``BlastRadiusConfirm``
are used for fidelity; the executor + host are in-memory fakes (no sandbox, no
container — capped-cgroup safe).
"""

from __future__ import annotations

import asyncio

import pytest
from disco.agent_server.build_kernel.pi_tool_bridge import (
    BridgeDecision,
    BridgeHost,
    PiToolBridge,
)
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationState,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm.types import OperatingMode
from disco.core.loop import signals
from disco.core.security.analyzers import RuleBasedAnalyzer

try:
    from disco.core.loop.policies import BlastRadiusConfirm
except ImportError:  # pragma: no cover - location guard
    from disco.core.loop.policies import ConfirmRisky as BlastRadiusConfirm  # type: ignore

ELISION_MARKER = "<4096 chars elided — re-issue the call or file_read the path for full content>"


# -- fakes --------------------------------------------------------------------
class FakeExecutor:
    """In-memory ``DefaultToolExecutor`` stand-in: records calls + grounding,
    returns scripted/structured ``ToolResult``s, models unknown-tool failure."""

    def __init__(
        self,
        *,
        scope: str = "host",
        results: dict[str, ToolResult] | None = None,
        known: tuple[str, ...] = ("file_write", "shell_exec", "file_read", "file_list"),
    ) -> None:
        self.scope = scope
        self.calls: list[ToolCall] = []
        self.grounded: list[str] = []
        self._results = results or {}
        self._known = set(known)
        self.sandbox = None
        self.raise_exc: Exception | None = None

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        if self.raise_exc is not None:
            raise self.raise_exc
        if call.tool_name not in self._known:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=False,
                error=f"unknown or out-of-scope tool {call.tool_name!r}",
            )
        scripted = self._results.get(call.tool_name)
        if scripted is not None:
            return scripted
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )

    def tool_scope(self, tool_name: str) -> str:
        return self.scope

    def note_grounding_read(self, path: str) -> None:
        self.grounded.append(path)

    def readonly_tool_names(self) -> frozenset[str]:
        return frozenset({"file_read", "file_list"})


class FakeHost:
    """Minimal :class:`BridgeHost` over an in-memory event log."""

    def __init__(
        self,
        executor: FakeExecutor,
        analyzer: object,
        policy: object,
        *,
        mode: OperatingMode = OperatingMode.LONG_HORIZON,
        plan_tool: str = "submit_plan",
        planning_allowed: frozenset[str] = frozenset({"file_read", "file_list"}),
        status: ConversationStatus = ConversationStatus.RUNNING,
        wire_replan: bool = False,
        state_raises: bool = False,
    ) -> None:
        self.executor = executor
        self.analyzer = analyzer
        self.policy = policy
        self.mode = mode
        self.plan_tool = plan_tool
        self._planning_allowed = planning_allowed
        self.log: list[Event] = []
        # Drivable by default (RUNNING); a test sets a halted status to exercise
        # the terminal-state gate. `state_raises` forces the fail-closed path.
        self.status = status
        self.pending_action_id: str | None = None
        self._state_raises = state_raises
        # `wire_replan` exposes the optional replan seam so the stale-plan test can
        # exercise P0/#3; it mirrors the engine gate using the REAL signals.
        self._wire_replan = wire_replan
        self.lock = asyncio.Lock()

    def planning_allowed_tool_names(self) -> frozenset[str]:
        return self._planning_allowed

    async def emit(self, event: Event) -> None:
        self.log.append(event)

    async def events(self) -> list[Event]:
        return list(self.log)

    async def state(self) -> ConversationState:
        if self._state_raises:
            raise RuntimeError("state indeterminate")
        return ConversationState(
            conversation_id="c",
            execution_status=self.status,
            pending_action_id=self.pending_action_id,
        )

    def __getattr__(self, name: str) -> object:
        # Expose `reenter_planning_for_followup` ONLY when wired — a faithful mirror
        # of the engine's `_maybe_reenter_planning_for_followup` (engine.py:1775)
        # built from the SAME signals, so the stale-plan test is meaningful. Absent
        # otherwise so the bridge's getattr-seam falls back to "no replan".
        if name == "reenter_planning_for_followup" and self.__dict__.get("_wire_replan"):
            return self._reenter_planning_for_followup
        raise AttributeError(name)

    async def _reenter_planning_for_followup(self, events: list[Event]) -> bool:
        if self.mode == OperatingMode.PLANNING:
            return False
        if not any(
            isinstance(e, StatusEvent) and e.detail == "plan_approved" for e in events
        ):
            return False
        text = signals.latest_unprocessed_user_text(events)
        if text is None or not signals.is_revision_intent(text):
            return False
        self.mode = OperatingMode.PLANNING
        return True


def _bridge(
    host: FakeHost, *, assist: bool = False, virtual: frozenset[str] = frozenset()
) -> PiToolBridge:
    return PiToolBridge(host, assist=assist, virtual_tool_names=virtual)


def _tc(tool_name: str, call_id: str = "c1", **args: object) -> ToolCall:
    return ToolCall(tool_name=tool_name, arguments=dict(args), call_id=call_id)


def _default_host(**kw: object) -> FakeHost:
    return FakeHost(FakeExecutor(), RuleBasedAnalyzer(), BlastRadiusConfirm(), **kw)  # type: ignore[arg-type]


# -- protocol -----------------------------------------------------------------
def test_fake_host_satisfies_protocol() -> None:
    assert isinstance(_default_host(), BridgeHost)


# -- G1: planning gate --------------------------------------------------------
@pytest.mark.asyncio
async def test_planning_gate_blocks_write() -> None:
    host = _default_host(mode=OperatingMode.PLANNING)
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content="x"))
    assert out.decision is BridgeDecision.REFUSED
    assert host.executor.calls == []  # NEVER executed
    assert out.error is not None and "PLANNING" in out.error.error
    # action + paired error in the log (KV-stable tool pairing)
    assert isinstance(host.log[0], ActionEvent)
    assert isinstance(host.log[1], AgentErrorEvent)
    assert host.log[1].action_id == host.log[0].id


@pytest.mark.asyncio
async def test_planning_gate_allows_read() -> None:
    host = _default_host(mode=OperatingMode.PLANNING)
    out = await _bridge(host).route(_tc("file_read", path="a.txt"))
    assert out.decision is BridgeDecision.EXECUTED
    assert [c.tool_name for c in host.executor.calls] == ["file_read"]


# -- G2: hard deny ------------------------------------------------------------
@pytest.mark.asyncio
async def test_hard_deny_refuses_rm_rf_root() -> None:
    host = _default_host()
    out = await _bridge(host).route(_tc("shell_exec", command="rm -rf /"))
    assert out.decision is BridgeDecision.REFUSED
    assert host.executor.calls == []
    assert out.error is not None and "hard-denied" in out.error.error
    # fidelity: same reason the real free function returns
    assert signals.hard_deny_reason(host.log[0]) is not None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_hard_deny_lets_safe_shell_through() -> None:
    host = _default_host(planning_allowed=frozenset())
    # auto-approve scope so the confirm gate doesn't intercept a benign command
    host.executor.scope = "sandbox"
    out = await _bridge(host).route(_tc("shell_exec", command="echo hi"))
    assert out.decision is BridgeDecision.EXECUTED
    assert [c.tool_name for c in host.executor.calls] == ["shell_exec"]


# -- G3: risk + confirmation --------------------------------------------------
@pytest.mark.asyncio
async def test_risky_host_call_hits_confirm_gate() -> None:
    host = _default_host()  # FakeExecutor scope defaults to "host"
    out = await _bridge(host).route(_tc("shell_exec", command="rm -rf /home/u/out"))
    assert out.decision is BridgeDecision.CONFIRM_REQUIRED
    assert host.executor.calls == []  # HALTED pending, not executed
    assert out.pending_action_id is not None
    # proposed action + WAITING_FOR_CONFIRMATION status in the log
    assert any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.WAITING_FOR_CONFIRMATION
        for e in host.log
    )
    # the risk audit was stamped into the action meta
    assert out.action is not None and "risk_assessment" in out.action.meta


@pytest.mark.asyncio
async def test_sandbox_scope_auto_approves_risky_call() -> None:
    host = _default_host()
    host.executor.scope = "sandbox"  # blast-radius exemption
    out = await _bridge(host).route(_tc("shell_exec", command="rm -rf /home/u/out"))
    assert out.decision is BridgeDecision.EXECUTED
    assert [c.tool_name for c in host.executor.calls] == ["shell_exec"]
    assert out.action is not None and out.action.meta.get("auto_approved") == "sandboxed"


# -- G4: K1 elision guard / recovery -----------------------------------------
@pytest.mark.asyncio
async def test_k1_marker_refused_when_unrecoverable() -> None:
    host = _default_host()
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content=ELISION_MARKER))
    assert out.decision is BridgeDecision.REFUSED
    assert host.executor.calls == []  # data-loss write blocked
    assert out.error is not None and "elision placeholder" in out.error.error


@pytest.mark.asyncio
async def test_k1_recovery_reexpands_from_log() -> None:
    host = _default_host()
    # prior confirmed write with the REAL content the marker stood in for
    prior_action = ActionEvent(
        thought="", tool_call=_tc("file_write", call_id="p1", path="a.txt", content="REAL CONTENT")
    )
    host.log.append(prior_action)
    host.log.append(
        ObservationEvent(
            tool_result=ToolResult(
                call_id="p1", tool_name="file_write", success=True, content="written"
            ),
            action_id=prior_action.id,
        )
    )
    out = await _bridge(host).route(
        _tc("file_write", call_id="c2", path="a.txt", content=ELISION_MARKER)
    )
    assert out.decision is BridgeDecision.EXECUTED
    # executed with the RE-EXPANDED content, not the marker
    assert host.executor.calls[-1].arguments["content"] == "REAL CONTENT"
    assert "a.txt" in host.executor.grounded  # grounded so it's not a blind rewrite


# -- executor validation / invalid-tool reroute ------------------------------
@pytest.mark.asyncio
async def test_unknown_tool_becomes_recoverable_failure() -> None:
    host = _default_host()
    out = await _bridge(host).route(_tc("totally_unknown_tool"))
    assert out.decision is BridgeDecision.FAILED
    assert out.error is not None and "unknown" in out.error.error
    assert out.error.action_id == host.log[0].id


# -- observation pairing ------------------------------------------------------
@pytest.mark.asyncio
async def test_success_pairs_observation() -> None:
    host = _default_host()
    out = await _bridge(host).route(_tc("file_write", call_id="cc", path="a.txt", content="hi"))
    assert out.decision is BridgeDecision.EXECUTED
    assert out.observation is not None
    assert out.observation.action_id == out.action.id  # type: ignore[union-attr]
    assert out.observation.tool_result.call_id == "cc"


@pytest.mark.asyncio
async def test_failure_pairs_error() -> None:
    fail = ToolResult(
        call_id="cc", tool_name="file_write", success=False, content="", error="disk full"
    )
    host = FakeHost(
        FakeExecutor(results={"file_write": fail}), RuleBasedAnalyzer(), BlastRadiusConfirm()
    )  # type: ignore[arg-type]
    host.executor.scope = "sandbox"
    out = await _bridge(host).route(_tc("file_write", call_id="cc", path="a.txt", content="hi"))
    assert out.decision is BridgeDecision.FAILED
    assert out.error is not None and out.error.error == "disk full"
    assert out.error.action_id == out.action.id  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_raising_tool_never_dangles() -> None:
    host = _default_host()
    host.executor.scope = "sandbox"
    host.executor.raise_exc = RuntimeError("boom")
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content="hi"))
    assert out.decision is BridgeDecision.FAILED
    assert out.error is not None and "boom" in out.error.error
    # the action is paired (no dangling) even though execute() raised
    assert PiToolBridge.reconcile_dangling_action(host.log) is None


# -- virtual / meta tools -----------------------------------------------------
@pytest.mark.asyncio
async def test_virtual_tool_short_circuits() -> None:
    host = _default_host()
    out = await _bridge(host, virtual=frozenset({"finish"})).route(_tc("finish"))
    assert out.decision is BridgeDecision.VIRTUAL
    assert host.executor.calls == []
    assert host.log == []  # not executed, nothing appended


# -- F9 read-dedup (assist-gated) ---------------------------------------------
def _prior_read_log() -> list[Event]:
    a = ActionEvent(thought="", tool_call=_tc("file_read", call_id="r1", path="a.txt"))
    o = ObservationEvent(
        tool_result=ToolResult(
            call_id="r1", tool_name="file_read", success=True, content="FILE BODY"
        ),
        action_id=a.id,
    )
    return [a, o]


@pytest.mark.asyncio
async def test_f9_assist_off_re_executes() -> None:
    host = _default_host()
    host.log = _prior_read_log()
    out = await _bridge(host, assist=False).route(_tc("file_read", call_id="r2", path="a.txt"))
    assert out.decision is BridgeDecision.EXECUTED
    assert [c.call_id for c in host.executor.calls] == ["r2"]  # actually executed


@pytest.mark.asyncio
async def test_f9_assist_on_short_circuits_and_grounds() -> None:
    host = _default_host()
    host.log = _prior_read_log()
    out = await _bridge(host, assist=True).route(_tc("file_read", call_id="r2", path="a.txt"))
    assert out.decision is BridgeDecision.EXECUTED
    assert host.executor.calls == []  # deduped — executor NOT called
    assert "a.txt" in host.executor.grounded  # read grounded (closes the write loop)
    assert out.observation is not None and out.observation.action_id == out.action.id  # type: ignore[union-attr]


# -- dangling / missing observation -------------------------------------------
def test_reconcile_dangling_returns_interrupted_error() -> None:
    action = ActionEvent(thought="", tool_call=_tc("shell_exec", command="make"))
    err = PiToolBridge.reconcile_dangling_action([action])
    assert isinstance(err, AgentErrorEvent)
    assert err.action_id == action.id
    assert "INTERRUPTED" in err.error and "NOT re-executed" in err.error


def test_reconcile_no_dangle_when_paired() -> None:
    action = ActionEvent(thought="", tool_call=_tc("shell_exec", command="make"))
    obs = ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name="shell_exec", success=True, content="ok"),
        action_id=action.id,
    )
    assert PiToolBridge.reconcile_dangling_action([action, obs]) is None


def test_reconcile_skips_confirmation_pending_action() -> None:
    # a confirmation-pending action is awaiting the confirm/reject surface, not dangling.
    # Real emission order (engine.py:1049-1054): action FIRST, then the gate status.
    action = ActionEvent(thought="", tool_call=_tc("shell_exec", command="rm -rf /home/u/out"))
    status = StatusEvent(status=ConversationStatus.WAITING_FOR_CONFIRMATION, detail=action.id)
    assert PiToolBridge.reconcile_dangling_action([action, status]) is None


# -- integration adapter (from_loop) -----------------------------------------
class _StubDriver:
    def planning_allowed_tool_names(self) -> frozenset[str]:
        return frozenset({"file_read"})


class _StubLoop:
    """A minimal AgentLoop-shaped object for the from_loop adapter."""

    def __init__(self) -> None:
        self.executor = FakeExecutor(scope="sandbox")
        self.analyzer = RuleBasedAnalyzer()
        self.policy = BlastRadiusConfirm()
        self.mode = OperatingMode.LONG_HORIZON
        self._plan_tool = "submit_plan"
        self._driver = _StubDriver()
        self.log: list[Event] = []
        self._lock = asyncio.Lock()
        self.reenter_calls = 0

    async def _emit(self, event: Event) -> None:
        self.log.append(event)

    async def _events(self) -> list[Event]:
        return list(self.log)

    async def get_state(self) -> ConversationState:
        # Pure reconstruct from the log (no terminal StatusEvent → IDLE = drivable),
        # exactly like AgentLoop.get_state delegating to the store.
        return ConversationState.reconstruct("c", self.log)

    async def _maybe_reenter_planning_for_followup(self, events: list[Event]) -> bool:
        # The adapter delegates the replan decision here; the stub records the call
        # to prove the bridge INHERITS the loop's real gate (no re-implementation).
        self.reenter_calls += 1
        return False


@pytest.mark.asyncio
async def test_from_loop_routes_through_wrapper() -> None:
    loop = _StubLoop()
    bridge = PiToolBridge.from_loop(loop)
    out = await bridge.route(_tc("file_write", path="a.txt", content="hi"))
    assert out.decision is BridgeDecision.EXECUTED
    assert [c.tool_name for c in loop.executor.calls] == ["file_write"]
    # the bridge INHERITED the loop's real replan gate (delegated, not re-impl'd)
    assert loop.reenter_calls == 1
    # live mode flip is honored: planning gate now blocks a write
    loop.mode = OperatingMode.PLANNING
    out2 = await bridge.route(_tc("file_write", call_id="c2", path="b.txt", content="x"))
    assert out2.decision is BridgeDecision.REFUSED


# =====================================================================
# Parity-drift fixes (P0/#1, P0/#2, P0/#3, P1/#4, P1/#5, P2/#6).
# Each proves a Pi tool call is gated EXACTLY as a Disco-loop call: same
# gates, same ORDER, fail-CLOSED when state can't be determined.
# =====================================================================

from disco.agent_server.build_kernel.pi_kernel import (  # noqa: E402
    _PI_VIRTUAL_TOOLS,
    PiKernel,
)

# Disco virtuals that are NOT in the planning allowlist — finish/serve/remember/
# notify_user/delegate_explore write memory / emit deliverables / dispatch helpers
# and MUST be rejected (not virtual-handled) in PLANNING (engine.py:1309 before
# the meta handlers engine.py:1325).
_NON_ALLOWLISTED_VIRTUALS = ["finish", "serve", "remember", "notify_user", "delegate_explore"]


# -- P0/#1: planning gate runs BEFORE the virtual short-circuit ---------------
@pytest.mark.parametrize("name", _NON_ALLOWLISTED_VIRTUALS)
@pytest.mark.asyncio
async def test_planning_rejects_non_allowlisted_virtual(name: str) -> None:
    # In PLANNING a non-allowlisted virtual is REJECTED, NOT short-circuited to
    # VIRTUAL — the planning gate fires first (parity with engine.py:1309/1325).
    host = _default_host(mode=OperatingMode.PLANNING)
    bridge = _bridge(host, virtual=_PI_VIRTUAL_TOOLS)
    out = await bridge.route(_tc(name))
    assert out.decision is BridgeDecision.REFUSED  # NOT VIRTUAL
    assert host.executor.calls == []
    assert out.error is not None and "PLANNING" in out.error.error
    # action + paired error (KV-stable tool pairing), exactly like Disco's reject
    assert isinstance(host.log[0], ActionEvent)
    assert isinstance(host.log[1], AgentErrorEvent)
    assert host.log[1].action_id == host.log[0].id


@pytest.mark.parametrize("name", _NON_ALLOWLISTED_VIRTUALS)
@pytest.mark.asyncio
async def test_execution_mode_virtual_short_circuits(name: str) -> None:
    # In EXECUTION mode the planning gate is a no-op → the same virtual SHORT-
    # CIRCUITS to VIRTUAL (loop-handled), never executed.
    host = _default_host()  # LONG_HORIZON (execution)
    bridge = _bridge(host, virtual=_PI_VIRTUAL_TOOLS)
    out = await bridge.route(_tc(name))
    assert out.decision is BridgeDecision.VIRTUAL
    assert host.executor.calls == []
    assert host.log == []  # nothing emitted


@pytest.mark.asyncio
async def test_planning_allowlisted_virtual_survives_to_short_circuit() -> None:
    # ask_user IS in the planning allowlist → it survives the planning gate and
    # reaches the virtual short-circuit (VIRTUAL), proving the gate runs first AND
    # lets the allowlisted virtuals through (parity: engine falls through then the
    # ask_user handler runs).
    host = _default_host(
        mode=OperatingMode.PLANNING, planning_allowed=frozenset({"file_read", "ask_user"})
    )
    bridge = _bridge(host, virtual=frozenset({"ask_user"}))
    out = await bridge.route(_tc("ask_user"))
    assert out.decision is BridgeDecision.VIRTUAL
    assert host.executor.calls == []
    assert host.log == []  # neither rejected nor executed


# -- P0/#2: terminal/halted-state gate (fail-closed) --------------------------
@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        ConversationStatus.AWAITING_USER_QUESTION,
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.PAUSED,
        ConversationStatus.FINISHED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
    ],
)
@pytest.mark.asyncio
async def test_halted_state_refuses_without_executing(status: ConversationStatus) -> None:
    host = _default_host(status=status)
    host.executor.scope = "sandbox"  # would auto-approve if it ever reached risk
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content="x"))
    assert out.decision is BridgeDecision.HALTED
    assert host.executor.calls == []  # NEVER executed
    assert host.log == []  # nothing emitted (no action to pair)


@pytest.mark.asyncio
async def test_halted_confirmation_carries_pending_action_id() -> None:
    host = _default_host(status=ConversationStatus.WAITING_FOR_CONFIRMATION)
    host.pending_action_id = "act-123"
    out = await _bridge(host).route(_tc("file_read", path="a.txt"))
    assert out.decision is BridgeDecision.HALTED
    assert out.pending_action_id == "act-123"  # caller routes to the confirm surface
    assert host.executor.calls == []


@pytest.mark.asyncio
async def test_idle_state_is_drivable() -> None:
    host = _default_host(status=ConversationStatus.IDLE)  # IDLE = ready, not halted
    host.executor.scope = "sandbox"
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content="x"))
    assert out.decision is BridgeDecision.EXECUTED


@pytest.mark.asyncio
async def test_indeterminate_state_fails_closed() -> None:
    host = _default_host(state_raises=True)  # state() raises → must REFUSE
    host.executor.scope = "sandbox"
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content="x"))
    assert out.decision is BridgeDecision.HALTED
    assert host.executor.calls == []


# -- P0/#3: stale-plan / change-steer replan gate -----------------------------
def _approved_plan_then_user(text: str) -> list[Event]:
    return [
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=1),
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content=text),
            seq=2,
        ),
    ]


@pytest.mark.asyncio
async def test_stale_plan_write_blocked_after_change_request() -> None:
    # A write landing against an OLD approved plan after the user asked for a CHANGE
    # is BLOCKED: the replan gate re-enters PLANNING, then the planning gate rejects.
    host = _default_host(wire_replan=True)
    host.log = _approved_plan_then_user("actually, change the header to blue")
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content="x"))
    assert out.decision is BridgeDecision.REFUSED  # never lands on the stale plan
    assert host.executor.calls == []
    assert host.mode is OperatingMode.PLANNING  # the replan gate re-entered planning
    assert out.error is not None and "PLANNING" in out.error.error


@pytest.mark.asyncio
async def test_qa_followup_does_not_block_write() -> None:
    # A pure Q&A follow-up is EXEMPT (is_revision_intent False) — the write proceeds.
    host = _default_host(wire_replan=True)
    host.executor.scope = "sandbox"
    host.log = _approved_plan_then_user("what font did you use?")
    out = await _bridge(host).route(_tc("file_write", path="a.txt", content="x"))
    assert out.decision is BridgeDecision.EXECUTED
    assert host.mode is OperatingMode.LONG_HORIZON  # stayed in execution


# -- P1/#4: plan_step / update_plan_progress are REAL executor tools ----------
def test_plan_progress_tools_are_not_virtual() -> None:
    assert "plan_step" not in _PI_VIRTUAL_TOOLS
    assert "update_plan_progress" not in _PI_VIRTUAL_TOOLS


@pytest.mark.parametrize("name", ["plan_step", "update_plan_progress"])
@pytest.mark.asyncio
async def test_plan_progress_routes_as_real_tool(name: str) -> None:
    host = _default_host()
    host.executor.scope = "sandbox"
    host.executor._known.add(name)  # a real executor tool
    bridge = _bridge(host, virtual=_PI_VIRTUAL_TOOLS)
    out = await bridge.route(_tc(name, step=1))
    assert out.decision is BridgeDecision.EXECUTED  # executed, NOT short-circuited
    assert [c.tool_name for c in host.executor.calls] == [name]


# -- P1/#5: an F9-setup raise must not leave a dangling ActionEvent -----------
@pytest.mark.asyncio
async def test_f9_readonly_raise_does_not_dangle() -> None:
    host = _default_host()
    host.log = _prior_read_log()

    def _boom() -> frozenset[str]:
        raise RuntimeError("readonly listing failed")

    host.executor.readonly_tool_names = _boom  # type: ignore[method-assign]
    out = await _bridge(host, assist=True).route(_tc("file_read", call_id="r2", path="a.txt"))
    # readonly() raised → caught → None → F9 falls back to _WORKSPACE_READ_TOOLS and
    # dedups; either way the action is PAIRED, never dangling, never propagating.
    assert out.decision is BridgeDecision.EXECUTED
    assert PiToolBridge.reconcile_dangling_action(host.log) is None  # NO dangle


# -- P2/#6: dangling reconciliation wired (route guard + resume hook) ---------
@pytest.mark.asyncio
async def test_route_reconciles_dangling_before_new_call() -> None:
    host = _default_host()
    host.executor.scope = "sandbox"
    dangling = ActionEvent(thought="", tool_call=_tc("shell_exec", call_id="d1", command="make"))
    host.log = [dangling]  # crash between action emit and observation
    out = await _bridge(host).route(_tc("file_write", call_id="c2", path="a.txt", content="x"))
    assert out.decision is BridgeDecision.EXECUTED
    paired = [e for e in host.log if isinstance(e, AgentErrorEvent) and e.action_id == dangling.id]
    assert len(paired) == 1 and "INTERRUPTED" in paired[0].error  # paired BEFORE new call
    # idempotent: a second call does not double-pair
    await _bridge(host).route(_tc("file_read", call_id="c3", path="a.txt"))
    again = [e for e in host.log if isinstance(e, AgentErrorEvent) and e.action_id == dangling.id]
    assert len(again) == 1


@pytest.mark.asyncio
async def test_kernel_reconcile_on_resume_appends_idempotently() -> None:
    import types

    loop = _StubLoop()
    dangling = ActionEvent(thought="", tool_call=_tc("shell_exec", command="make"))
    loop.log = [dangling]
    kernel = PiKernel(types.SimpleNamespace(_loop_for=lambda _cid: loop))
    ev = await kernel.reconcile_dangling_on_resume("c")
    assert isinstance(ev, AgentErrorEvent) and ev.action_id == dangling.id
    assert loop.log[-1] is ev  # appended
    # idempotent — second call is a no-op
    assert await kernel.reconcile_dangling_on_resume("c") is None
    assert sum(isinstance(e, AgentErrorEvent) for e in loop.log) == 1
