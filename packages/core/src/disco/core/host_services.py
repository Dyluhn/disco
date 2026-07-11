"""WO-A2.1 — the HOST-SERVICE registry (plan §10.2, slice 1 of WO-A2).

Generated apps run as Cloudflare Workers under wrangler/workerd INSIDE the
sandbox; they cannot import host SDKs or hold secrets. A "host service" is a
named capability ("email.send", "ai.chat", …) the app reaches over ONE HTTP hop
to the host plane, where the handler composes the existing S-W2 security
chokepoints. This module is that registry + dispatcher and NOTHING more:

* ``HostServiceDefinition`` — name → async handler (+ optional pydantic payload
  schema), mirroring the appkit primitive registry's conventions exactly
  (duplicate-name hard error, idempotent same-object re-register);
* ``HostServiceContext`` — the host-plane dependencies a handler composes
  (SecretStore / OriginApprovalStore / egress allowlist), shaped to slot
  STRAIGHT into ``resolve_provider_secret`` / ``OriginApprovalStore.is_approved``
  / ``guarded_request(allow_hosts=…)``;
* ``call_host_service`` — strict resolve + schema validation + invoke, raising
  TYPED errors the future bus endpoint maps to HTTP statuses.

⚠ NO AUTH LIVES HERE. Authentication/authorization of the calling app is the
WO-A2.2 security-classed slice (per-app conversation-bound bearer, minted
host-side, injected as a Worker env var). The bus endpoint MUST NOT expose
``call_host_service`` to the sandbox without the A2.2 auth layer in front of it.
See ``docs/wo-a2-host-bus-design-notes.md``.

HANDLER DISCIPLINE (plan §10.2 A2.1): handlers are THIN ADAPTERS that only
compose existing S-W2 calls — no new URL validation, no token minting, no
secret handling beyond passing resolver outputs along. A handler that needs a
new enforcement primitive is out of scope by definition and escalates. The
canonical shape for an egress-touching handler (pattern documentation ONLY —
the first live one is WO-A2.4 ``email.send``)::

    # import asyncio, json
    # from disco.core.host_egress import EgressDenied, guarded_request
    # from disco.core.llm.secret_refs import (
    #     resolve_provider_secret,
    #     secret_ref_allowed_for_origin,
    # )
    #
    # _SEND_URL = "https://smtp-bridge.example.com/send"   # operator-configured
    # _SECRET_REF = "smtp"                                  # SecretStore ref id
    # _PURPOSE = "email.send"
    #
    # async def _send_email(payload: dict[str, Any], ctx: HostServiceContext) -> dict[str, Any]:
    #     # 1. resolve the secret HOST-SIDE (never enters the sandbox or the tree)
    #     secret = resolve_provider_secret(_SECRET_REF, ctx.secret_store)
    #     if secret is None:
    #         return {"ok": False, "error": f"secret ref {_SECRET_REF!r} is not configured"}
    #     # 2. refuse a ref that is origin-pinned to somewhere else
    #     if not secret_ref_allowed_for_origin(_SECRET_REF, _SEND_URL):
    #         return {"ok": False, "error": "secret ref not allowed for this origin"}
    #     # 3. the target origin must carry a SIGNED operator approval
    #     approved = ctx.approvals is not None and ctx.approvals.is_approved(
    #         _SEND_URL, _PURPOSE, _SECRET_REF
    #     )
    #     if not approved:
    #         return {"ok": False, "error": "origin awaiting operator approval"}
    #     # 4. all wire traffic goes through the guarded egress chokepoint
    #     try:
    #         resp = await asyncio.to_thread(
    #             guarded_request,
    #             "POST",
    #             _SEND_URL,
    #             headers={"Authorization": f"Bearer {secret}"},
    #             body=json.dumps(payload).encode("utf-8"),
    #             allow_hosts=ctx.allow_hosts,
    #             timeout_s=ctx.request_timeout_s,
    #         )
    #     except EgressDenied as exc:
    #         return {"ok": False, "error": f"egress denied: {exc}"}
    #     return {"ok": resp.status_code < 300, "status": resp.status_code}

PURITY / LAYERING: ``disco.core`` is the leaf package (.importlinter). This
module imports only the stdlib at import time — core siblings and pydantic are
typing-only or function-local — matching ``appkit/primitives.py``'s discipline,
so registering services never drags the egress/secrets stack into importers
that only need the registry shape.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from pydantic import BaseModel

    from .llm.secrets import SecretStore
    from .origin_approvals import OriginApprovalStore
    from .stripe_host_service import StripeAppConfigStore
    from .webhook_host_service import WebhookAppConfigStore


class HostServiceError(Exception):
    """Base class for typed host-service dispatch errors (bus → HTTP mapping)."""


class UnknownHostServiceError(HostServiceError):
    """Strict resolve failed: no service registered under that name (bus → 404)."""


class HostServicePayloadError(HostServiceError):
    """The payload failed the service's declared ``payload_schema`` (bus → 422)."""


MAX_HOST_SERVICE_NAME_BYTES = 128
_HOST_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


def valid_host_service_name(name: str) -> bool:
    """Return whether name has the one canonical A2 dotted-service shape."""
    return (
        len(name.encode("utf-8")) <= MAX_HOST_SERVICE_NAME_BYTES
        and _HOST_SERVICE_NAME_RE.fullmatch(name) is not None
    )


@dataclass(frozen=True)
class HostServiceContext:
    """The host-plane dependencies a handler composes, shaped for the S-W2 calls:
    ``secret_store`` feeds ``resolve_provider_secret(ref, store)``, ``approvals``
    is checked via ``OriginApprovalStore.is_approved(url, purpose, secret_ref)``,
    and ``allow_hosts`` passes through to ``guarded_request(allow_hosts=…)``.

    ``allow_hosts=None`` means "no EXTRA host allowlist" — ``guarded_request``
    still enforces its class-1 policy (public-IP-only, redirect revalidation)
    unconditionally; None never disables that."""

    secret_store: SecretStore | None = None
    approvals: OriginApprovalStore | None = None
    allow_hosts: frozenset[str] | None = None
    app_id: str = ""
    conversation_id: str = ""
    owner_id: str = ""
    allowed_services: frozenset[str] = frozenset()
    allowed_origins: frozenset[str] = frozenset()
    credential_kind: str = ""
    credential_generation: int = 0
    request_timeout_s: float = 5.0
    stripe_config_store: StripeAppConfigStore | None = None
    webhook_config_store: WebhookAppConfigStore | None = None


def return_url_allowed(ctx: HostServiceContext, url: str) -> bool:
    """Check a callback/return URL against credential-bound app origins.

    This is separate from outbound service egress and signed operator approval.
    Any handler accepting a browser return or callback URL must call this helper.
    """
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False
    from .host_egress import origin_for_url

    origin = origin_for_url(url)
    return origin is not None and origin in ctx.allowed_origins


# A handler takes the (validated) payload dict + the host-plane context and
# returns a JSON-safe result dict. Async because every real service awaits I/O
# (guarded egress via asyncio.to_thread at minimum).
HostServiceHandler = Callable[[dict[str, Any], HostServiceContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class HostServiceDefinition:
    """One host service: its dotted name ("svc.ping", "email.send"), the async
    handler, a human/model-facing description, and an OPTIONAL pydantic payload
    schema enforced by ``call_host_service`` BEFORE the handler runs. Frozen so
    a registered service can't be mutated after registration."""

    name: str
    handler: HostServiceHandler
    description: str
    payload_schema: type[BaseModel] | None = None


# The registry, keyed by dotted service name. Services register at import time
# of their owning module (exactly how generator.py populates the primitive
# registry) so the bus sees a complete map before the first dispatch.
_REGISTRY: dict[str, HostServiceDefinition] = {}


def register_host_service(defn: HostServiceDefinition) -> None:
    """Register a host service (idempotent for the SAME definition object). A
    second, DIFFERENT definition for an already-claimed name is a hard error —
    a duplicate is a bug, never a silent shadow."""
    if not valid_host_service_name(defn.name):
        raise ValueError(f"invalid host service name: {defn.name!r}")
    existing = _REGISTRY.get(defn.name)
    if existing is not None and existing is not defn:
        raise ValueError(f"duplicate host service name: {defn.name!r}")
    _REGISTRY[defn.name] = defn


def get_host_service(name: str) -> HostServiceDefinition | None:
    """Strictly look up a service by name — None if unknown, never a fallback."""
    return _REGISTRY.get(name)


def host_service_names() -> frozenset[str]:
    """The set of registered host-service names."""
    return frozenset(_REGISTRY)


async def call_host_service(
    name: str, payload: dict[str, Any], ctx: HostServiceContext
) -> dict[str, Any]:
    """Resolve → validate → invoke one host service.

    ⚠ NO AUTH IS PERFORMED HERE — the bus endpoint MUST NOT expose this to the
    sandbox without the WO-A2.2 auth layer (per-app conversation-bound bearer)
    in front of it.

    * unknown ``name`` → ``UnknownHostServiceError`` (the bus maps it to 404);
    * a declared ``payload_schema`` is enforced BEFORE the handler runs —
      failure → ``HostServicePayloadError`` (→ 422); on success the handler
      receives the schema's ``model_dump()`` (defaults filled, extras dropped),
      so schema'd handlers always see a NORMALIZED payload;
    * otherwise the raw payload dict passes through untouched.
    """
    defn = _REGISTRY.get(name)
    if defn is None:
        raise UnknownHostServiceError(f"unknown host service: {name!r}")
    if defn.payload_schema is not None:
        from pydantic import ValidationError

        try:
            validated = defn.payload_schema.model_validate(payload)
        except ValidationError as exc:
            raise HostServicePayloadError(
                f"payload for host service {name!r} failed validation: {exc}"
            ) from exc
        payload = validated.model_dump()
    return await defn.handler(payload, ctx)


# ---- svc.ping — the zero-security reference service ---------------------------
#
# A pure echo: no egress, no secrets, no approvals. It exists so the registry +
# dispatcher (and later the A2.2 bus + A2.3 client shim) are provably wired
# end-to-end without touching a single security surface.

PING_SERVICE_NAME = "svc.ping"


async def _ping_handler(payload: dict[str, Any], ctx: HostServiceContext) -> dict[str, Any]:
    del ctx  # deliberately unused — ping touches no host-plane dependency
    return {
        "ok": True,
        "service": PING_SERVICE_NAME,
        "payload_size": len(payload),
        "payload_keys": sorted(payload),
    }


PING_SERVICE = HostServiceDefinition(
    name=PING_SERVICE_NAME,
    handler=_ping_handler,
    description="Echo reference service: returns ok + the payload's size and keys. "
    "No egress, no secrets — proves the registry/bus wiring end-to-end.",
)
register_host_service(PING_SERVICE)


__all__ = [
    "PING_SERVICE",
    "PING_SERVICE_NAME",
    "HostServiceContext",
    "HostServiceDefinition",
    "HostServiceError",
    "HostServiceHandler",
    "HostServicePayloadError",
    "MAX_HOST_SERVICE_NAME_BYTES",
    "UnknownHostServiceError",
    "call_host_service",
    "get_host_service",
    "host_service_names",
    "register_host_service",
    "return_url_allowed",
    "valid_host_service_name",
]
