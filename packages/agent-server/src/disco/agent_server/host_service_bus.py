"""Authenticated, scoped WO-A2.2 host-service bus endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from disco.core.events import LLMMessage
from disco.core.host_services import (
    HostServiceContext,
    HostServicePayloadError,
    HostServiceUsage,
    call_host_service,
    get_host_service,
    valid_host_service_name,
)
from disco.core.llm import CapabilityProfile, CompletionRequest, ModelRole
from disco.core.llm.routing import CallContext
from disco.core.quota import QuotaError, QuotaStore
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    PAYMENTS_READY_SERVICE_NAME,
    STRIPE_API_HOSTS,
    StripeAppConfigStore,
)
from disco.core.webhook_host_service import WEBHOOK_EMIT_SERVICE_NAME, WebhookAppConfigStore
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .ai_chat_host_service import AI_CHAT_SERVICE_NAME
from .host_token_store import HostTokenRecord, HostTokenStore

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .runtime import ConversationRuntime

_LOG = logging.getLogger(__name__)

_BUS_PATH_PREFIX = "/_disco/svc/"
_BUS_PATH_PREFIX_BYTES = _BUS_PATH_PREFIX.encode("ascii")
_MAX_BODY_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
# Test-only/global emergency override. Normal operation uses each registered
# service's bounded timeout; ai.chat therefore is not constrained by egress's 6s.
_HANDLER_TIMEOUT_S: float | None = None
_DOWNSTREAM_TIMEOUT_S = 5.0
_SERVICE_ALLOW_HOSTS: Mapping[str, frozenset[str] | None] = {
    "svc.ping": frozenset(),
    PAYMENTS_CHECKOUT_SERVICE_NAME: STRIPE_API_HOSTS,
    PAYMENTS_READY_SERVICE_NAME: frozenset(),
    # Target host is operator-configured and origin-approved. None means no
    # additional static allowlist; guarded_request still enforces public IPs,
    # connection-peer validation, and redirect revalidation unconditionally.
    WEBHOOK_EMIT_SERVICE_NAME: None,
    AI_CHAT_SERVICE_NAME: frozenset(),
}


class _BusClientError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_bus_route_raw(raw_path: bytes) -> bool:
    """Return true only for a single raw path segment under the bus prefix.

    Name validation remains in the route so malformed/encoded candidates receive
    the bus's sanitized 400 response rather than browser-session authentication.
    """
    if not raw_path.startswith(_BUS_PATH_PREFIX_BYTES):
        return False
    service_bytes = raw_path[len(_BUS_PATH_PREFIX_BYTES) :]
    return bool(service_bytes) and b"/" not in service_bytes


def _extract_service(raw_path: bytes) -> str | None:
    if not is_bus_route_raw(raw_path):
        return None
    service_bytes = raw_path[len(_BUS_PATH_PREFIX_BYTES) :]
    if b"%" in service_bytes or b";" in service_bytes:
        return None
    try:
        service = service_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None
    return service if valid_host_service_name(service) else None


_extract_service_name = _extract_service


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    return [
        value
        for header_name, value in request.scope.get("headers", ())
        if header_name.lower() == name
    ]


def _extract_bearer(request: Request) -> str | None:
    values = _raw_header_values(request, b"authorization")
    if len(values) != 1:
        return None
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    if parts[1] != parts[1].strip() or any(ch.isspace() for ch in parts[1]):
        return None
    return parts[1]


def _one_header(request: Request, name: bytes) -> bytes | None:
    values = _raw_header_values(request, name)
    if len(values) > 1:
        raise _BusClientError("ambiguous_headers")
    return values[0] if values else None


async def _read_capped_json_object(request: Request) -> dict[str, Any]:
    if _raw_header_values(request, b"transfer-encoding"):
        raise _BusClientError("unsupported_framing")

    content_encoding = _one_header(request, b"content-encoding")
    if content_encoding is not None and content_encoding.strip().lower() != b"identity":
        raise _BusClientError("unsupported_encoding")

    content_type = _one_header(request, b"content-type")
    if content_type is None:
        raise _BusClientError("content_type")
    media_type = content_type.split(b";", 1)[0].strip().lower()
    if media_type != b"application/json":
        raise _BusClientError("content_type")

    declared: int | None = None
    length_value = _one_header(request, b"content-length")
    if length_value is not None:
        if not length_value.isdigit():
            raise _BusClientError("bad_length")
        declared = int(length_value)
        if declared > _MAX_BODY_BYTES:
            raise _BusClientError("oversize_body")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise _BusClientError("oversize_body")
    if declared is not None and len(body) != declared:
        raise _BusClientError("bad_length")
    if not body:
        raise _BusClientError("invalid_json")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _BusClientError("duplicate_keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise _BusClientError("invalid_json")

    try:
        parsed = json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _BusClientError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise _BusClientError("json_not_object")
    return parsed


HostServiceContextFactory = Callable[[str, HostTokenRecord], HostServiceContext]


def _audit(record: HostTokenRecord, service: str, outcome: str) -> None:
    _LOG.info(
        "host service call outcome=%s selector=%s conversation=%s app=%s service=%s",
        outcome,
        record.selector,
        record.conversation_id,
        record.audience,
        service,
    )


def _ai_chat_callback(
    runtime: ConversationRuntime, record: HostTokenRecord
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Bind tool-free inference to the authenticated conversation principal."""

    async def complete(payload: dict[str, Any]) -> dict[str, Any]:
        messages = [LLMMessage.model_validate(value) for value in payload["messages"]]
        response = await runtime._router_now(
            conversation_id=record.conversation_id
        ).complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=messages,
                tools=None,
                temperature=payload["temperature"],
                max_tokens=payload["max_tokens"],
                enable_thinking=False,
            ),
            context=CallContext(conversation_id=record.conversation_id),
        )
        return {
            "ok": True,
            "text": response.text,
            "finish_reason": response.finish_reason,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }

    return complete


def make_host_service_context_factory(
    runtime: ConversationRuntime | None,
    stripe_config_store: StripeAppConfigStore | None = None,
    webhook_config_store: WebhookAppConfigStore | None = None,
) -> HostServiceContextFactory:
    """Construct handler context only from authenticated server-side state."""

    def factory(service: str, record: HostTokenRecord) -> HostServiceContext:
        if runtime is None:
            return HostServiceContext(
                allow_hosts=_SERVICE_ALLOW_HOSTS.get(service, frozenset()),
                app_id=record.audience,
                conversation_id=record.conversation_id,
                owner_id=record.owner_id,
                allowed_services=record.allowed_services,
                allowed_origins=record.allowed_origins,
                credential_kind=record.kind,
                credential_generation=record.generation,
                request_timeout_s=_DOWNSTREAM_TIMEOUT_S,
                stripe_config_store=stripe_config_store,
                webhook_config_store=webhook_config_store,
            )
        secret_store = runtime._secret_store
        approvals = runtime._config_store.approval_store(secret_store=secret_store)
        return HostServiceContext(
            secret_store=secret_store,
            approvals=approvals,
            allow_hosts=_SERVICE_ALLOW_HOSTS.get(service, frozenset()),
            app_id=record.audience,
            conversation_id=record.conversation_id,
            owner_id=record.owner_id,
            allowed_services=record.allowed_services,
            allowed_origins=record.allowed_origins,
            credential_kind=record.kind,
            credential_generation=record.generation,
            request_timeout_s=_DOWNSTREAM_TIMEOUT_S,
            stripe_config_store=stripe_config_store,
            webhook_config_store=webhook_config_store,
            ai_chat_complete=_ai_chat_callback(runtime, record),
        )

    return factory


def make_host_service_bus_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    token_store: HostTokenStore,
    quota_store: QuotaStore,
    stripe_config_store: StripeAppConfigStore | None = None,
    webhook_config_store: WebhookAppConfigStore | None = None,
) -> APIRouter:
    context_factory = make_host_service_context_factory(
        runtime, stripe_config_store, webhook_config_store
    )
    router = APIRouter()

    @router.post(_BUS_PATH_PREFIX + "{service:path}")
    async def host_service_bus(request: Request, service: str) -> Response:
        del service
        # Authentication precedes route validation, body parsing and lookup.
        bearer = _extract_bearer(request)
        record = token_store.verify(bearer) if bearer is not None else None
        if record is None:
            return _auth_error()
        owner = await store.conversation_owner_id(record.conversation_id)
        if owner is None or owner != record.owner_id:
            return _auth_error()

        validated_service = _extract_service(request.scope.get("raw_path", b""))
        if validated_service is None:
            _audit(record, "invalid", "bad_service")
            return _error_response(400, "bad_service")
        if validated_service not in record.allowed_services:
            _audit(record, validated_service, "service_not_allowed")
            return _error_response(403, "service_not_allowed")

        try:
            payload = await _read_capped_json_object(request)
        except _BusClientError as exc:
            _audit(record, validated_service, exc.reason)
            status = (
                413
                if exc.reason == "oversize_body"
                else 415
                if exc.reason == "content_type"
                else 400
            )
            return _error_response(status, exc.reason)

        definition = get_host_service(validated_service)
        if definition is None:
            _audit(record, validated_service, "unknown_service")
            return _error_response(404, "unknown_service")
        try:
            estimate = (
                definition.estimate_usage(payload)
                if definition.estimate_usage is not None
                else HostServiceUsage()
            )
            admission = quota_store.reserve(
                owner_id=record.owner_id,
                audience=record.audience,
                service=validated_service,
                reservation_id="q_" + secrets.token_urlsafe(18),
                estimated_input_tokens=estimate.input_tokens,
                estimated_output_tokens=estimate.output_tokens,
            )
        except (QuotaError, ValueError, OverflowError):
            _audit(record, validated_service, "quota_unavailable")
            _LOG.warning("host service quota admission failed")
            return _error_response(503, "quota_unavailable")
        if not admission.allowed or admission.reservation is None:
            retry_after = admission.retry_after_seconds or 1
            _audit(record, validated_service, "quota_exceeded")
            return JSONResponse(
                status_code=429,
                content={"error": "quota_exceeded", "limit": admission.reason},
                headers={
                    "Cache-Control": "no-store",
                    "Retry-After": str(retry_after),
                },
            )

        reservation_id = admission.reservation.reservation_id
        settled = False

        def settle(usage: HostServiceUsage) -> bool:
            nonlocal settled
            if settled:
                return True
            try:
                quota_store.complete(
                    owner_id=record.owner_id,
                    audience=record.audience,
                    reservation_id=reservation_id,
                    actual_input_tokens=usage.input_tokens,
                    actual_output_tokens=usage.output_tokens,
                )
            except (QuotaError, ValueError, OverflowError):
                _LOG.warning("host service quota settlement failed")
                return False
            settled = True
            return True

        ctx = context_factory(validated_service, record)
        try:
            result = await asyncio.wait_for(
                call_host_service(validated_service, payload, ctx),
                timeout=(
                    definition.timeout_s
                    if _HANDLER_TIMEOUT_S is None
                    else _HANDLER_TIMEOUT_S
                ),
            )
        except TimeoutError:
            settle(HostServiceUsage())
            _audit(record, validated_service, "handler_timeout")
            _LOG.warning("host service request timed out")
            return _error_response(504, "handler_timeout")
        except HostServicePayloadError:
            settle(HostServiceUsage())
            _audit(record, validated_service, "payload_error")
            return _error_response(422, "payload_error")
        except asyncio.CancelledError:
            settle(HostServiceUsage())
            raise
        except Exception:
            settle(HostServiceUsage())
            _audit(record, validated_service, "handler_error")
            _LOG.warning("host service handler failed")
            return _error_response(500, "handler_error")

        if not isinstance(result, dict):
            settle(HostServiceUsage())
            _audit(record, validated_service, "handler_error")
            _LOG.warning("host service returned an invalid result")
            return _error_response(500, "handler_error")
        try:
            actual = (
                definition.read_usage(result)
                if definition.read_usage is not None and result.get("ok") is True
                else HostServiceUsage()
            )
        except (TypeError, ValueError, OverflowError):
            if not settle(estimate):
                return _error_response(503, "quota_unavailable")
            _audit(record, validated_service, "handler_error")
            _LOG.warning("host service returned invalid metering data")
            return _error_response(500, "handler_error")
        if not settle(actual):
            _audit(record, validated_service, "quota_unavailable")
            return _error_response(503, "quota_unavailable")
        try:
            body = json.dumps(
                result,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            _audit(record, validated_service, "handler_error")
            _LOG.warning("host service returned an invalid result")
            return _error_response(500, "handler_error")
        if len(body) > _MAX_RESPONSE_BYTES:
            _audit(record, validated_service, "response_too_large")
            return _error_response(500, "response_too_large")
        _audit(record, validated_service, "ok")
        return Response(
            content=body,
            media_type="application/json",
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    return router


def _auth_error() -> Response:
    return _error_response(401, "auth_required")


def _error_response(status_code: int, reason: str) -> Response:
    return JSONResponse(
        status_code=status_code,
        content={"error": reason},
        headers={"Cache-Control": "no-store"},
    )


__all__ = [
    "_HANDLER_TIMEOUT_S",
    "_MAX_BODY_BYTES",
    "_MAX_RESPONSE_BYTES",
    "HostServiceContextFactory",
    "is_bus_route_raw",
    "make_host_service_bus_router",
    "make_host_service_context_factory",
]
