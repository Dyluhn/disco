"""PR C2 — the DiscoInferenceGateway endpoint.

``POST /internal/pi-kernel/v1/chat/completions`` is a local, run-scoped,
OpenAI-compatible endpoint that lets the Pi sidecar drive the **UI-selected** model
without ever seeing a provider API key (campaign §4.2 / §11.1). It is a RAW
pass-through to the provider for Pi's OWN messages:

* **Bearer-gated.** The request must carry a live, unrevoked, unexpired gateway
  token (``pi_inference.PiInferenceTokenStore``). Invalid/expired/revoked → 401.
* **Loopback only.** Served to local peers (loopback / unix socket) only — a remote
  peer is rejected (the token is a local capability, never a network credential).
* **Model is pinned by the TOKEN, never the request.** The endpoint resolves the
  model from ``token.model_key`` and OVERWRITES any ``model`` Pi sends. A
  model-switch attempt is ignored-and-pinned — Pi cannot change which model answers.
* **Keys stay orchestrator-side.** The provider key is resolved here (SecretStore,
  decrypted in-process) and injected ONLY into the OUTBOUND request to the provider.
  It is never returned to Pi, never echoed into the response, never logged. Pi's own
  ``Authorization`` (the gateway token) is NOT forwarded upstream.
* **No Disco prompt / tool injection.** The request is relayed verbatim (only the
  ``model`` field is overridden) — the gateway does NOT prepend Disco's driver system
  prompt or Disco tool schemas. Pi owns its own loop / prompt / tools.
* **Budgeted + observed.** Over-budget → 429. Usage (model id, provider, tokens) is
  recorded against the token and traced by fingerprint — never the token, never the key.

The reuse points (campaign §4): the model catalogue + the selected model live in the
shared ``ConfigStore`` (``disco.core.llm``); the provider key comes from the
``SecretStore`` (decrypted orchestrator-side, the SAME store the build-loop driver
uses). The endpoint forwards over plain ``httpx`` rather than the ``DefaultLLMRouter``
ON PURPOSE: the router would inject the driver system prompt + tool surface, which a
raw Pi pass-through must not do.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from disco.core.llm import ConfigStore
from disco.core.llm.secrets import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_API_KEY_ENV_LEGACY,
    SecretStore,
)
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..pi_inference import InvalidGatewayToken, PiInferenceTokenStore

_LOG = logging.getLogger("disco.pi_inference")

# Loopback peers the endpoint serves. A unix-socket peer surfaces as
# ``request.client is None`` (no TCP peer) and is also treated as local.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})

# Upstream request timeout. Generous — a build turn can be a long generation.
_UPSTREAM_TIMEOUT_S = 600.0

# Only ever parse usage from the TAIL of a stream (the final SSE chunk carries
# ``usage`` when the client asked for stream_options.include_usage). Bounding the
# buffer keeps a huge generation from being re-accumulated in full just to read
# the trailing token counts.
_USAGE_TAIL_BYTES = 16_384


@dataclass(frozen=True)
class _UpstreamTarget:
    """Where a bound model's requests go, resolved orchestrator-side. ``api_key`` is
    the decrypted provider key (or None for a keyless local endpoint); it lives only
    in this process and is injected solely into the outbound provider request."""

    base_url: str
    model_id: str
    provider: str
    api_key: str | None


def _client_is_local(request: Request) -> bool:
    """True when the request's peer is loopback or a unix socket. The gateway is a
    LOCAL capability; a remote peer must never reach it even with a token."""
    client = request.client
    if client is None:  # unix socket / ASGI with no TCP peer
        return True
    host = (client.host or "").strip()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _bearer(request: Request) -> str | None:
    """Extract the bearer token from the Authorization header (case-insensitive
    scheme). Returns None when absent/malformed. The value is NEVER logged."""
    auth = request.headers.get("authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def _resolve_provider_key(api_key_env: str | None, secret_store: SecretStore) -> str | None:
    """The decrypted provider key for a model's ``api_key_env``, ORCHESTRATOR-SIDE —
    the same resolution order the runtime uses to overlay a key at build time:
    the encrypted SecretStore wins, with the reserved OpenRouter slot + the
    DISCO_→PMX_ legacy alias honored, then the live process env as a last resort.

    Returns None when no key is configured (a keyless local endpoint, §4.4)."""
    if not api_key_env:
        return None
    # (1) a secret stored directly under the env-var name.
    val = secret_store.get_secret(api_key_env)
    if val:
        return val
    # (2) the reserved "openrouter" slot maps to the OpenRouter env-var names.
    if api_key_env in (OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY):
        val = secret_store.get_secret("openrouter")
        if val:
            return val
    # (3) DISCO_<X> ciphertext may be stored under the legacy PMX_<X> name.
    if api_key_env.startswith("DISCO_"):
        val = secret_store.get_secret("PMX_" + api_key_env[len("DISCO_") :])
        if val:
            return val
    # (4) back-compat: an env-var-configured key (never persisted in plaintext by us).
    return os.environ.get(api_key_env) or None


def resolve_upstream(
    model_key: str, *, config_store: ConfigStore, secret_store: SecretStore
) -> _UpstreamTarget | None:
    """Resolve the SELECTED model (its catalogue key) to a concrete provider target,
    decrypting the key in-process. Returns None when the key is unknown or has no
    live chat endpoint (a non-chat catalogue entry, e.g. the NLI cross-encoder).

    This is the security boundary: the only place the provider key is read, and it
    flows ONLY into ``_UpstreamTarget.api_key`` (outbound auth) — never to Pi."""
    cfg = config_store.load()
    entry = cfg.models.get(model_key)
    if entry is None or not entry.base_url:
        return None
    return _UpstreamTarget(
        base_url=entry.base_url.rstrip("/"),
        model_id=entry.model_id,
        provider=entry.provider,
        api_key=_resolve_provider_key(entry.api_key_env, secret_store),
    )


def _outbound_headers(target: _UpstreamTarget) -> dict[str, str]:
    """Fresh headers for the upstream provider request. Pi's Authorization (the
    gateway token) is NEVER forwarded; the provider key — when there is one — is
    injected here and nowhere else."""
    headers = {"content-type": "application/json"}
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"
    return headers


def _extract_usage(obj: object) -> tuple[int, int] | None:
    """Pull (input_tokens, output_tokens) from an OpenAI-shaped ``usage`` object,
    or None when absent."""
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    return (
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
    )


def _usage_from_sse_tail(tail: str) -> tuple[int, int]:
    """Best-effort: scan the tail of a streamed SSE response for the last ``usage``
    object (emitted when the client requested stream_options.include_usage). Returns
    (0, 0) when no usage line is present."""
    found = (0, 0)
    for raw in tail.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]" or not payload:
            continue
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        u = _extract_usage(obj)
        if u is not None:
            found = u
    return found


def make_pi_inference_router(
    token_store: PiInferenceTokenStore,
    *,
    config_store: ConfigStore | None = None,
    secret_store: SecretStore | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> APIRouter:
    """Build the gateway router. ``token_store`` is the shared run-scoped token store.
    ``config_store`` / ``secret_store`` default to the real shared stores (constructed
    fresh per request so a settings/secret change is honored live, mirroring the rest
    of the agent-server). ``http_transport`` is a test seam for the UPSTREAM provider
    call (an ``httpx.MockTransport``); None = real network."""
    router = APIRouter()

    def _stores() -> tuple[ConfigStore, SecretStore]:
        cs = config_store if config_store is not None else ConfigStore()
        ss = secret_store if secret_store is not None else SecretStore()
        return cs, ss

    @router.post("/internal/pi-kernel/v1/chat/completions")
    async def pi_chat_completions(request: Request) -> Response:
        # 1) Loopback-only. A remote peer never reaches the model, token or not.
        if not _client_is_local(request):
            return JSONResponse(
                {"error": {"message": "gateway is loopback-only", "type": "forbidden"}},
                status_code=403,
            )

        # 2) Validate the bearer token (live, unrevoked, unexpired). NEVER log it.
        token = _bearer(request)
        try:
            rec = token_store.validate(token)
        except InvalidGatewayToken as exc:
            _LOG.info("pi-gateway rejected token: %s", exc.reason)  # reason only, never the token
            return JSONResponse(
                {"error": {"message": "invalid gateway token", "type": "unauthorized"}},
                status_code=401,
            )
        assert token is not None  # validate() raises on a falsy token; narrow for typing

        # 3) Budget gate — refuse to start a call once the cap is reached.
        if rec.is_over_budget():
            _LOG.info(
                "pi-gateway over budget token=%s used=%d budget=%d",
                rec.fingerprint, rec.used_tokens, rec.budget_tokens,
            )
            return JSONResponse(
                {"error": {"message": "token budget exhausted", "type": "budget_exceeded"}},
                status_code=429,
            )

        # 4) Parse Pi's OpenAI-compatible body.
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"message": "request body must be a JSON object", "type": "bad_request"}},
                status_code=400,
            )

        # 5) Resolve the BOUND model (token, not request) → provider target.
        config, secrets_store = _stores()
        target = resolve_upstream(
            rec.model_key, config_store=config, secret_store=secrets_store
        )
        if target is None:
            _LOG.warning(
                "pi-gateway bound model has no live endpoint token=%s model_key=%s",
                rec.fingerprint, rec.model_key,
            )
            return JSONResponse(
                {
                    "error": {
                        "message": "selected model has no live chat endpoint",
                        "type": "model_unavailable",
                    }
                },
                status_code=502,
            )

        # 6) PIN the model: ignore whatever Pi put in `model`, always use the bound
        #    model id. This is the model-switch defense (ignore-and-pin). The rest of
        #    the body — messages, tools, params — is relayed VERBATIM (no Disco
        #    system-prompt / tool-schema injection).
        body["model"] = target.model_id
        stream = bool(body.get("stream", False))
        payload = json.dumps(body).encode("utf-8")
        headers = _outbound_headers(target)
        url = f"{target.base_url}/chat/completions"

        _LOG.info(
            "pi-gateway -> token=%s model=%s provider=%s stream=%s",
            rec.fingerprint, target.model_id, target.provider, stream,
        )

        client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_S, transport=http_transport)

        if not stream:
            try:
                resp = await client.post(url, content=payload, headers=headers)
            except httpx.HTTPError as exc:
                await client.aclose()
                return JSONResponse(
                    {"error": {"message": f"upstream connection error: {exc}", "type": "upstream"}},
                    status_code=502,
                )
            data = resp.content
            status = resp.status_code
            media = resp.headers.get("content-type", "application/json")
            await client.aclose()
            # Record usage for accounting (never the token / key).
            try:
                u = _extract_usage(json.loads(data))
            except (json.JSONDecodeError, ValueError):
                u = None
            if u is not None:
                token_store.record_usage(token, input_tokens=u[0], output_tokens=u[1])
                _LOG.info(
                    "pi-gateway usage token=%s model=%s in=%d out=%d",
                    rec.fingerprint, target.model_id, u[0], u[1],
                )
            # Relay the provider's response verbatim. It cannot contain the key (we
            # never sent the key in the body, and the provider doesn't echo auth).
            return Response(content=data, status_code=status, media_type=media)

        # Streaming: open the upstream stream and relay bytes through unchanged,
        # observing the trailing usage for accounting without altering the stream.
        async def _proxy_stream() -> AsyncIterator[bytes]:
            tail = b""
            try:
                async with client.stream(
                    "POST", url, content=payload, headers=headers
                ) as resp:
                    if resp.status_code >= 400:
                        yield await resp.aread()
                        return
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            tail = (tail + chunk)[-_USAGE_TAIL_BYTES:]
                            yield chunk
            except httpx.HTTPError as exc:
                yield (
                    b'data: {"error": {"message": "upstream stream error: '
                    + str(exc).encode("utf-8", "replace")
                    + b'"}}\n\n'
                )
            finally:
                await client.aclose()
                in_tok, out_tok = _usage_from_sse_tail(tail.decode("utf-8", "replace"))
                if in_tok or out_tok:
                    token_store.record_usage(token, input_tokens=in_tok, output_tokens=out_tok)
                    _LOG.info(
                        "pi-gateway usage token=%s model=%s in=%d out=%d",
                        rec.fingerprint, target.model_id, in_tok, out_tok,
                    )

        return StreamingResponse(_proxy_stream(), media_type="text/event-stream")

    return router
