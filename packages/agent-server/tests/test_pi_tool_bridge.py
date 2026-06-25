"""PR D2 + D3 — the Pi tool bridge endpoint.

Proves the loopback, run-scoped seam that lets a Pi custom tool drive ONE Disco
tool call through the conversation's real ``DefaultToolExecutor``:

  * a valid bridge call (``file_write`` then ``file_read``) appends an
    ActionEvent + ObservationEvent pair and returns the observation to Pi — the
    write is REAL (ProcessSandbox) and the read returns its bytes;
  * a tool outside the D3 allowlist is REFUSED with a structured ``success=false``
    result and is never executed (no events appended);
  * a non-loopback peer / missing / invalid token is rejected (403/401), mirroring
    the inference-gateway test;
  * an executor FAILURE (file_read of a missing path) never raises — the bridge
    returns a structured error result AND appends an AgentErrorEvent.

Real composition (ConversationRuntime + DefaultToolExecutor + agent tools +
ProcessSandbox); the model is never invoked (Pi drives the tools via the bridge).
The endpoint is driven over ``ASGITransport`` with no real sockets.
"""

from __future__ import annotations

import httpx
from disco.agent_server import ConversationRuntime
from disco.agent_server.pi_inference import PiInferenceTokenStore
from disco.agent_server.routes.pi_tools import make_pi_tools_router
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ObservationEvent,
    SqliteEventStore,
)
from disco.core.llm import DefaultLLMRouter, ModelEntry, RouterConfig
from disco.tools import ProcessSandboxService
from fastapi import FastAPI

CID = "c1"
KERNEL = "k1"
MODEL_KEY = "m"


class _IdleProvider:
    """A model double that is never consulted (Pi drives the tools via the bridge);
    present only so the build loop composes."""

    name = "fake"

    async def complete(self, req, *, model):  # pragma: no cover - never called
        raise AssertionError("the bridge must not invoke the model")

    async def stream_complete(self, req, *, model):  # pragma: no cover - never called
        raise AssertionError("the bridge must not invoke the model")
        yield  # noqa: unreachable — marks this an async generator

    def supports(self, requirement, *, model):
        return True


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    cfg = RouterConfig(
        models={MODEL_KEY: ModelEntry(model_id=MODEL_KEY, provider="fake", context_window=8192)},
        default_model=MODEL_KEY,
    )
    router = DefaultLLMRouter(cfg, {"fake": _IdleProvider()})
    return ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())


def _make_app(token_store: PiInferenceTokenStore, runtime: ConversationRuntime) -> FastAPI:
    app = FastAPI()
    app.include_router(make_pi_tools_router(token_store, runtime))
    return app


async def _post(
    app: FastAPI,
    kernel_id: str,
    tool_name: str,
    token: str | None,
    body: dict,
    *,
    client=("127.0.0.1", 5555),
):
    transport = httpx.ASGITransport(app=app, client=client)
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as c:
        return await c.post(
            f"/internal/pi-kernel/{kernel_id}/tools/{tool_name}", json=body, headers=headers
        )


def _new_convo(store: SqliteEventStore) -> tuple[ConversationRuntime, PiInferenceTokenStore, str]:
    store.create_conversation(CID, owner_id="local")
    runtime = _runtime(store)
    runtime.set_surface(CID, "build")
    token_store = PiInferenceTokenStore()
    runtime.attach_pi_token_store(token_store)
    token = token_store.issue(
        kernel_id=KERNEL, conversation_id=CID, model_key=MODEL_KEY, ttl_s=60, budget_tokens=1000
    )
    return runtime, token_store, token


async def _drive_plan_approval(app, store, runtime, token, *, steps=None) -> None:
    """Submit a plan over the bridge and approve it — the realistic path into
    execution mode. After this the build loop's mode is EXECUTION, so the bridge's
    REAL planning gate (loop.mode == PLANNING) no longer blocks writes/shell. Mirrors
    a Pi run: submit_plan parks at AWAITING_PLAN_APPROVAL; approve_plan flips the loop
    and resolves the held submit_plan call."""
    import asyncio

    from disco.core import ConversationStatus

    plan_args = {"summary": "go", "steps": steps or [{"title": "scaffold"}]}
    post = asyncio.create_task(
        _post(
            app, KERNEL, "submit_plan", token,
            {"call_id": "plan", "arguments": plan_args},
        )
    )
    await _wait_status(store, ConversationStatus.AWAITING_PLAN_APPROVAL)
    await runtime._pi_kernel.approve_plan(CID)
    await post  # the held submit_plan returns its verdict; loop now in execution mode


# ---------------------------------------------------------------------------
# Happy path — write then read; Action/Observation appended, observation returned.
# EPIC-D graft: writes are gated by the bridge's REAL planning gate, so the write
# only lands AFTER a plan is approved (loop.mode flips to execution). This proves
# the full safe path end-to-end (plan → approve → write → read), not a bare execute.
# ---------------------------------------------------------------------------


async def test_bridge_write_then_read_appends_action_and_observation(tmp_path) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)

    # Approve a plan first — the bridge's planning gate blocks writes until then.
    await _drive_plan_approval(app, store, runtime, token)

    # file_write — a REAL write into the ProcessSandbox workspace.
    w = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "tc-write", "arguments": {"path": "hello.txt", "content": "hello world"}},
    )
    assert w.status_code == 200
    wbody = w.json()
    assert wbody["success"] is True
    assert wbody["tool_name"] == "file_write"
    assert wbody["call_id"] == "tc-write"

    # file_read — returns the bytes just written.
    r = await _post(
        app, KERNEL, "file_read", token,
        {"call_id": "tc-read", "arguments": {"path": "hello.txt"}},
    )
    assert r.status_code == 200
    rbody = r.json()
    assert rbody["success"] is True
    assert rbody["tool_name"] == "file_read"
    assert "hello world" in rbody["content"]

    # Both calls appended an ActionEvent + ObservationEvent pair, correlated by id.
    events = await store.get_events(CID)
    actions = [e for e in events if isinstance(e, ActionEvent)]
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert [a.tool_call.tool_name for a in actions] == ["file_write", "file_read"]
    assert len(obs) == 2
    # Pairing: each observation's action_id points at its action; tool names align.
    by_action = {o.action_id: o for o in obs}
    for a in actions:
        assert a.id in by_action
        assert by_action[a.id].tool_result.tool_name == a.tool_call.tool_name
        assert by_action[a.id].tool_result.success is True


# ---------------------------------------------------------------------------
# D3 allowlist — an out-of-set tool is refused, not executed.
# ---------------------------------------------------------------------------


async def test_out_of_allowlist_tool_is_refused_not_executed(tmp_path) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)

    resp = await _post(
        app, KERNEL, "shell",  # 'shell' exists in Disco but is NOT in the Pi set
        token, {"call_id": "tc-x", "arguments": {"command": "echo nope"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False  # refused
    assert body["structured"]["kind"] == "tool_not_allowed"
    # Nothing was executed: no Action/Observation appended for the blocked call.
    events = await store.get_events(CID)
    assert [e for e in events if isinstance(e, ActionEvent)] == []
    assert [e for e in events if isinstance(e, ObservationEvent)] == []


# ---------------------------------------------------------------------------
# E1/E2/E3 — the plan gate + ask/clarify gates (held PYTHON-side over the bridge).
# ---------------------------------------------------------------------------


async def _wait_status(store: SqliteEventStore, status, timeout: float = 5.0) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        st = await store.get_state(CID)
        if st.execution_status is status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"status {status} not reached within {timeout}s")


async def _wait_confirm_gate(pk, timeout: float = 5.0) -> None:
    """Wait until the held bridge request has REGISTERED its confirm gate (the
    WAITING_FOR_CONFIRMATION status becomes visible a hair BEFORE the gate future is
    parked, so confirm/reject must wait for the registration to avoid a no-op race —
    a race that only exists in this tightly-coupled test; in production the user's
    confirm arrives long after)."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        gate = pk._confirm_gates.get(CID)
        if gate is not None and not gate.done() and pk._pending_actions.get(CID) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("confirm gate not registered within timeout")


async def test_submit_plan_holds_then_approve_resumes(tmp_path) -> None:
    """submit_plan appends a PlanEvent + AWAITING_PLAN_APPROVAL and LONG-POLLS; while
    parked, a write tool is refused; approve_plan resolves the held call (Pi resumes)
    and unblocks writes."""
    import asyncio

    from disco.core import ConversationStatus, PlanEvent

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel

    post = asyncio.create_task(
        _post(
            app, KERNEL, "submit_plan", token,
            {"call_id": "p1",
             "arguments": {"summary": "ship it", "steps": [{"title": "scaffold"}]}},
        )
    )
    await _wait_status(store, ConversationStatus.AWAITING_PLAN_APPROVAL)
    # A PlanEvent was appended, and writes are blocked while unapproved.
    events = await store.get_events(CID)
    assert any(isinstance(e, PlanEvent) for e in events)
    assert pk.writes_blocked(CID) is True
    # While parked at AWAITING_PLAN_APPROVAL, the conversation is NOT drivable, so the
    # bridge's HALTED-state gate refuses the write WITHOUT executing (no action emitted).
    w = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "w1", "arguments": {"path": "x.txt", "content": "no"}},
    )
    assert w.json()["structured"]["kind"] == "halted"
    assert [e for e in (await store.get_events(CID)) if isinstance(e, ActionEvent)] == []

    # Approve → the held submit_plan returns its verdict; writes now allowed.
    await pk.approve_plan(CID)
    resp = await post
    body = resp.json()
    assert body["success"] is True
    assert "approved" in body["content"].lower()
    assert pk.writes_blocked(CID) is False
    w2 = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "w2", "arguments": {"path": "x.txt", "content": "yes"}},
    )
    assert w2.json()["success"] is True


async def test_reject_plan_forces_replan_and_keeps_writes_blocked(tmp_path) -> None:
    """reject_plan returns a 'submit a revised plan, do NOT write' verdict; writes stay
    blocked until a NEW plan is submitted AND approved."""
    import asyncio

    from disco.core import ConversationStatus

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel

    post = asyncio.create_task(
        _post(
            app, KERNEL, "submit_plan", token,
            {"call_id": "p1", "arguments": {"summary": "v1", "steps": ["a", "b"]}},
        )
    )
    await _wait_status(store, ConversationStatus.AWAITING_PLAN_APPROVAL)
    await pk.reject_plan(CID, "no tests")
    body = (await post).json()
    assert "reject" in body["content"].lower()
    assert pk.writes_blocked(CID) is True  # still blocked after rejection
    # After rejection the loop is RUNNING but mode is still PLANNING (never approved),
    # so the bridge's REAL planning gate refuses the write (not the old narrow gate).
    w = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "w1", "arguments": {"path": "x.txt", "content": "no"}},
    )
    assert w.json()["structured"]["kind"] == "refused_by_gate"

    # A REVISED plan, then approval, finally unblocks writes.
    post2 = asyncio.create_task(
        _post(
            app, KERNEL, "submit_plan", token,
            {"call_id": "p2", "arguments": {"summary": "v2", "steps": ["a", "b", "tests"]}},
        )
    )
    await _wait_status(store, ConversationStatus.AWAITING_PLAN_APPROVAL)
    await pk.approve_plan(CID)
    assert (await post2).json()["success"] is True
    assert pk.writes_blocked(CID) is False


async def test_must_submit_plan_before_write(tmp_path) -> None:
    """A managed Pi conversation with NO approved plan refuses write tools — now via the
    bridge's REAL planning gate (loop.mode == PLANNING). The refusal is audited as an
    ActionEvent + AgentErrorEvent pair (KV-stable, exactly like Disco's planning gate),
    but the tool is NEVER executed (no ObservationEvent)."""
    from disco.core import ObservationEvent

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    runtime._pi_kernel._managed.add(CID)  # the kernel is driving this conversation

    resp = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "w", "arguments": {"path": "x.txt", "content": "premature"}},
    )
    assert resp.json()["success"] is False
    assert resp.json()["structured"]["kind"] == "refused_by_gate"
    # The planning gate refused it: NOTHING executed (no observation), but the proposed
    # action + the refusal are recorded for audit (mirror engine.py:945-969).
    events = await store.get_events(CID)
    assert [e for e in events if isinstance(e, ObservationEvent)] == []  # never executed
    actions = [e for e in events if isinstance(e, ActionEvent)]
    errors = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(actions) == 1 and len(errors) == 1
    assert errors[0].action_id == actions[0].id
    assert "PLANNING" in errors[0].error


async def test_ask_user_holds_then_user_turn_resolves(tmp_path) -> None:
    """ask_user parks at AWAITING_USER_QUESTION; the user's next turn (send_user_turn)
    IS the answer and resolves the held call."""
    import asyncio

    from disco.core import ConversationStatus

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel

    post = asyncio.create_task(
        _post(
            app, KERNEL, "ask_user", token,
            {"call_id": "a1", "arguments": {"question": "which framework?"}},
        )
    )
    await _wait_status(store, ConversationStatus.AWAITING_USER_QUESTION)
    await pk.send_user_turn(CID, "react")
    assert (await post).json()["content"] == "react"


async def test_clarify_holds_then_user_turn_resolves(tmp_path) -> None:
    """clarify appends a ClarifyEvent + AWAITING_USER_QUESTION and long-polls until the
    user answers."""
    import asyncio

    from disco.core import ClarifyEvent, ConversationStatus

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel

    post = asyncio.create_task(
        _post(
            app, KERNEL, "clarify", token,
            {"call_id": "c1", "arguments": {"question": "details?",
                                            "items": [{"id": "q1", "question": "target?"}]}},
        )
    )
    await _wait_status(store, ConversationStatus.AWAITING_USER_QUESTION)
    assert any(isinstance(e, ClarifyEvent) for e in (await store.get_events(CID)))
    await pk.send_user_turn(CID, "web app")
    assert (await post).json()["content"] == "web app"


# ---------------------------------------------------------------------------
# Auth + loopback — mirrors the inference-gateway test.
# ---------------------------------------------------------------------------


async def test_remote_peer_rejected_even_with_valid_token(tmp_path) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    resp = await _post(
        app, KERNEL, "file_list", token, {"call_id": "tc", "arguments": {}},
        client=("8.8.8.8", 443),  # a remote peer
    )
    assert resp.status_code == 403
    assert (await store.get_events(CID)) == []


async def test_missing_and_invalid_token_401(tmp_path) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    body = {"call_id": "tc", "arguments": {}}
    assert (await _post(app, KERNEL, "file_list", None, body)).status_code == 401
    assert (await _post(app, KERNEL, "file_list", "bogus", body)).status_code == 401
    token_store.revoke(token)
    assert (await _post(app, KERNEL, "file_list", token, body)).status_code == 401
    assert (await store.get_events(CID)) == []


async def test_token_for_other_kernel_rejected(tmp_path) -> None:
    """A token minted for kernel k1 cannot drive a different kernel's tools."""
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    resp = await _post(
        app, "k2-other", "file_list", token, {"call_id": "tc", "arguments": {}}
    )
    assert resp.status_code == 403
    assert (await store.get_events(CID)) == []


# ---------------------------------------------------------------------------
# Executor failure never raises — structured error + AgentErrorEvent.
# ---------------------------------------------------------------------------


async def test_executor_failure_returns_structured_error_and_agent_error_event(tmp_path) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)

    # file_read of a path that does not exist → the executor returns success=false
    # (it never raises); the bridge surfaces it as a structured error result.
    resp = await _post(
        app, KERNEL, "file_read", token,
        {"call_id": "tc-miss", "arguments": {"path": "does/not/exist.txt"}},
    )
    assert resp.status_code == 200  # bridge did NOT raise / 500
    body = resp.json()
    assert body["success"] is False
    assert body["error"]  # a populated, model-readable error string

    # The Action was appended and paired with an AgentErrorEvent (NOT an Observation).
    events = await store.get_events(CID)
    actions = [e for e in events if isinstance(e, ActionEvent)]
    errors = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(actions) == 1
    assert [e for e in events if isinstance(e, ObservationEvent)] == []
    assert len(errors) == 1
    assert errors[0].action_id == actions[0].id
    assert errors[0].tool_call_id == "tc-miss"


# ===========================================================================
# EPIC-D GRAFT — the route now drives every Pi tool call through the in-process
# PiToolBridge (the FULL gate stack), NOT a bare executor.execute(). These prove
# the HTTP route CANNOT bypass any safety gate.
# ===========================================================================


async def test_route_cannot_bypass_hard_deny(tmp_path) -> None:
    """A catastrophic shell command is HARD-DENIED by the bridge before execution —
    the route can never reach the executor with it, even after plan approval."""
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    await _drive_plan_approval(app, store, runtime, token)

    resp = await _post(
        app, KERNEL, "shell_exec", token,
        {"call_id": "rm", "arguments": {"command": "rm -rf /"}},
    )
    body = resp.json()
    assert body["success"] is False
    assert body["structured"]["kind"] == "refused_by_gate"
    assert "hard-denied" in body["content"]
    # Never executed: no ObservationEvent for this call.
    events = await store.get_events(CID)
    assert all(
        not (isinstance(e, ObservationEvent) and e.tool_result.call_id == "rm") for e in events
    )


async def test_route_cannot_bypass_k1_elision_guard(tmp_path) -> None:
    """A file_write whose content is ONLY an internal elision placeholder is REFUSED
    (data-loss guard); with no prior real content to recover, it never executes."""
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    await _drive_plan_approval(app, store, runtime, token)

    marker = "<4096 chars elided — re-issue the call or file_read the path for full content>"
    resp = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "k1", "arguments": {"path": "a.txt", "content": marker}},
    )
    body = resp.json()
    assert body["success"] is False
    assert body["structured"]["kind"] == "refused_by_gate"
    assert "elision placeholder" in body["content"]
    # The marker was NOT written — no successful observation for this call.
    events = await store.get_events(CID)
    assert all(
        not (isinstance(e, ObservationEvent) and e.tool_result.call_id == "k1") for e in events
    )


async def test_route_cannot_bypass_halted_state(tmp_path) -> None:
    """While the conversation is parked AWAITING_USER_QUESTION (an ask_user gate held),
    a normal tool call is REFUSED by the bridge's halted-state gate without executing."""
    import asyncio

    from disco.core import ConversationStatus

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel

    ask = asyncio.create_task(
        _post(
            app, KERNEL, "ask_user", token,
            {"call_id": "a1", "arguments": {"question": "which?"}},
        )
    )
    await _wait_status(store, ConversationStatus.AWAITING_USER_QUESTION)
    # A read tool while parked → HALTED (not drivable), never executed.
    r = await _post(
        app, KERNEL, "file_list", token, {"call_id": "ls", "arguments": {}},
    )
    body = r.json()
    assert body["success"] is False
    assert body["structured"]["kind"] == "halted"
    assert [e for e in (await store.get_events(CID)) if isinstance(e, ActionEvent)] == []
    # Release the gate so the held ask request returns cleanly.
    await pk.send_user_turn(CID, "answer")
    await ask


async def test_route_cannot_bypass_stale_plan_replan(tmp_path) -> None:
    """After a plan is approved, a CHANGE follow-up re-enters PLANNING (the bridge's
    real replan gate) so a subsequent write is REFUSED on the stale plan."""
    from disco.core import EventSource, LLMMessage, MessageEvent

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    await _drive_plan_approval(app, store, runtime, token)

    # A REVISION-intent follow-up lands while the build is on an approved plan.
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="actually, change the header color to blue"),
        ),
    )
    resp = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "w", "arguments": {"path": "x.txt", "content": "stale"}},
    )
    body = resp.json()
    assert body["success"] is False
    assert body["structured"]["kind"] == "refused_by_gate"  # re-entered planning → write rejected
    assert "PLANNING" in body["content"]
    # The loop really re-entered planning mode.
    assert runtime._loops[CID].mode.name == "PLANNING"


async def test_route_confirm_required_then_confirm_executes(tmp_path) -> None:
    """A risk-gated call HALTs pending (WAITING_FOR_CONFIRMATION); a REAL confirm
    continuation executes the SAME action and returns its result (not a no-op)."""
    import asyncio

    from disco.core import ConversationStatus
    from disco.core.loop import AlwaysConfirm

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel
    await _drive_plan_approval(app, store, runtime, token)
    # Force the per-action gate to fire (the sandbox policy auto-approves otherwise).
    # AlwaysConfirm gates EVERY action (incl. the read-back below), so capture the real
    # policy and restore it before the verification read — otherwise that read would
    # itself park at the confirm gate with nobody to resolve it.
    original_policy = runtime._loops[CID].policy
    runtime._loops[CID].policy = AlwaysConfirm()

    post = asyncio.create_task(
        _post(
            app, KERNEL, "file_write", token,
            {"call_id": "wc", "arguments": {"path": "c.txt", "content": "gated"}},
        )
    )
    await _wait_status(store, ConversationStatus.WAITING_FOR_CONFIRMATION)
    await _wait_confirm_gate(pk)
    # The proposed action is in the log but NOT yet executed (no observation).
    pre = await store.get_events(CID)
    pre_obs = [e for e in pre if isinstance(e, ObservationEvent) and e.tool_result.call_id == "wc"]
    assert pre_obs == []
    # Confirm → the held request returns the executed result.
    await pk.confirm(CID)
    body = (await post).json()
    assert body["success"] is True
    assert body["call_id"] == "wc"
    # Restore the auto-approving policy so the verification read isn't itself gated.
    runtime._loops[CID].policy = original_policy
    # The write really landed.
    r = await _post(
        app, KERNEL, "file_read", token, {"call_id": "rc", "arguments": {"path": "c.txt"}}
    )
    assert "gated" in r.json()["content"]


async def test_route_confirm_required_then_reject_denies(tmp_path) -> None:
    """Rejecting a risk-gated call denies it WITHOUT executing — the held request
    returns a structured rejection and the action is paired (never dangling)."""
    import asyncio

    from disco.core import ConversationStatus
    from disco.core.loop import AlwaysConfirm

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel
    await _drive_plan_approval(app, store, runtime, token)
    # AlwaysConfirm gates EVERY action; capture the real policy so the read-back
    # verification below isn't itself parked at the confirm gate (it would never
    # resolve and would hang the test).
    original_policy = runtime._loops[CID].policy
    runtime._loops[CID].policy = AlwaysConfirm()

    post = asyncio.create_task(
        _post(
            app, KERNEL, "file_write", token,
            {"call_id": "wr", "arguments": {"path": "r.txt", "content": "nope"}},
        )
    )
    await _wait_status(store, ConversationStatus.WAITING_FOR_CONFIRMATION)
    await _wait_confirm_gate(pk)
    await pk.reject(CID, "not allowed")
    body = (await post).json()
    assert body["success"] is False
    assert body["structured"]["kind"] == "rejected"
    # Restore the auto-approving policy so the verification read isn't itself gated.
    runtime._loops[CID].policy = original_policy
    # Never written; the proposed action is paired with an AgentErrorEvent.
    rd = await _post(
        app, KERNEL, "file_read", token, {"call_id": "rr", "arguments": {"path": "r.txt"}}
    )
    assert rd.json()["success"] is False  # file does not exist


async def test_approve_plan_is_fail_closed_without_pending_plan(tmp_path) -> None:
    """approve_plan is a NO-OP unless a plan is actually pending (P0/a): a spurious
    approve before any submit_plan must NOT open the workspace; writes stay blocked."""
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    pk = runtime._pi_kernel
    pk._managed.add(CID)

    # No submit_plan happened — approve_plan must be ignored (fail-closed).
    await pk.approve_plan(CID)
    assert pk._plan_approved.get(CID) is not True
    assert pk.writes_blocked(CID) is True

    # A write is still refused by the planning gate (loop never left PLANNING).
    resp = await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "w", "arguments": {"path": "x.txt", "content": "premature"}},
    )
    assert resp.json()["structured"]["kind"] == "refused_by_gate"
    events = await store.get_events(CID)
    assert [e for e in events if isinstance(e, ObservationEvent)] == []  # never executed


async def test_finish_routes_through_dod_gate(tmp_path) -> None:
    """Pi's finish goes through the REAL Definition-of-Done gate (P1): an unmet spec
    BLOCKS finish (not FINISHED); with the spec satisfied, finish concludes."""
    from disco.core import ConversationStatus, DoDSpec
    from disco.core.dod import FileExistsPredicate

    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)
    await _drive_plan_approval(app, store, runtime, token)

    # Materialize the ProcessSandbox workspace with a real (unrelated) write — the DoD
    # gate grades against ``executor.sandbox.workspace_path``, which is lazily created on
    # the first file op. Without a workspace the gate degrades to "skip" (documented
    # production behaviour: refusing without evidence would be a silent fail), so finish
    # would NOT be blocked and the gate could never be exercised.
    await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "seed", "arguments": {"path": "scratch.txt", "content": "seed"}},
    )

    # A DoD that requires a file the workspace does NOT have → finish must be blocked.
    await store.set_dod_spec(
        CID, DoDSpec(predicates=[FileExistsPredicate(path="REQUIRED.txt")])
    )
    blocked = await _post(
        app, KERNEL, "finish", token, {"call_id": "f1", "arguments": {}}
    )
    body = blocked.json()
    assert body["success"] is False
    assert body["structured"]["kind"] == "dod_unmet"
    st = await store.get_state(CID)
    assert st.execution_status is not ConversationStatus.FINISHED  # NOT finished

    # Satisfy the DoD (create the required file via the bridge) → finish now concludes.
    await _post(
        app, KERNEL, "file_write", token,
        {"call_id": "mk", "arguments": {"path": "REQUIRED.txt", "content": "ok"}},
    )
    done = await _post(app, KERNEL, "finish", token, {"call_id": "f2", "arguments": {}})
    dbody = done.json()
    assert dbody["success"] is True
    assert dbody["structured"]["kind"] == "finished"
    await _wait_status(store, ConversationStatus.FINISHED)
