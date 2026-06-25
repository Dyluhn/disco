"""EPIC C — DiscoInferenceGateway (PR C1 token model + PR C2 endpoint).

Proves the security seam: Pi reaches the UI-selected model over a loopback,
OpenAI-compatible endpoint authenticated by an ephemeral, run-scoped token, and

  * the model is PINNED by the token (a model-switch in the body is ignored),
  * the provider key is resolved server-side (SecretStore) and is injected ONLY
    into the OUTBOUND provider request — it never reaches Pi's response,
  * the gateway is a THIN pass-through: it does NOT prepend Disco's driver system
    prompt or Disco tool schemas (Pi owns its own loop),
  * the token budget is enforced (over-budget → 429),
  * the endpoint is loopback-only (a remote peer is rejected even with a token),
  * the token lifecycle (issue / validate / expire / revoke) holds.

These tests run no containers and open no real sockets — the upstream provider is
an ``httpx.MockTransport`` and the endpoint is driven over ``ASGITransport``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.parse
from types import SimpleNamespace

import httpx
import pytest
from disco.agent_server.pi_inference import (
    _MAX_TTL_S,
    GatewayToken,
    InvalidGatewayToken,
    PiInferenceTokenStore,
    fingerprint,
)
from disco.agent_server.routes.pi_inference import (
    _client_is_local,
    _estimate_prompt_tokens,
    _key_byte_needles,
    _redact_bytes,
    _redact_stream,
    make_pi_inference_router,
    resolve_upstream,
)
from disco.core.llm.config import ModelEntry, RouterConfig
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secrets import SecretBox, SecretStore
from fastapi import FastAPI

# A fake clock the store reads via its injectable ``clock`` so expiry is exact.


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# PR C1 — the token model (pure; no network).
# ---------------------------------------------------------------------------


def test_issue_mints_256bit_token_bound_to_model() -> None:
    clock = _Clock()
    store = PiInferenceTokenStore(clock=clock)
    token = store.issue(
        kernel_id="k1",
        conversation_id="c1",
        model_key="selected-model",
        ttl_s=60,
        budget_tokens=500,
    )
    # 32 random bytes → a url-safe value of >= 43 chars (256 bits of entropy).
    assert isinstance(token, str)
    assert len(token) >= 43
    rec = store.validate(token)
    assert rec.kernel_id == "k1"
    assert rec.conversation_id == "c1"
    assert rec.model_key == "selected-model"
    assert rec.budget_tokens == 500
    assert rec.expires_at == clock.now + 60
    assert rec.fingerprint == fingerprint(token)
    # Two issues never collide.
    assert store.issue(
        kernel_id="k1", conversation_id="c1", model_key="m", ttl_s=60, budget_tokens=10
    ) != token


def test_token_value_is_not_stored_in_the_record() -> None:
    """The raw bearer value is the store's KEY, never a record field — so a record
    repr / log line cannot leak the secret."""
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key="m", ttl_s=60, budget_tokens=10
    )
    rec = store.validate(token)
    assert token not in repr(rec)
    for value in vars(rec).values():
        assert value != token


def test_validate_unknown_revoked_expired() -> None:
    clock = _Clock()
    store = PiInferenceTokenStore(clock=clock)
    # unknown
    with pytest.raises(InvalidGatewayToken) as ei:
        store.validate("nope")
    assert ei.value.reason == "unknown"
    with pytest.raises(InvalidGatewayToken):
        store.validate(None)
    # revoked
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key="m", ttl_s=60, budget_tokens=10
    )
    assert store.revoke(token) is True
    with pytest.raises(InvalidGatewayToken) as er:
        store.validate(token)
    assert er.value.reason == "revoked"
    # expired
    fresh = store.issue(
        kernel_id="k", conversation_id="c", model_key="m", ttl_s=10, budget_tokens=10
    )
    assert store.validate(fresh).model_key == "m"
    clock.now += 11  # past TTL
    with pytest.raises(InvalidGatewayToken) as ee:
        store.validate(fresh)
    assert ee.value.reason == "expired"


def test_revoke_conversation_and_kernel() -> None:
    store = PiInferenceTokenStore()
    a = store.issue(
        kernel_id="k1", conversation_id="c1", model_key="m", ttl_s=60, budget_tokens=10
    )
    b = store.issue(
        kernel_id="k2", conversation_id="c1", model_key="m", ttl_s=60, budget_tokens=10
    )
    c = store.issue(
        kernel_id="k1", conversation_id="c2", model_key="m", ttl_s=60, budget_tokens=10
    )
    assert store.revoke_conversation("c1") == 2
    with pytest.raises(InvalidGatewayToken):
        store.validate(a)
    with pytest.raises(InvalidGatewayToken):
        store.validate(b)
    assert store.validate(c).kernel_id == "k1"  # other conversation untouched
    assert store.revoke_kernel("k1") == 1  # c still live, a already revoked
    with pytest.raises(InvalidGatewayToken):
        store.validate(c)


def test_budget_threshold() -> None:
    rec = GatewayToken(
        kernel_id="k",
        conversation_id="c",
        model_key="m",
        expires_at=9e9,
        budget_tokens=100,
    )
    assert rec.is_over_budget() is False
    assert rec.budget_remaining() == 100
    rec.used_tokens = 99
    assert rec.is_over_budget() is False
    rec.used_tokens = 100  # at the cap → rejected
    assert rec.is_over_budget() is True
    assert rec.budget_remaining() == 0
    # No cap configured → never over budget.
    uncapped = GatewayToken(
        kernel_id="k", conversation_id="c", model_key="m", expires_at=9e9
    )
    uncapped.used_tokens = 10_000
    assert uncapped.is_over_budget() is False
    assert uncapped.budget_remaining() is None


def test_record_usage_accrues() -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key="m", ttl_s=60, budget_tokens=100
    )
    store.record_usage(token, input_tokens=30, output_tokens=20)
    assert store.validate(token).used_tokens == 50
    store.record_usage("unknown", input_tokens=1, output_tokens=1)  # harmless no-op


# ---------------------------------------------------------------------------
# PR C2 — the gateway endpoint. Shared harness below.
# ---------------------------------------------------------------------------

_SELECTED_KEY = "selected-model"
_REAL_MODEL_ID = "vendor/real-model-7b"
_PROVIDER_KEY_ENV = "PI_TEST_PROVIDER_KEY"
_SECRET_VALUE = "sk-super-secret-provider-key-DO-NOT-LEAK"
_BASE_URL = "http://upstream.test/v1"


def _config_store(tmp_path) -> ConfigStore:
    cfg = RouterConfig(
        models={
            _SELECTED_KEY: ModelEntry(
                model_id=_REAL_MODEL_ID,
                provider="testprov",
                context_window=8192,
                base_url=_BASE_URL,
                api_key_env=_PROVIDER_KEY_ENV,
            ),
            # a model the body might try to switch TO — proves the switch is ignored.
            "other-model": ModelEntry(
                model_id="vendor/other-99b",
                provider="testprov",
                context_window=8192,
                base_url=_BASE_URL,
                api_key_env=_PROVIDER_KEY_ENV,
            ),
        },
        default_model=_SELECTED_KEY,
    )
    # nonexistent path → load() returns the base_factory config.
    return ConfigStore(path=tmp_path / "no-such-config.json", base_factory=lambda: cfg)


def _secret_store(tmp_path) -> SecretStore:
    box = SecretBox(app_secret="unit-test-app-secret")
    store = SecretStore(path=tmp_path / "secrets.json", box=box)
    store.set_secret(_PROVIDER_KEY_ENV, _SECRET_VALUE)  # encrypted at rest
    return store


def _make_app(token_store: PiInferenceTokenStore, tmp_path, *, handler, trust_local_no_peer=False):
    """A FastAPI app mounting only the gateway router, wired to a mock upstream."""
    app = FastAPI()
    app.include_router(
        make_pi_inference_router(
            token_store,
            config_store=_config_store(tmp_path),
            secret_store=_secret_store(tmp_path),
            http_transport=httpx.MockTransport(handler),
            trust_local_no_peer=trust_local_no_peer,
        )
    )
    return app


def _capturing_handler(captured: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-x",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"}}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    return handler


async def _post(app, token: str | None, body: dict, *, client=("127.0.0.1", 5555)):
    transport = httpx.ASGITransport(app=app, client=client)
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as c:
        return await c.post(
            "/internal/pi-kernel/v1/chat/completions", json=body, headers=headers
        )


# -- resolve_upstream: the key comes from the SecretStore, server-side ------


def test_resolve_upstream_reads_key_from_secret_store(tmp_path) -> None:
    target = resolve_upstream(
        _SELECTED_KEY,
        config_store=_config_store(tmp_path),
        secret_store=_secret_store(tmp_path),
    )
    assert target is not None
    assert target.model_id == _REAL_MODEL_ID
    assert target.base_url == _BASE_URL
    assert target.api_key == _SECRET_VALUE  # decrypted in-process, orchestrator-side


# -- loopback-only ----------------------------------------------------------


async def test_remote_peer_rejected_even_with_valid_token(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app,
        token,
        {"messages": [{"role": "user", "content": "hi"}]},
        client=("8.8.8.8", 443),  # a remote peer
    )
    assert resp.status_code == 403
    assert captured == []  # never reached the provider


# -- auth -------------------------------------------------------------------


async def test_missing_and_invalid_token_401(tmp_path) -> None:
    store = PiInferenceTokenStore()
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert (await _post(app, None, body)).status_code == 401
    assert (await _post(app, "bogus-token", body)).status_code == 401
    # a revoked token is also 401
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    store.revoke(token)
    assert (await _post(app, token, body)).status_code == 401
    assert captured == []  # nothing reached the provider


# -- model is pinned by the token, never the request ------------------------


async def test_model_switch_attempt_is_ignored_and_pinned(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    # Pi tries to switch to a different, more expensive model.
    resp = await _post(
        app,
        token,
        {
            "model": "vendor/other-99b",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    assert len(captured) == 1
    sent = json.loads(captured[0].content)
    # The token's bound model won — Pi's `model` was overwritten.
    assert sent["model"] == _REAL_MODEL_ID
    assert sent["model"] != "vendor/other-99b"


# -- key isolation: never echoed to Pi, never in the response ---------------


async def test_provider_key_never_reaches_pi(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 200
    # 1) The key DID reach the provider (server-side outbound auth).
    assert captured[0].headers.get("authorization") == f"Bearer {_SECRET_VALUE}"
    # 2) Pi's own bearer (the gateway token) was NOT forwarded upstream — the
    #    outbound auth is the PROVIDER key, not Pi's token.
    assert captured[0].headers.get("authorization") != f"Bearer {token}"
    assert token not in json.dumps(dict(captured[0].headers))
    # 3) The key is NOWHERE in the response handed back to Pi.
    assert _SECRET_VALUE not in resp.text
    assert _SECRET_VALUE not in json.dumps(dict(resp.headers))


# -- no Disco prompt / tool-schema double-injection -------------------------


async def test_no_disco_prompt_or_tool_injection(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    pi_messages = [
        {"role": "system", "content": "PI-OWN-SYSTEM-PROMPT"},
        {"role": "user", "content": "build me a thing"},
    ]
    pi_tools = [{"type": "function", "function": {"name": "pi_tool"}}]
    resp = await _post(
        app,
        token,
        {"messages": pi_messages, "tools": pi_tools, "temperature": 0.3},
    )
    assert resp.status_code == 200
    sent = json.loads(captured[0].content)
    # Messages relayed VERBATIM — only `model` was overridden. No Disco system
    # prompt prepended, no extra messages injected.
    assert sent["messages"] == pi_messages
    # Pi's own tools survive untouched; Disco did not append its tool surface.
    assert sent["tools"] == pi_tools
    # Other params relayed verbatim.
    assert sent["temperature"] == 0.3


# -- budget enforcement -----------------------------------------------------


async def test_over_budget_rejected_429_before_upstream(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k",
        conversation_id="c",
        model_key=_SELECTED_KEY,
        ttl_s=60,
        budget_tokens=40,
    )
    store.record_usage(token, input_tokens=30, output_tokens=10)  # hits the cap
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 429
    assert captured == []  # the call never started


async def test_usage_accrued_from_response(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k",
        conversation_id="c",
        model_key=_SELECTED_KEY,
        ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 200
    # 11 prompt + 7 completion = 18 accrued against the budget.
    assert store.validate(token).used_tokens == 18


# -- streaming passthrough ---------------------------------------------------


async def test_streaming_passthrough_and_usage(tmp_path) -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k",
        conversation_id="c",
        model_key=_SELECTED_KEY,
        ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app,
        token,
        {"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    assert resp.content == sse  # relayed byte-for-byte
    assert _SECRET_VALUE not in resp.text
    assert store.validate(token).used_tokens == 8  # 5 + 3 from the trailing usage


# ===========================================================================
# Codex BLOCK fixes — regression coverage.
# ===========================================================================

# -- P2: issue() rejects/clamps unsafe TTL + budget -------------------------


def test_issue_rejects_unsafe_ttl_and_budget() -> None:
    store = PiInferenceTokenStore()
    base = dict(kernel_id="k", conversation_id="c", model_key="m")
    for bad_ttl in (0, -5, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            store.issue(**base, ttl_s=bad_ttl, budget_tokens=100)
    for bad_budget in (0, -1):
        with pytest.raises(ValueError):
            store.issue(**base, ttl_s=60, budget_tokens=bad_budget)


def test_issue_clamps_oversized_ttl() -> None:
    clock = _Clock()
    store = PiInferenceTokenStore(clock=clock)
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key="m",
        ttl_s=10**9, budget_tokens=100,  # absurd TTL
    )
    rec = store.validate(token)
    assert rec.expires_at == clock.now + _MAX_TTL_S  # clamped to the ceiling


# -- P1: loopback check fails closed on a missing peer ----------------------


def _req(host):
    client = None if host is None else SimpleNamespace(host=host, port=1)
    return SimpleNamespace(client=client)


def test_loopback_no_peer_fails_closed_by_default() -> None:
    assert _client_is_local(_req(None), trust_local_no_peer=False) is False
    # only when a deployment explicitly opts into a trusted unix socket:
    assert _client_is_local(_req(None), trust_local_no_peer=True) is True


def test_loopback_uses_ip_address_not_string_prefix() -> None:
    assert _client_is_local(_req("127.0.0.1"), trust_local_no_peer=False) is True
    assert _client_is_local(_req("::1"), trust_local_no_peer=False) is True
    assert _client_is_local(_req("127.5.9.9"), trust_local_no_peer=False) is True
    assert _client_is_local(_req("::ffff:127.0.0.1"), trust_local_no_peer=False) is True
    # a remote host that merely starts with "127." textually is NOT loopback:
    assert _client_is_local(_req("127.0.0.1.evil.example"), trust_local_no_peer=False) is False
    assert _client_is_local(_req("8.8.8.8"), trust_local_no_peer=False) is False


async def test_no_peer_request_is_403_by_default(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}]}, client=None
    )
    assert resp.status_code == 403  # peerless rejected even with a valid token
    assert captured == []


async def test_no_peer_request_allowed_when_opted_in(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(
        store, tmp_path, handler=_capturing_handler(captured), trust_local_no_peer=True
    )
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}]}, client=None
    )
    assert resp.status_code == 200
    assert len(captured) == 1


# -- P1: provider routing / fallback fields are stripped --------------------


async def test_provider_routing_fields_are_stripped(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app,
        token,
        {
            "messages": [{"role": "user", "content": "hi"}],
            "model": "vendor/other-99b",
            "models": ["vendor/other-99b", "vendor/expensive"],
            "provider": {"order": ["sneaky"], "allow_fallbacks": True},
            "route": "fallback",
            "transforms": ["middle-out"],
            "made_up_field": "x",
        },
    )
    assert resp.status_code == 200
    sent = json.loads(captured[0].content)
    for stripped in ("models", "provider", "route", "transforms", "made_up_field"):
        assert stripped not in sent
    # the pinned model is the ONLY routing signal that leaves the process.
    assert sent["model"] == _REAL_MODEL_ID
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


# -- P1: budget reservation can't be bypassed -------------------------------


async def test_streaming_without_include_usage_still_accrues(tmp_path) -> None:
    # A provider that ignores stream_options.include_usage and emits NO usage tail.
    sse = (
        b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # The gateway must have forced include_usage on the outbound request.
        body = json.loads(request.content)
        assert body["stream_options"]["include_usage"] is True
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert resp.status_code == 200
    # Even with no usage tail, the up-front prompt reservation stands.
    assert store.validate(token).used_tokens > 0


async def test_oversized_request_clamped_to_remaining_budget(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=500,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app,
        token,
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1_000_000},
    )
    assert resp.status_code == 200
    sent = json.loads(captured[0].content)
    assert 0 < sent["max_tokens"] <= 500  # clamped down to what the budget allows
    assert sent["max_tokens"] < 1_000_000


async def test_oversized_max_tokens_clamped_to_provider_ceiling(tmp_path) -> None:
    # #108: with a LARGE budget the budget clamp won't bound an absurd SDK-default
    # max_tokens — but several providers 400 over ~524288 (MiniMax-M3). The gateway must
    # cap to the provider-portable ceiling (131072) regardless of budget. (Previously this
    # was patched only in the dev MiniMax relay; now it lives in the gateway.)
    from disco.agent_server.routes.pi_inference import _MAX_TOKENS_CEILING

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=8_000_000,  # large enough that the budget clamp won't bound it
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    # (a) Pi sends an oversized max_tokens
    resp = await _post(
        app, token,
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 4_000_000},
    )
    assert resp.status_code == 200
    assert json.loads(captured[0].content)["max_tokens"] <= _MAX_TOKENS_CEILING
    # (b) Pi sends NO max_tokens — the injected cap must also respect the ceiling
    resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert json.loads(captured[1].content)["max_tokens"] <= _MAX_TOKENS_CEILING


async def test_concurrent_calls_cannot_both_pass_cap(tmp_path) -> None:
    store = PiInferenceTokenStore()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    # Budget sized so exactly ONE prompt estimate fits with a little headroom but two
    # do not (the stricter ``used + estimate > budget`` gate rejects the second).
    est = _estimate_prompt_tokens(body)
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=est + 5,  # one reservation fits (with room to generate), two don't
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    r1, r2 = await asyncio.gather(
        _post(app, token, body), _post(app, token, body)
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 429]  # exactly one passed the gate
    assert len(captured) == 1  # only one call reached the provider


# -- P0: provider key never leaks back to Pi (echo / error / stream) --------


async def test_key_redacted_when_upstream_echoes_it(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile/buggy upstream that echoes the request auth header + key.
        echoed = request.headers.get("authorization")
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "echoed_auth": echoed,  # "Bearer <provider-key>"
                    "note": f"your key is {_SECRET_VALUE}",
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode("utf-8"),
            headers={"content-type": f"application/json; leaked={_SECRET_VALUE}"},
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    # The provider key is absent from EVERYTHING Pi receives.
    assert _SECRET_VALUE not in resp.text
    assert f"Bearer {_SECRET_VALUE}" not in resp.text
    assert _SECRET_VALUE not in json.dumps(dict(resp.headers))


async def test_key_redacted_in_stream(tmp_path) -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"leak='
        + _SECRET_VALUE.encode("utf-8")
        + b'"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert resp.status_code == 200
    assert _SECRET_VALUE not in resp.text  # redacted from the SSE chunk


async def test_upstream_error_is_generic_and_redacted(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom while using key {_SECRET_VALUE}")

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 502
    assert _SECRET_VALUE not in resp.text  # exception string never reaches Pi
    assert resp.json()["error"]["message"] == "upstream request failed"  # generic


async def test_stream_upstream_error_is_generic_and_redacted(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom while using key {_SECRET_VALUE}")

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert _SECRET_VALUE not in resp.text
    assert "upstream request failed" in resp.text  # generic stream error


# ===========================================================================
# Codex round-2 BLOCK fixes — regression coverage.
# ===========================================================================

# -- P0: key redaction across SSE chunk boundaries + encoded forms ----------


async def test_redact_stream_catches_key_split_across_chunks() -> None:
    """A key split between two SSE chunks (``sk-ab`` | ``cdef``) reassembles intact
    under per-chunk redaction; the cross-boundary carry-over catches it."""
    key = _SECRET_VALUE
    mid = len(key) // 2

    async def chunks():
        # The key straddles the boundary between chunk 1 and chunk 2.
        yield b'data: {"choices":[{"delta":{"content":"leak=' + key[:mid].encode("utf-8")
        yield key[mid:].encode("utf-8") + b'"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    out = b""
    async for piece in _redact_stream(chunks(), key):
        out += piece
    assert key.encode("utf-8") not in out  # the split key is fully redacted
    assert b"[REDACTED]" in out
    assert b"data: [DONE]" in out  # ordinary streaming content untouched
    assert b'"content":"leak=[REDACTED]"' in out


async def test_redact_stream_passthrough_without_key() -> None:
    """With no provider key the stream is byte-for-byte transparent."""
    payload = b'data: {"choices":[{"delta":{"content":"hello world"}}]}\n\n'

    async def chunks():
        yield payload[:20]
        yield payload[20:]

    out = b""
    async for piece in _redact_stream(chunks(), None):
        out += piece
    assert out == payload


def test_redact_bytes_handles_encoded_key_forms() -> None:
    """A provider may echo request data URL- or base64-encoded; those forms are
    redacted too (a key with special chars makes the encodings differ from the raw)."""
    key = "sk-a/b+c d=secret"  # / + space = → percent/base64 forms differ from raw
    pct = urllib.parse.quote(key, safe="").encode("utf-8")
    b64 = base64.b64encode(key.encode("utf-8"))
    assert pct != key.encode("utf-8")  # the encoded form really is different
    needles = _key_byte_needles(key)
    assert pct in needles
    assert b64 in needles
    red_pct = _redact_bytes(b"prefix " + pct + b" suffix", key)
    assert pct not in red_pct
    assert b"[REDACTED]" in red_pct
    red_b64 = _redact_bytes(b"prefix " + b64 + b" suffix", key)
    assert b64 not in red_b64
    assert b"[REDACTED]" in red_b64


async def test_key_redacted_across_stream_chunks_end_to_end(tmp_path) -> None:
    """End-to-end: an upstream that delivers the key split across two streamed chunks
    is redacted before Pi sees the reassembled stream."""
    key = _SECRET_VALUE
    mid = len(key) // 2

    async def upstream_body():
        yield b'data: {"choices":[{"delta":{"content":"k=' + key[:mid].encode("utf-8")
        yield key[mid:].encode("utf-8") + b'"}}]}\n\n'
        yield b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=upstream_body(), headers={"content-type": "text/event-stream"}
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert resp.status_code == 200
    assert _SECRET_VALUE not in resp.text  # split key never reassembles on Pi's side
    assert "[REDACTED]" in resp.text


# -- P1: budget pre-check rejects an oversized request before the provider ---


async def test_oversized_first_request_rejected_pre_call(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=3,  # smaller than any real prompt estimate
    )
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _estimate_prompt_tokens(body) > 3  # the estimate already exceeds the budget
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(app, token, body)
    assert resp.status_code == 429
    assert captured == []  # rejected before the provider was ever called
    assert store.validate(token).used_tokens == 0  # nothing reserved


async def test_zero_remaining_rejected_not_one_token_allowed(tmp_path) -> None:
    store = PiInferenceTokenStore()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    est = _estimate_prompt_tokens(body)
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=est,  # exactly enough for the prompt, no room to generate
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(app, token, body)
    assert resp.status_code == 429  # remaining==0 → rejected, not a 1-token call
    assert captured == []


async def test_max_completion_tokens_clamped_to_remaining_budget(tmp_path) -> None:
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=500,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app,
        token,
        {
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 1_000_000,
        },
    )
    assert resp.status_code == 200
    sent = json.loads(captured[0].content)
    # max_completion_tokens is whitelisted → it must be clamped too, not bypass the cap.
    assert 0 < sent["max_completion_tokens"] <= 500
    assert sent["max_completion_tokens"] < 1_000_000


# -- P1: upstream error BODIES (4xx/5xx) are not relayed to Pi ---------------


async def test_upstream_error_body_not_relayed_non_stream(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A provider error body that echoes request data + the injected key.
        return httpx.Response(
            500,
            content=json.dumps(
                {
                    "error": "bad things",
                    "echoed_auth": request.headers.get("authorization"),
                    "leaked": _SECRET_VALUE,
                }
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 500  # status code passes through
    assert _SECRET_VALUE not in resp.text  # but the body does NOT
    assert "bad things" not in resp.text  # the provider body is replaced wholesale
    assert resp.json()["error"]["message"] == "upstream request failed"  # generic


async def test_upstream_error_body_not_relayed_stream(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            content=json.dumps(
                {"error": "upstream boom", "leaked": _SECRET_VALUE}
            ).encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert _SECRET_VALUE not in resp.text  # error body never relayed
    assert "upstream boom" not in resp.text
    assert "upstream request failed" in resp.text  # generic message instead


# ===========================================================================
# Codex round-3 residual fixes — regression coverage.
# ===========================================================================

# -- P0: the ENCODED full ``Bearer <key>`` Authorization-header form is redacted --


def test_encoded_bearer_header_form_is_in_needle_set() -> None:
    """base64 is NOT substring-preserving, so the encoded WHOLE ``Bearer <key>``
    header is a distinct needle the bare-key encodings cannot cover — it must be
    present in its own right."""
    bearer_b64 = base64.b64encode(f"Bearer {_SECRET_VALUE}".encode()).decode("ascii")
    bare_b64 = base64.b64encode(_SECRET_VALUE.encode()).decode("ascii")
    assert bearer_b64 != bare_b64
    assert bearer_b64.encode("utf-8") not in bare_b64.encode("utf-8")  # not nested
    needles = _key_byte_needles(_SECRET_VALUE)
    assert bearer_b64.encode("utf-8") in needles  # the encoded header form is covered
    assert bare_b64.encode("utf-8") in needles  # (and the bare-key form still is)


async def test_encoded_bearer_header_echo_redacted_success(tmp_path) -> None:
    """A provider that echoes the base64 of the whole Authorization header must not
    leak it back to Pi in a 200 body."""
    bearer_b64 = base64.b64encode(f"Bearer {_SECRET_VALUE}".encode()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "echoed_b64_auth": bearer_b64,  # base64("Bearer <key>")
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert bearer_b64 not in resp.text  # encoded header form redacted
    assert _SECRET_VALUE not in resp.text


async def test_encoded_bearer_header_echo_redacted_stream(tmp_path) -> None:
    """The encoded whole-header form is also redacted out of a streamed body."""
    bearer_b64 = base64.b64encode(f"Bearer {_SECRET_VALUE}".encode()).decode("ascii")
    sse = (
        b'data: {"choices":[{"delta":{"content":"' + bearer_b64.encode("utf-8") + b'"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert resp.status_code == 200
    assert bearer_b64 not in resp.text  # encoded header form redacted from the stream
    assert _SECRET_VALUE not in resp.text


# -- P1: an ENCODED key in an upstream error body is redacted in OUR logs ----


async def test_encoded_key_in_error_body_redacted_in_log(tmp_path, caplog) -> None:
    """The server-side error log uses the SAME encoded-aware redactor as egress, so a
    base64-encoded key in an upstream error body does not get written to our logs."""
    key_b64 = base64.b64encode(_SECRET_VALUE.encode()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            content=json.dumps({"error": "boom", "leaked_b64": key_b64}).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    with caplog.at_level(logging.WARNING, logger="disco.pi_inference"):
        resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 500
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "status=500" in logged  # the error WAS logged...
    assert key_b64 not in logged  # ...but the encoded key was redacted out of the log
    assert _SECRET_VALUE not in logged


# -- P2: a remaining<=0 pre-upstream reject REFUNDS its reservation ----------


async def test_remaining_zero_reject_refunds_reservation(tmp_path) -> None:
    """When ``reserve`` succeeds but leaves remaining==0, the request is rejected
    BEFORE upstream — and the reservation is refunded, so the surviving budget is not
    burned by a call that never ran."""
    store = PiInferenceTokenStore()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    est = _estimate_prompt_tokens(body)
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=est,  # exactly the prompt estimate → remaining==0 after reserve
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(app, token, body)
    assert resp.status_code == 429
    assert captured == []  # never reached upstream
    # The reservation was refunded — the budget is intact, not drained by the reject.
    assert store.validate(token).used_tokens == 0


# ===========================================================================
# Completeness sweep — NEW findings (2 P1 + 4 P2).
# ===========================================================================

# -- P1 #1: the reservation covers the COMPLETION budget, not just the prompt --


async def test_completion_budget_reserved_concurrent_cannot_overspend(tmp_path) -> None:
    """Two concurrent calls whose PROMPT estimates both fit (prompt-only reservation
    would let both through) must still not both pass: the first call also reserves an
    OUTPUT cap, so the second sees the completion budget consumed and is rejected. The
    completion budget cannot be double-spent."""
    store = PiInferenceTokenStore()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    est = _estimate_prompt_tokens(body)
    # Headroom so TWO prompt-only reservations (2*est) would fit — proving the gate is
    # the OUTPUT reservation, not the prompt estimate.
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=est + 50,
    )
    assert 2 * est <= est + 50  # prompt-only reservations would NOT block the second
    captured: list[httpx.Request] = []

    # An async handler that holds the FIRST upstream call in flight, so the second
    # call reserves while the first's full reservation (prompt + output cap) is still
    # held — proving the completion budget cannot be double-spent (with an instant
    # handler the first would settle before the second reserved).
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    app = _make_app(store, tmp_path, handler=handler)
    r1, r2 = await asyncio.gather(_post(app, token, body), _post(app, token, body))
    assert sorted([r1.status_code, r2.status_code]) == [200, 429]
    assert len(captured) == 1  # the completion reservation blocked the second call


async def test_n_greater_than_one_forced_to_single_completion(tmp_path) -> None:
    """``n`` is forwarded but unaccounted; ``n>1`` would multiply real spend past the
    single reservation. The gateway forces ``n=1`` on the outbound request."""
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "n": 5}
    )
    assert resp.status_code == 200
    assert json.loads(captured[0].content)["n"] == 1  # forced to a single completion


async def test_provider_omitting_usage_charges_full_reservation(tmp_path) -> None:
    """A provider that returns NO usage is charged the full reservation (prompt
    estimate + reserved output cap), not just the prompt — so an unmetered call still
    draws down the budget by what it could have generated."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}]}  # NO usage field
        )

    store = PiInferenceTokenStore()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    est = _estimate_prompt_tokens(body)
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(app, token, body)
    assert resp.status_code == 200
    # The whole budget was reserved (est + output cap == budget) and stands as the
    # charge — far more than the prompt estimate alone.
    used = store.validate(token).used_tokens
    assert used == 1000
    assert used > est


# -- P1 #2: encoded redaction covers percent case-variants + JSON slash-escape --


def test_lowercase_percent_key_form_is_redacted() -> None:
    """Percent escapes are case-insensitive — a provider reflecting the request with
    LOWERCASE percent hex (``%2f``) must be redacted as well as the canonical
    uppercase (``%2F``) ``urllib.parse.quote`` emits."""
    key = "sk-a/b c"  # '/' and ' ' percent-encode to %2F and %20
    upper = urllib.parse.quote(key, safe="").encode("utf-8")  # sk-a%2Fb%20c
    lower = upper.lower()  # NOTE: only the hex differs here (no other letters)
    assert b"%2f" in lower and b"%2F" in upper  # the two spellings really differ
    needles = _key_byte_needles(key)
    assert lower in needles  # the lowercase percent spelling is covered
    red = _redact_bytes(b"prefix " + lower + b" suffix", key)
    assert lower not in red
    assert b"[REDACTED]" in red


def test_json_slash_escaped_key_form_is_redacted() -> None:
    """A JSON encoder that escapes ``/`` as ``\\/`` produces a form ``json.dumps``
    does not — it must still be redacted (a key with a ``/`` reflected JSON-escaped)."""
    key = "sk-a/b/c"  # contains slashes
    slash_escaped = key.replace("/", "\\/").encode("utf-8")  # sk-a\/b\/c
    assert slash_escaped != key.encode("utf-8")
    needles = _key_byte_needles(key)
    assert slash_escaped in needles
    red = _redact_bytes(b'{"k":"' + slash_escaped + b'"}', key)
    assert slash_escaped not in red
    assert b"[REDACTED]" in red


# -- P2 #3: bounded request + upstream body buffering -----------------------


async def test_oversized_request_body_rejected_413(tmp_path, monkeypatch) -> None:
    """A request body larger than the limit is rejected with 413 before it is buffered
    or parsed."""
    monkeypatch.setattr(
        "disco.agent_server.routes.pi_inference._MAX_REQUEST_BYTES", 256
    )
    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    captured: list[httpx.Request] = []
    app = _make_app(store, tmp_path, handler=_capturing_handler(captured))
    big = {"messages": [{"role": "user", "content": "x" * 5000}]}  # well over 256 bytes
    resp = await _post(app, token, big)
    assert resp.status_code == 413
    assert captured == []  # never reached the provider


async def test_oversized_upstream_body_is_capped(tmp_path, monkeypatch) -> None:
    """An oversized upstream (non-stream) body is truncated to the cap + a note, not
    buffered in full."""
    monkeypatch.setattr(
        "disco.agent_server.routes.pi_inference._MAX_UPSTREAM_BYTES", 128
    )
    huge = json.dumps({"filler": "Z" * 100_000}).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=huge, headers={"content-type": "application/json"})

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert b"...[truncated]" in resp.content  # capped + noted
    assert len(resp.content) < len(huge)  # not the full body
    assert len(resp.content) <= 128 + len(b"\n...[truncated]")


# -- P2 #4: streaming has a wall-clock + total-byte cap ---------------------


async def test_stream_exceeding_byte_cap_is_terminated(tmp_path, monkeypatch) -> None:
    """A stream whose total bytes exceed the ceiling is cut off rather than relayed in
    full — the gateway stops pulling and closes the upstream."""
    monkeypatch.setattr(
        "disco.agent_server.routes.pi_inference._STREAM_MAX_BYTES", 200
    )
    chunk = b'data: {"choices":[{"delta":{"content":"' + b"y" * 100 + b'"}}]}\n\n'
    full = chunk * 20  # ~3 KB, well over the 200-byte cap

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=full, headers={"content-type": "text/event-stream"})

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=100_000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert resp.status_code == 200
    assert len(resp.content) < len(full)  # terminated early, not the whole stream


# -- P2 #5: a stream upstream error is a NON-200 to Pi (not a 200 SSE) -------


async def test_stream_upstream_error_status_yields_non_200(tmp_path) -> None:
    """A provider 401/500 on the STREAM path must surface as a non-200 to Pi
    (consistent with the non-stream path), not a 200 SSE carrying an error body."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=json.dumps({"error": "nope", "leaked": _SECRET_VALUE}).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(
        app, token, {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert resp.status_code == 401  # upstream status passed through, NOT a 200 SSE
    assert _SECRET_VALUE not in resp.text  # the error body is never relayed
    assert "nope" not in resp.text
    assert resp.json()["error"]["message"] == "upstream request failed"  # generic


# -- P2 #6: malformed usage is treated as absent, never crashes -------------


def test_extract_usage_malformed_fields_treated_as_absent() -> None:
    """A non-finite / non-numeric / wrong-typed usage field is treated as absent
    (None) rather than raising TypeError/OverflowError/ValueError."""
    from disco.agent_server.routes.pi_inference import _extract_usage

    inf = float("inf")
    assert _extract_usage({"usage": {"prompt_tokens": inf, "completion_tokens": 1}}) is None
    assert _extract_usage({"usage": {"prompt_tokens": [1, 2], "completion_tokens": 1}}) is None
    assert _extract_usage({"usage": {"prompt_tokens": "abc", "completion_tokens": 1}}) is None
    assert _extract_usage({"usage": {"prompt_tokens": -5, "completion_tokens": 1}}) is None
    # a well-formed usage still parses.
    assert _extract_usage({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}) == (3, 4)


async def test_malformed_usage_does_not_crash_request(tmp_path) -> None:
    """A provider returning a malformed ``usage`` field does not crash the request; the
    usage is treated as absent and the reservation stands as the charge."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": [99], "completion_tokens": "oops"},  # malformed
            },
        )

    store = PiInferenceTokenStore()
    token = store.issue(
        kernel_id="k", conversation_id="c", model_key=_SELECTED_KEY, ttl_s=60,
        budget_tokens=1000,
    )
    app = _make_app(store, tmp_path, handler=handler)
    resp = await _post(app, token, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200  # no crash
    assert store.validate(token).used_tokens == 1000  # reservation stands (usage absent)
