"""Persisted-row -> ``HostTokenRecord`` marshalling and validation.

Extracted from ``HostTokenStore._record_from_row`` (PKG-10-SANDBOX), which
exceeded the per-callable McCabe budget as one flat function. Split into one
small validator per field so each stays trivially under budget; the
orchestrating ``_record_from_row`` is now a straight-line sequence of calls
with no branching of its own. Persisted auth state is untrusted at this
boundary — every validator raises the exact same ``HostTokenError`` message
the monolithic function used to, so callers (``verify``, listing) keep
treating any corruption as an ordinary invalid credential.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

from disco.core.host_services import valid_host_service_name

from ._model import (
    _LEGACY_TOKEN_VERSION,
    _SELECTOR_RE,
    _TOKEN_KINDS,
    _TOKEN_VERSION,
    HostTokenError,
    HostTokenRecord,
    TokenKind,
)
from ._origins import _canonical_origins


def _validate_selector(row: dict[str, Any], expected_selector: str | None) -> str:
    selector = row["selector"]
    if (
        not isinstance(selector, str)
        or _SELECTOR_RE.fullmatch(selector) is None
        or (expected_selector is not None and selector != expected_selector)
    ):
        raise HostTokenError("invalid selector in host token store")
    return selector


def _validate_identity(row: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for field in ("conversation_id", "owner_id", "audience"):
        value = row[field]
        if not isinstance(value, str) or not value:
            raise HostTokenError("invalid credential identity in host token store")
        identity[field] = value
    return identity


def _validate_kind(row: dict[str, Any]) -> str:
    kind = str(row["kind"])
    if kind not in _TOKEN_KINDS:
        raise HostTokenError("invalid credential kind in host token store")
    return kind


def _validate_version(row: dict[str, Any]) -> int:
    version_value = row["version"]
    if not isinstance(version_value, int) or isinstance(version_value, bool):
        raise HostTokenError("invalid credential version in host token store")
    version = version_value
    if version not in {_LEGACY_TOKEN_VERSION, _TOKEN_VERSION}:
        raise HostTokenError("invalid credential version in host token store")
    return version


def _validate_generation(row: dict[str, Any]) -> int:
    generation_value = row["generation"]
    if (
        not isinstance(generation_value, int)
        or isinstance(generation_value, bool)
        or generation_value < 0
    ):
        raise HostTokenError("invalid credential generation in host token store")
    return generation_value


def _validate_services(row: dict[str, Any]) -> frozenset[str]:
    services_value = json.loads(row["allowed_services"])
    if (
        not isinstance(services_value, list)
        or not services_value
        or any(not isinstance(value, str) for value in services_value)
    ):
        raise HostTokenError("invalid service scope in host token store")
    services = frozenset(services_value)
    if len(services) != len(services_value) or any(
        not valid_host_service_name(service) for service in services
    ):
        raise HostTokenError("invalid service scope in host token store")
    return services


def _validate_origins(row: dict[str, Any]) -> frozenset[str]:
    origins_value = json.loads(row["allowed_origins"])
    if not isinstance(origins_value, list) or any(
        not isinstance(value, str) for value in origins_value
    ):
        raise HostTokenError("invalid origin scope in host token store")
    origins = frozenset(origins_value)
    try:
        canonical_origins = _canonical_origins(origins)
    except ValueError as exc:
        raise HostTokenError("invalid origin scope in host token store") from exc
    if len(origins) != len(origins_value) or canonical_origins != origins:
        raise HostTokenError("invalid origin scope in host token store")
    return origins


def _stored_datetime(value: object, *, required: bool) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise HostTokenError("invalid credential timestamp in host token store")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HostTokenError("credential timestamp must be timezone-aware")
    return parsed


def _record_from_row(
    row: dict[str, Any], *, expected_selector: str | None = None
) -> HostTokenRecord:
    selector = _validate_selector(row, expected_selector)
    identity = _validate_identity(row)
    kind = _validate_kind(row)
    version = _validate_version(row)
    generation = _validate_generation(row)
    services = _validate_services(row)
    origins = _validate_origins(row)
    created_at = _stored_datetime(row["created_at"], required=True)
    expires_at = _stored_datetime(row["expires_at"], required=False)
    revoked_at = _stored_datetime(row["revoked_at"], required=False)
    return HostTokenRecord(
        selector=selector,
        version=version,
        conversation_id=identity["conversation_id"],
        owner_id=identity["owner_id"],
        audience=identity["audience"],
        allowed_services=services,
        allowed_origins=origins,
        kind=cast(TokenKind, kind),
        generation=generation,
        created_at=cast(datetime, created_at),
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
