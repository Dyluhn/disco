"""EPIC C deferred finding C#3 — gateway-token revocation on conversation-end.

The DiscoInferenceGateway mints an ephemeral, run-scoped token that authorizes a Pi
kernel to drive the UI-selected model over the loopback gateway. That capability MUST
die the moment the run ends — otherwise a kernel keeps calling the model (on the
user's key + budget) until the token's TTL even after the conversation FINISHED /
ERROR / STUCK / IDLE / was cancelled / killed / deleted.

These tests wire a `PiInferenceTokenStore` onto a REAL `ConversationRuntime` (so the
shipped terminalizers / control ops / delete / shutdown / startup-reconcile run, not a
copy) and prove that a token issued for a conversation is REVOKED after every end
path — i.e. `validate` then raises ``InvalidGatewayToken("revoked")``, which is the
SOLE auth check the gateway performs, so the same token now 401s. One end-to-end test
drives the real HTTP gateway to confirm the runtime-side revoke really yields a 401.

The generation-guard tie-in is proven directly: a STALE old-generation terminalizer
must NOT revoke a token that now belongs to a NEWER run on the same conversation
(the revoke is co-located with `_unpin_if_current_generation`, sharing its guard).

No containers, no real sockets: the upstream provider is an ``httpx.MockTransport``
and the gateway is driven over ``ASGITransport``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.app import create_app
from disco.agent_server.pi_inference import InvalidGatewayToken, PiInferenceTokenStore
from disco.agent_server.routes.pi_inference import make_pi_inference_router
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm.config import ModelEntry, RouterConfig
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.tools import ProcessSandboxService
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio

CID = "conv-tok-revoke"


def _runtime(
    store: SqliteEventStore, token_store: PiInferenceTokenStore | None
) -> ConversationRuntime:
    """A real runtime with the gateway token store wired (or None for the None-safe
    test). A MagicMock router keeps construction inference-free."""
    return ConversationRuntime(
        store,
        router=MagicMock(),
        sandbox_service=ProcessSandboxService(),
        pi_token_store=token_store,
    )


async def _seed(store: SqliteEventStore, cid: str = CID) -> None:
    store.create_conversation(cid, owner_id="local")
    await store.append(
        cid,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="build it")),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))


def _issue(token_store: PiInferenceTokenStore, cid: str = CID) -> str:
    return token_store.issue(
        kernel_id="k1", conversation_id=cid, model_key="selected-model",
        ttl_s=3600, budget_tokens=1000,
    )


def _assert_revoked(token_store: PiInferenceTokenStore, token: str) -> None:
    """Prove the token can no longer authorize: `validate` (the gateway's only auth
    gate) raises with reason 'revoked' — which the endpoint maps to HTTP 401."""
    with pytest.raises(InvalidGatewayToken) as ei:
        token_store.validate(token)
    assert ei.value.reason == "revoked"


def _assert_live(token_store: PiInferenceTokenStore, token: str) -> None:
    rec = token_store.validate(token)  # raises if not live
    assert rec.revoked is False


# -- every conversation-end path revokes the token ----------------------------


async def test_cancel_revokes_token() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    token = _issue(ts)
    _assert_live(ts, token)
    await rt.cancel(CID)
    _assert_revoked(ts, token)


async def test_kill_revokes_token() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    token = _issue(ts)
    await rt.kill(CID)
    _assert_revoked(ts, token)
    # kill terminalizes to IDLE — the run actually ended.
    assert (await store.get_state(CID)).execution_status is ConversationStatus.IDLE


async def test_finished_finalize_revokes_token() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    rt._run_generation[CID] = 1
    token = _issue(ts)
    # The clean-return finalizer for the WINNING generation revokes (FINISHED is a
    # kernel-unpin status), co-located with the guarded pin clear.
    await rt._finalize_clean_return(CID, 1)
    _assert_revoked(ts, token)


async def test_stuck_finalize_revokes_token() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    await store.append(CID, StatusEvent(status=ConversationStatus.STUCK))
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    rt._run_generation[CID] = 1
    token = _issue(ts)
    await rt._finalize_clean_return(CID, 1)
    _assert_revoked(ts, token)


async def test_error_terminalize_revokes_token() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)  # still RUNNING — the crash terminalizer appends ERROR
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    rt._run_generation[CID] = 1
    token = _issue(ts)
    await rt._terminalize_crashed(CID, RuntimeError("boom"), 1)
    assert (await store.get_state(CID)).execution_status is ConversationStatus.ERROR
    _assert_revoked(ts, token)


async def test_forget_conversation_revokes_token() -> None:
    store = SqliteEventStore(":memory:")
    await _seed(store)
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    token = _issue(ts)
    await rt.forget_conversation(CID)  # conversation DELETE
    _assert_revoked(ts, token)


async def test_startup_reconcile_revokes_orphaned_token() -> None:
    """An orphaned RUNNING run reconciled to PAUSED on boot has its token revoked
    (defensive — normally the in-memory store is empty after a real restart, but a
    soft restart that reuses the store must not leave a live token behind)."""
    store = SqliteEventStore(":memory:")
    await _seed(store)  # latest status RUNNING == orphaned on boot
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    token = _issue(ts)
    await rt.reconcile_orphaned_runs()
    assert (await store.get_state(CID)).execution_status is ConversationStatus.PAUSED
    _assert_revoked(ts, token)


# -- the generation guard: a STALE terminalizer must NOT revoke a NEWER token -


async def test_stale_generation_terminalizer_does_not_revoke_newer_token() -> None:
    """C#3 CORE: `_terminalize_crashed`/`_finalize_clean_return` schedule async; before
    a stale one runs, a NEWER run (generation N+1) can reuse the conversation and issue a
    FRESH token. The stale terminalizer (carrying the OLD generation) loses the guard and
    must therefore NOT revoke — else it would kill the newer run's token. Revoke is
    co-located with `_unpin_if_current_generation`, so it shares the guard exactly."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    # A NEWER run (gen 2) already owns the conversation + holds a fresh token.
    rt._run_generation[CID] = 2
    newer_token = _issue(ts)

    # The STALE crash terminalizer for the OLD generation 1 runs now.
    await rt._terminalize_crashed(CID, RuntimeError("stale"), 1)

    # It bailed on the guard (no ERROR appended, no revoke) — the newer run is intact.
    assert (await store.get_state(CID)).execution_status is ConversationStatus.RUNNING
    _assert_live(ts, newer_token)

    # And the WINNING generation's terminalizer DOES revoke it.
    await rt._terminalize_crashed(CID, RuntimeError("real"), 2)
    _assert_revoked(ts, newer_token)


# -- None-safe when no token store is wired -----------------------------------


async def test_end_paths_are_none_safe_without_token_store() -> None:
    """No gateway store wired (the default / non-gateway + test paths): every end path
    is a harmless no-op, never an AttributeError/crash."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    rt = _runtime(store, None)  # no token store
    assert rt._pi_token_store is None
    rt._run_generation[CID] = 1
    await rt.cancel(CID)
    await rt._terminalize_crashed(CID, RuntimeError("x"), 1)
    await rt.forget_conversation(CID)
    await _seed(store)  # re-seed (forget dropped caches); exercise kill + finalize too
    await rt.kill(CID)
    rt._revoke_pi_tokens(CID)  # direct call is also a no-op


# -- end-to-end: a runtime-side revoke really yields a gateway 401 ------------

_SELECTED_KEY = "selected-model"
_PROVIDER_KEY_ENV = "PI_TEST_PROVIDER_KEY"


def _gateway_app(token_store: PiInferenceTokenStore, tmp_path) -> FastAPI:
    cfg = RouterConfig(
        models={
            _SELECTED_KEY: ModelEntry(
                model_id="vendor/real-7b", provider="testprov", context_window=8192,
                base_url="http://upstream.test/v1", api_key_env=_PROVIDER_KEY_ENV,
            )
        },
        default_model=_SELECTED_KEY,
    )
    config_store = ConfigStore(path=tmp_path / "none.json", base_factory=lambda: cfg)
    secret_store = SecretStore(path=tmp_path / "secrets.json", box=SecretBox(app_secret="t"))
    secret_store.set_secret(_PROVIDER_KEY_ENV, "sk-secret")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": [], "usage": {}})

    app = FastAPI()
    app.include_router(
        make_pi_inference_router(
            token_store, config_store=config_store, secret_store=secret_store,
            http_transport=httpx.MockTransport(_handler),
        )
    )
    return app


async def _post(app: FastAPI, token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as c:
        return await c.post(
            "/internal/pi-kernel/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "model": _SELECTED_KEY},
            headers={"authorization": f"Bearer {token}"},
        )


async def test_kill_makes_the_real_gateway_401(tmp_path) -> None:
    """End-to-end: ONE store serves both the gateway AND the runtime. A live token
    POSTs 200; after `runtime.kill`, the SAME token 401s at the real HTTP gateway."""
    store = SqliteEventStore(":memory:")
    await _seed(store)
    ts = PiInferenceTokenStore()
    rt = _runtime(store, ts)
    app = _gateway_app(ts, tmp_path)
    token = ts.issue(
        kernel_id="k1", conversation_id=CID, model_key=_SELECTED_KEY,
        ttl_s=3600, budget_tokens=1000,
    )

    assert (await _post(app, token)).status_code == 200  # live token works

    await rt.kill(CID)

    assert (await _post(app, token)).status_code == 401  # revoked → 401


# -- create_app wires the store onto the runtime + revokes all on shutdown ----


async def test_create_app_wires_store_and_revokes_all_on_shutdown() -> None:
    store = SqliteEventStore(":memory:")
    rt = _runtime(store, None)  # starts unwired; create_app must attach it
    app = create_app(store, runtime=rt)
    ts = app.state.pi_token_store
    # Wiring proof: the runtime now reaches the SAME store the gateway router serves.
    assert rt._pi_token_store is ts
    token = _issue(ts, "c-shutdown")
    _assert_live(ts, token)
    # Run the app lifespan; exiting it triggers shutdown → revoke_all.
    async with app.router.lifespan_context(app):
        pass
    _assert_revoked(ts, token)
