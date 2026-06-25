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


# ---------------------------------------------------------------------------
# Happy path — write then read; Action/Observation appended, observation returned.
# ---------------------------------------------------------------------------


async def test_bridge_write_then_read_appends_action_and_observation(tmp_path) -> None:
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)

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


async def test_gate_tool_without_handler_returns_not_yet_wired(tmp_path) -> None:
    """ask_user is allowlisted but its gate handler lands in the E batch; until then
    the bridge returns a structured 'not yet wired' placeholder (not executed)."""
    store = SqliteEventStore(path=str(tmp_path / "events.db"))
    runtime, token_store, token = _new_convo(store)
    app = _make_app(token_store, runtime)

    resp = await _post(
        app, KERNEL, "ask_user", token,
        {"call_id": "tc-ask", "arguments": {"question": "which framework?"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["structured"]["kind"] == "not_yet_wired"
    events = await store.get_events(CID)
    assert [e for e in events if isinstance(e, ActionEvent)] == []


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
