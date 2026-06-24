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

import base64
import ipaddress
import json
import logging
import os
import urllib.parse
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

# Upstream request timeout. Generous — a build turn can be a long generation.
_UPSTREAM_TIMEOUT_S = 600.0

# What Pi receives instead of any provider-key bytes / raw upstream error string.
_REDACTED = "[REDACTED]"
_GENERIC_UPSTREAM_ERROR = "upstream request failed"

# Portable OpenAI chat-completion fields we relay to the provider. Anything NOT in
# this allow-list is dropped before the upstream call — in particular the provider
# routing aliases below, which OpenRouter et al. honor to override the pinned model
# (``models`` fallback list, ``provider`` routing, ``route``, ``transforms``). The
# pinned model id is the ONLY routing signal that leaves this process.
_PORTABLE_CHAT_FIELDS = frozenset(
    {
        "messages", "model", "temperature", "top_p", "n", "stream", "stream_options",
        "stop", "max_tokens", "max_completion_tokens", "presence_penalty",
        "frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "seed",
        "user", "tools", "tool_choice", "parallel_tool_calls", "functions",
        "function_call", "response_format", "modalities", "audio", "prediction",
        "reasoning_effort", "service_tier", "metadata", "store",
    }
)

# Explicitly-named provider routing/fallback aliases (a subset of "not portable")
# — listed so a dropped one is logged by name for the audit trail.
_ROUTING_ALIASES = frozenset({"models", "provider", "route", "transforms"})

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


def _client_is_local(request: Request, *, trust_local_no_peer: bool) -> bool:
    """True when the request's peer is a real loopback address. The gateway is a
    LOCAL capability; a remote peer must never reach it even with a token.

    Fail-closed: a missing peer (``request.client is None``) is REJECTED by default.
    A peerless request can be a unix-socket deployment, but it can equally be a
    misconfigured proxy that strips the peer — so it is trusted ONLY when the server
    is explicitly configured for a trusted unix-socket deployment
    (``trust_local_no_peer``). Loopback is decided with ``ipaddress`` (covers all of
    127.0.0.0/8, ::1, and IPv4-mapped loopback), never a string prefix that a host
    like ``127.evil.example`` could spoof."""
    client = request.client
    if client is None:  # unix socket / ASGI with no TCP peer
        return trust_local_no_peer
    host = (client.host or "").strip()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"  # the only non-numeric host we accept
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:  # ::ffff:127.0.0.1 → 127.0.0.1
        ip = mapped
    return ip.is_loopback


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


def _encoded_forms(plain: str) -> set[str]:
    """The common ENCODED transforms a provider might apply when reflecting a request
    string ``plain`` back to us — URL percent-encoding, JSON-string escaping, and
    base64 (standard + url-safe, padded and unpadded) — PLUS the raw value itself.
    Deliberately bounded to these specific, realistic forms (not a regex-of-everything)."""
    forms: set[str] = {plain}
    # URL percent-encoding (safe="" → the whole string is encoded).
    forms.add(urllib.parse.quote(plain, safe=""))
    # JSON-string escaping (drop the quotes json.dumps wraps it in).
    forms.add(json.dumps(plain)[1:-1])
    raw = plain.encode("utf-8")
    for enc in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        s = enc.decode("ascii")
        forms.add(s)
        forms.add(s.rstrip("="))  # unpadded variant
    return forms


def _key_forms(api_key: str | None) -> list[str]:
    """The ONE shared encoded-aware needle set used for BOTH the Pi-facing egress
    redaction AND the server-side log redaction. Every literal + encoded STRING form
    of the provider key a hostile/buggy upstream could echo back (or that could
    surface in an upstream error we log): the bare key, its ``Bearer <key>``
    Authorization-header form, and the encoded transforms (percent / JSON-escape /
    base64) of BOTH — because base64 is NOT substring-preserving, the encoded whole
    ``Bearer <key>`` header is not found by the bare-key encodings and must be added
    explicitly. Sorted LONGEST-first so an overlapping/containing form is replaced
    before a shorter one nested inside it (``Bearer <key>`` before ``<key>``; a
    padded base64 before its unpadded prefix)."""
    if not api_key:
        return []
    forms: set[str] = set()
    for plain in (api_key, f"Bearer {api_key}"):  # bare key AND the full header string
        forms |= _encoded_forms(plain)
    return sorted((f for f in forms if f), key=len, reverse=True)


def _key_byte_needles(api_key: str | None) -> list[bytes]:
    """The shared ``_key_forms`` needle set as BYTES (for redacting a relayed body /
    SSE chunk), re-sorted LONGEST-first by byte length."""
    needles = [f.encode("utf-8") for f in _key_forms(api_key)]
    return sorted(needles, key=len, reverse=True)


def _redact_text(text: str, api_key: str | None) -> str:
    """Strip every (literal + encoded) form of the provider key from a Pi-facing or
    server-LOGGED string (content-type, error message, log line) using the SAME
    encoded-aware needle set as the egress byte redaction — so an encoded key form
    (esp. base64) in an upstream error body is redacted in OUR logs too."""
    for needle in _key_forms(api_key):
        text = text.replace(needle, _REDACTED)
    return text


def _redact_bytes(data: bytes, api_key: str | None) -> bytes:
    """Strip every (literal + encoded) form of the provider key from Pi-facing BYTES (a
    relayed body or SSE chunk). Best-effort per buffer: a hostile/buggy upstream that
    echoes the request ``Authorization`` header (or embeds the key in a body / error /
    SSE chunk, possibly URL- or base64-encoded) must never hand it back to Pi."""
    for needle in _key_byte_needles(api_key):
        data = data.replace(needle, _REDACTED.encode("ascii"))
    return data


async def _redact_stream(
    chunks: AsyncIterator[bytes], api_key: str | None
) -> AsyncIterator[bytes]:
    """Redact the provider key from a stream of bytes ACROSS chunk boundaries.

    Per-chunk redaction alone leaks a key split between two SSE chunks
    (``sk-ab`` | ``cdef`` reassembles intact on Pi's side). This keeps a carry-over
    buffer of the longest needle minus one byte between chunks, so a key straddling a
    boundary is reconstructed and caught before it reaches Pi; only bytes that cannot
    possibly start a still-unfinished match are emitted, and the tail is flushed
    (redacted) at stream end. With no key configured it is a transparent pass-through."""
    needles = _key_byte_needles(api_key)
    if not needles:
        async for chunk in chunks:
            if chunk:
                yield chunk
        return
    keep = max(len(n) for n in needles) - 1  # retain enough to complete a split match
    carry = b""
    async for chunk in chunks:
        if not chunk:
            continue
        buf = _redact_bytes(carry + chunk, api_key)
        if len(buf) > keep:
            yield buf[:-keep]
            carry = buf[-keep:]
        else:
            carry = buf
    if carry:
        yield _redact_bytes(carry, api_key)


def _sanitize_body(body: dict, *, model_id: str) -> tuple[dict, list[str]]:
    """Build the upstream body from ONLY portable OpenAI chat-completion fields and
    PIN the model. Provider routing/fallback aliases (``models``/``provider``/
    ``route``/``transforms``) and any other non-portable key are dropped, so Pi
    cannot steer routing past the pinned model. Returns (clean_body, dropped_keys)."""
    clean = {k: v for k, v in body.items() if k in _PORTABLE_CHAT_FIELDS}
    dropped = sorted(k for k in body if k not in _PORTABLE_CHAT_FIELDS)
    clean["model"] = model_id  # pinned model overrides any Pi-sent `model`
    return clean, dropped


def _estimate_prompt_tokens(body: dict) -> int:
    """Rough up-front prompt-size estimate (~4 chars/token) so budget is charged
    BEFORE the call — not only when the provider returns a usage tail. Reconciled to
    the provider's actual counts after completion (``settle_usage``)."""
    try:
        text = json.dumps(body.get("messages", ""), ensure_ascii=False)
        tools = body.get("tools")
        if tools:
            text += json.dumps(tools, ensure_ascii=False)
    except (TypeError, ValueError):
        text = ""
    return max(1, len(text) // 4)


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
    trust_local_no_peer: bool = False,
) -> APIRouter:
    """Build the gateway router. ``token_store`` is the shared run-scoped token store.
    ``config_store`` / ``secret_store`` default to the real shared stores (constructed
    fresh per request so a settings/secret change is honored live, mirroring the rest
    of the agent-server). ``http_transport`` is a test seam for the UPSTREAM provider
    call (an ``httpx.MockTransport``); None = real network. ``trust_local_no_peer``
    opts a trusted unix-socket deployment into accepting peerless requests
    (``request.client is None``); it defaults False (fail-closed)."""
    router = APIRouter()

    def _stores() -> tuple[ConfigStore, SecretStore]:
        cs = config_store if config_store is not None else ConfigStore()
        ss = secret_store if secret_store is not None else SecretStore()
        return cs, ss

    @router.post("/internal/pi-kernel/v1/chat/completions")
    async def pi_chat_completions(request: Request) -> Response:
        # 1) Loopback-only. A remote peer never reaches the model, token or not.
        if not _client_is_local(request, trust_local_no_peer=trust_local_no_peer):
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

        # 3) Parse Pi's OpenAI-compatible body.
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"message": "request body must be a JSON object", "type": "bad_request"}},
                status_code=400,
            )

        # 4) Resolve the BOUND model (token, not request) → provider target.
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

        # 5) PIN + SANITIZE: keep only portable chat fields, pin the bound model id,
        #    and DROP provider routing/fallback aliases so Pi cannot steer routing
        #    past the pin. Messages/tools/params are relayed verbatim otherwise (no
        #    Disco system-prompt / tool-schema injection).
        clean, dropped = _sanitize_body(body, model_id=target.model_id)
        if dropped:
            _LOG.info(
                "pi-gateway dropped non-portable fields token=%s fields=%s",
                rec.fingerprint, ",".join(dropped),
            )
        stream = bool(clean.get("stream", False))

        # 6) Budget: atomically RESERVE an up-front prompt estimate (the real gate —
        #    a concurrent/oversized request cannot slip past), clamp max_tokens to the
        #    remaining budget, and force usage emission on streams so usage always
        #    accrues. Reconciled to actual usage after the call.
        estimate = _estimate_prompt_tokens(clean)
        if not token_store.reserve(token, estimate):
            _LOG.info(
                "pi-gateway over budget token=%s used=%d budget=%d",
                rec.fingerprint, rec.used_tokens, rec.budget_tokens,
            )
            return JSONResponse(
                {"error": {"message": "token budget exhausted", "type": "budget_exceeded"}},
                status_code=429,
            )
        remaining = rec.budget_remaining()
        if remaining is not None:
            if remaining <= 0:
                # The reservation consumed the last of the budget — there is no room
                # to generate. Reject rather than force a min-1 cap (which would let a
                # request through at an exhausted budget). REFUND the reservation: this
                # request never reaches upstream, so it must not burn the budget it
                # just reserved (otherwise a rejected request silently drains the cap).
                token_store.release(token, estimate)
                _LOG.info(
                    "pi-gateway no budget remaining after reserve token=%s used=%d budget=%d",
                    rec.fingerprint, rec.used_tokens, rec.budget_tokens,
                )
                return JSONResponse(
                    {"error": {"message": "token budget exhausted", "type": "budget_exceeded"}},
                    status_code=429,
                )
            cap = remaining
            # Clamp max_tokens (injecting it when absent so generation is always
            # capped to what the budget allows).
            requested = clean.get("max_tokens")
            if not isinstance(requested, int) or requested > cap:
                clean["max_tokens"] = cap
            # Clamp any OTHER whitelisted generation-limit field Pi may have set, so it
            # cannot bypass the cap on a provider that honors it (e.g. OpenAI's
            # ``max_completion_tokens``). Only clamp DOWN — never inject a field Pi did
            # not send.
            for gen_field in ("max_completion_tokens",):
                val = clean.get(gen_field)
                if isinstance(val, int) and val > cap:
                    clean[gen_field] = cap
        if stream:
            opts = clean.get("stream_options")
            opts = dict(opts) if isinstance(opts, dict) else {}
            opts["include_usage"] = True  # force a usage tail so streams are charged
            clean["stream_options"] = opts

        payload = json.dumps(clean).encode("utf-8")
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
                # Reconcile budget (no usage → reservation stands) and return a
                # GENERIC error; the real (redacted) detail is logged server-side.
                token_store.settle_usage(
                    token, reserved=estimate, input_tokens=None, output_tokens=None
                )
                _LOG.warning(
                    "pi-gateway upstream connection error token=%s detail=%s",
                    rec.fingerprint, _redact_text(str(exc), target.api_key),
                )
                return JSONResponse(
                    {"error": {"message": _GENERIC_UPSTREAM_ERROR, "type": "upstream"}},
                    status_code=502,
                )
            data = resp.content
            status = resp.status_code
            if status >= 400:
                # An upstream error BODY can echo request data/headers (and thus the
                # injected key). Never relay it: replace with the generic message (the
                # status code may pass through, the body must not). Log the real,
                # redacted body server-side only.
                await client.aclose()
                token_store.settle_usage(
                    token, reserved=estimate, input_tokens=None, output_tokens=None
                )
                _LOG.warning(
                    "pi-gateway upstream error status=%d token=%s body=%s",
                    status, rec.fingerprint,
                    _redact_text(data.decode("utf-8", "replace"), target.api_key),
                )
                return JSONResponse(
                    {"error": {"message": _GENERIC_UPSTREAM_ERROR, "type": "upstream"}},
                    status_code=status,
                )
            media = _redact_text(
                resp.headers.get("content-type", "application/json"), target.api_key
            )
            await client.aclose()
            # Reconcile usage from the provider's report (or keep the reservation).
            try:
                u = _extract_usage(json.loads(data))
            except (json.JSONDecodeError, ValueError):
                u = None
            token_store.settle_usage(
                token,
                reserved=estimate,
                input_tokens=(u[0] if u is not None else None),
                output_tokens=(u[1] if u is not None else None),
            )
            if u is not None:
                _LOG.info(
                    "pi-gateway usage token=%s model=%s in=%d out=%d",
                    rec.fingerprint, target.model_id, u[0], u[1],
                )
            # Relay the provider's response, but REDACT any provider-key bytes a
            # hostile/buggy upstream or proxy might have echoed back.
            return Response(
                content=_redact_bytes(data, target.api_key),
                status_code=status,
                media_type=media,
            )

        # Streaming: open the upstream stream and relay bytes through unchanged,
        # observing the trailing usage for accounting without altering the stream.
        async def _proxy_stream() -> AsyncIterator[bytes]:
            tail = b""

            async def _raw() -> AsyncIterator[bytes]:
                # Observe the ORIGINAL bytes for usage accounting; the redaction layer
                # below relays the (cross-chunk-redacted) bytes to Pi.
                nonlocal tail
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        tail = (tail + chunk)[-_USAGE_TAIL_BYTES:]
                        yield chunk

            try:
                async with client.stream(
                    "POST", url, content=payload, headers=headers
                ) as resp:
                    if resp.status_code >= 400:
                        # An upstream error BODY can echo request data/headers (and the
                        # injected key). Never relay it — emit the generic message and
                        # log the real, redacted body server-side only.
                        body = await resp.aread()
                        _LOG.warning(
                            "pi-gateway upstream stream error status=%d token=%s body=%s",
                            resp.status_code, rec.fingerprint,
                            _redact_text(body.decode("utf-8", "replace"), target.api_key),
                        )
                        yield b'data: {"error": {"message": "upstream request failed"}}\n\n'
                        return
                    # Redact the provider key ACROSS chunk boundaries before it reaches
                    # Pi (a key split between two SSE chunks would otherwise reassemble).
                    async for out in _redact_stream(_raw(), target.api_key):
                        yield out
            except httpx.HTTPError as exc:
                # Generic Pi-facing error; real (redacted) detail logged server-side.
                _LOG.warning(
                    "pi-gateway upstream stream error token=%s detail=%s",
                    rec.fingerprint, _redact_text(str(exc), target.api_key),
                )
                yield b'data: {"error": {"message": "upstream request failed"}}\n\n'
            finally:
                await client.aclose()
                in_tok, out_tok = _usage_from_sse_tail(tail.decode("utf-8", "replace"))
                if in_tok or out_tok:
                    # Reconcile the up-front reservation to actual stream usage.
                    token_store.settle_usage(
                        token, reserved=estimate, input_tokens=in_tok, output_tokens=out_tok
                    )
                    _LOG.info(
                        "pi-gateway usage token=%s model=%s in=%d out=%d",
                        rec.fingerprint, target.model_id, in_tok, out_tok,
                    )

        return StreamingResponse(_proxy_stream(), media_type="text/event-stream")

    return router
