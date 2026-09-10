"""WO-A2.1 — the host-service registry, proven wired end-to-end (no security surface).

Covers:
  * REGISTRATION discipline mirrors the primitive registry: a second DIFFERENT
    definition for a claimed name is a hard error; re-registering the SAME
    object is idempotent;
  * STRICT resolve: `get_host_service` returns None for an unknown name, never
    a fallback; `host_service_names` reports what's registered;
  * `svc.ping` happy path through `call_host_service` (echo, no ctx deps);
  * a declared `payload_schema` is enforced BEFORE the handler runs — a bad
    payload raises the typed `HostServicePayloadError` and the handler never
    fires; a good payload reaches the handler NORMALIZED (defaults filled,
    extras dropped);
  * an unknown service name raises the typed `UnknownHostServiceError`.
"""

from __future__ import annotations

from typing import Any

import pytest
from disco.core.host_services import (
    PING_SERVICE,
    PING_SERVICE_NAME,
    HostServiceContext,
    HostServiceDefinition,
    HostServiceError,
    HostServicePayloadError,
    UnknownHostServiceError,
    call_host_service,
    get_host_service,
    host_service_names,
    register_host_service,
    return_url_allowed,
)
from pydantic import BaseModel


async def _noop_handler(payload: dict[str, Any], ctx: HostServiceContext) -> dict[str, Any]:
    del payload, ctx
    return {"ok": True}


# ---- 1. registration discipline ------------------------------------------------


def test_duplicate_name_with_different_definition_is_refused() -> None:
    first = HostServiceDefinition(
        name="svc.test.dup",
        handler=_noop_handler,
        description="first claimant",
    )
    register_host_service(first)
    impostor = HostServiceDefinition(
        name="svc.test.dup",
        handler=_noop_handler,
        description="a DIFFERENT definition for the same name",
    )
    with pytest.raises(ValueError, match="duplicate host service name"):
        register_host_service(impostor)
    # the original registration is untouched
    assert get_host_service("svc.test.dup") is first


def test_reregistering_the_same_object_is_idempotent() -> None:
    register_host_service(PING_SERVICE)  # already registered at import time
    register_host_service(PING_SERVICE)
    assert get_host_service(PING_SERVICE_NAME) is PING_SERVICE


# ---- 2. strict resolve ----------------------------------------------------------


def test_get_host_service_is_strict_no_fallback() -> None:
    assert get_host_service("svc.no.such.service") is None


def test_host_service_names_reports_registrations() -> None:
    names = host_service_names()
    assert PING_SERVICE_NAME in names
    assert "svc.no.such.service" not in names


# ---- 3. svc.ping happy path -----------------------------------------------------


async def test_ping_echoes_payload_shape() -> None:
    result = await call_host_service(PING_SERVICE_NAME, {"b": 2, "a": 1}, HostServiceContext())
    assert result == {
        "ok": True,
        "service": PING_SERVICE_NAME,
        "payload_size": 2,
        "payload_keys": ["a", "b"],
    }


async def test_ping_with_empty_payload() -> None:
    result = await call_host_service(PING_SERVICE_NAME, {}, HostServiceContext())
    assert result["ok"] is True
    assert result["payload_size"] == 0
    assert result["payload_keys"] == []


# ---- 4. payload_schema enforcement ----------------------------------------------


class _EchoPayload(BaseModel):
    message: str
    repeat: int = 1


_SCHEMA_CALLS: list[dict[str, Any]] = []


async def _schema_handler(payload: dict[str, Any], ctx: HostServiceContext) -> dict[str, Any]:
    del ctx
    _SCHEMA_CALLS.append(payload)
    return {"ok": True, "echo": payload["message"] * payload["repeat"]}


register_host_service(
    HostServiceDefinition(
        name="svc.test.schema_echo",
        handler=_schema_handler,
        description="test-only schema'd service",
        payload_schema=_EchoPayload,
    )
)


async def test_schema_violation_is_typed_error_and_handler_never_fires() -> None:
    _SCHEMA_CALLS.clear()
    with pytest.raises(HostServicePayloadError, match="svc.test.schema_echo"):
        await call_host_service(
            "svc.test.schema_echo",
            {"repeat": 2},
            HostServiceContext(),  # message missing
        )
    assert _SCHEMA_CALLS == []
    # typed errors share the HostServiceError base the bus dispatches on
    assert issubclass(HostServicePayloadError, HostServiceError)


async def test_valid_payload_reaches_handler_normalized() -> None:
    _SCHEMA_CALLS.clear()
    result = await call_host_service(
        "svc.test.schema_echo",
        {"message": "hi", "stray_extra": True},  # extra dropped, repeat defaulted
        HostServiceContext(),
    )
    assert result == {"ok": True, "echo": "hi"}
    assert _SCHEMA_CALLS == [{"message": "hi", "repeat": 1}]


# ---- 5. unknown service ----------------------------------------------------------


async def test_unknown_service_is_typed_error() -> None:
    with pytest.raises(UnknownHostServiceError, match="svc.definitely.missing"):
        await call_host_service("svc.definitely.missing", {}, HostServiceContext())
    assert issubclass(UnknownHostServiceError, HostServiceError)


@pytest.mark.parametrize(
    "url",
    [
        "https://app.example.com/success",
        "https://app.example.com/cancel?from=checkout",
    ],
)
def test_return_url_allowed_uses_credential_bound_origin(url: str) -> None:
    ctx = HostServiceContext(allowed_origins=frozenset({"https://app.example.com"}))
    assert return_url_allowed(ctx, url)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/success",
        "https://app.example.com.evil.example/success",
        "https://user@app.example.com/success",
        "javascript:alert(1)",
        "https://app.example.com/success#fragment",
    ],
)
def test_return_url_allowed_fails_closed(url: str) -> None:
    ctx = HostServiceContext(allowed_origins=frozenset({"https://app.example.com"}))
    assert not return_url_allowed(ctx, url)
    assert not return_url_allowed(HostServiceContext(), url)
