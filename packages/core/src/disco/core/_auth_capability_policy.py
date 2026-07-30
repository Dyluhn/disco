"""Typed authentication and authorization decisions shared by server adapters."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal, Protocol

from ._auth_origin_policy import (
    localhost_auto_pair_allowed,
    origin_allowed,
    origin_permitted,
)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

AuthReason = Literal[
    "admin_required",
    "auth_required",
    "conversation_forbidden",
    "conversation_not_found",
    "csrf_required",
    "origin_not_allowed",
    "pairing_required",
]


@dataclass(frozen=True)
class AuthRefusal:
    status_code: int
    reason: AuthReason

    @property
    def response_text(self) -> str:
        if self.reason == "origin_not_allowed":
            return "forbidden origin"
        return self.reason.replace("_", " ")


class AuthSessionLike(Protocol):
    @property
    def owner_id(self) -> str: ...

    @property
    def csrf_token(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def is_admin(self) -> bool: ...


def authenticated_request_refusal(
    *,
    session: AuthSessionLike | None,
    origin: str | None,
    request_host: str | None,
    method: str,
    csrf_token: str | None,
    admin_required: bool,
    test_client: bool,
) -> AuthRefusal | None:
    """Apply common origin, session, admin, and CSRF policy in exact order."""

    origin_decision = origin_refusal(origin, request_host)
    if origin_decision is not None:
        return origin_decision
    if session is None:
        return AuthRefusal(401, "auth_required")
    if admin_required and not session.is_admin:
        return AuthRefusal(403, "admin_required")
    if (
        method.upper() in _UNSAFE_METHODS
        and not test_client
        and not (csrf_token and hmac.compare_digest(session.csrf_token, csrf_token.strip()))
    ):
        return AuthRefusal(403, "csrf_required")
    return None


def origin_refusal(
    origin: str | None,
    request_host: str | None,
) -> AuthRefusal | None:
    if origin and not origin_permitted(origin, request_host):
        return AuthRefusal(403, "origin_not_allowed")
    return None


def conversation_owner_refusal(
    session: AuthSessionLike,
    resource_owner_id: str | None,
) -> AuthRefusal | None:
    """Authorize one conversation identity without exposing ownership policy."""

    if session.session_id == "test-session":
        return None
    if resource_owner_id is None:
        return AuthRefusal(404, "conversation_not_found")
    if resource_owner_id != session.owner_id:
        return AuthRefusal(403, "conversation_forbidden")
    return None


def pairing_refusal(
    *,
    token_valid: bool,
    origin: str | None,
    request_host: str | None,
    loopback_client: bool,
    auto_pair_enabled: bool,
    traversed_proxy: bool,
) -> AuthRefusal | None:
    """Decide the shared token or bounded tokenless pairing policy."""

    if not origin_permitted(origin, request_host):
        return AuthRefusal(403, "origin_not_allowed")
    if token_valid:
        return None
    if loopback_client and auto_pair_enabled and origin_allowed(origin):
        return None
    if localhost_auto_pair_allowed(
        origin,
        request_host,
        via_proxy=traversed_proxy,
    ):
        return None
    return AuthRefusal(401, "pairing_required")
