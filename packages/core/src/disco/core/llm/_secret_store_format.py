"""Versioned encrypted-secret document parsing and construction.

This module owns the on-disk shape only.  Key selection, encryption and the
rotation workflow remain separate so malformed documents can be rejected
without coupling format validation to runtime key material.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

STORE_FORMAT = "disco.secret-store"
STORE_VERSION = 2
MAX_OLD_READ_KEYS = 4


class SecretStoreFormatError(RuntimeError):
    """The encrypted store is malformed or uses an unsupported format."""


class UnknownSecretKeyError(RuntimeError):
    """A versioned record names a key that is absent from the bounded keyring."""


class SecretDecryptionError(RuntimeError):
    """A record names an available key but its ciphertext does not authenticate."""


def validate_key_id(key_id: str) -> None:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if not 1 <= len(key_id) <= 128 or any(char not in allowed for char in key_id):
        raise ValueError("secret key IDs must contain 1-128 ASCII letters, digits, '.', '_' or '-'")


def derived_key_id(app_secret: str) -> str:
    """Stable compatibility ID for deployments that have not named their key yet."""
    digest = hashlib.sha256(app_secret.encode()).hexdigest()
    return f"key-{digest[:32]}"


def is_derived_key_id(key_id: str) -> bool:
    digest = key_id.removeprefix("key-")
    return (
        key_id.startswith("key-")
        and len(digest) == 32
        and all(char in "0123456789abcdef" for char in digest)
    )


def _empty_document() -> dict[str, Any]:
    return {
        "legacy": True,
        "active_key_id": None,
        "records": {},
        "rotation": None,
    }


def _legacy_document(raw: Mapping[Any, Any]) -> dict[str, Any]:
    for name, token in raw.items():
        if not isinstance(name, str) or token is not None and not isinstance(token, str):
            raise SecretStoreFormatError("legacy encrypted secret store records must be strings")
    return {
        "legacy": True,
        "active_key_id": None,
        "records": dict(raw),
        "rotation": None,
    }


def validate_versioned_records(records: Mapping[Any, Any]) -> dict[str, dict[str, str]]:
    checked: dict[str, dict[str, str]] = {}
    for name, record in records.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise SecretStoreFormatError("versioned secret records must be named objects")
        if set(record) != {"key_id", "ciphertext"}:
            raise SecretStoreFormatError(f"versioned secret record {name!r} has an invalid shape")
        key_id = record.get("key_id")
        ciphertext = record.get("ciphertext")
        if not isinstance(key_id, str) or not isinstance(ciphertext, str) or not ciphertext:
            raise SecretStoreFormatError(
                f"versioned secret record {name!r} has invalid key metadata"
            )
        try:
            validate_key_id(key_id)
        except ValueError as exc:
            raise SecretStoreFormatError(
                f"versioned secret record {name!r} has an invalid key ID"
            ) from exc
        checked[name] = {"key_id": key_id, "ciphertext": ciphertext}
    return checked


def _stored_key_id(value: Any, *, missing: str, invalid: str) -> str:
    if not isinstance(value, str):
        raise SecretStoreFormatError(missing)
    try:
        validate_key_id(value)
    except ValueError as exc:
        raise SecretStoreFormatError(invalid) from exc
    return value


def _rotation_source_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise SecretStoreFormatError("secret rotation source key IDs are invalid")
    if any(not isinstance(item, str) for item in value):
        raise SecretStoreFormatError("secret rotation source key IDs are invalid")
    source_ids = list(value)
    if len(set(source_ids)) != len(source_ids) or len(source_ids) > MAX_OLD_READ_KEYS:
        raise SecretStoreFormatError("secret rotation source key IDs are invalid")
    for item in source_ids:
        try:
            validate_key_id(item)
        except ValueError as exc:
            raise SecretStoreFormatError("secret rotation source key ID is invalid") from exc
    return source_ids


def _rotation_document(raw: Any, active_key_id: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SecretStoreFormatError("secret rotation metadata must be an object or null")
    if set(raw) != {
        "from_active_key_id",
        "from_key_ids",
        "to_key_id",
        "rollback_records",
    }:
        raise SecretStoreFormatError("secret rotation metadata has an invalid shape")
    to_key_id = _stored_key_id(
        raw.get("to_key_id"),
        missing="secret rotation metadata lacks to_key_id",
        invalid="secret rotation target key ID is invalid",
    )
    if to_key_id != active_key_id:
        raise SecretStoreFormatError("secret rotation metadata lacks to_key_id")
    from_active_key_id = _stored_key_id(
        raw.get("from_active_key_id"),
        missing="secret rotation metadata lacks from_active_key_id",
        invalid="secret rotation rollback active key ID is invalid",
    )
    from_key_ids = _rotation_source_ids(raw.get("from_key_ids"))
    rollback_raw = raw.get("rollback_records")
    if not isinstance(rollback_raw, dict):
        raise SecretStoreFormatError("secret rotation metadata lacks rollback_records")
    rollback_records = validate_versioned_records(rollback_raw)
    if not rollback_records:
        raise SecretStoreFormatError("pending secret rotation has no rollback records")
    expected_source_ids = sorted(
        {
            record["key_id"]
            for record in rollback_records.values()
            if record["key_id"] != to_key_id
        }
    )
    if from_key_ids != expected_source_ids:
        raise SecretStoreFormatError(
            "secret rotation source key IDs do not match rollback records"
        )
    return {
        "from_active_key_id": from_active_key_id,
        "from_key_ids": from_key_ids,
        "to_key_id": to_key_id,
        "rollback_records": rollback_records,
    }


def read_secret_document(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_document()
    except OSError as exc:
        raise SecretStoreFormatError(f"cannot read encrypted secret store: {exc}") from exc
    except ValueError as exc:
        raise SecretStoreFormatError("encrypted secret store is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise SecretStoreFormatError("encrypted secret store must be a JSON object")
    if raw.get("$format") != STORE_FORMAT:
        return _legacy_document(raw)
    version = raw.get("version")
    if isinstance(version, bool) or version != STORE_VERSION:
        raise SecretStoreFormatError(f"unsupported encrypted secret store version {version!r}")
    records = raw.get("records")
    if not isinstance(records, dict):
        raise SecretStoreFormatError("versioned encrypted secret store lacks records")
    active_key_id = _stored_key_id(
        raw.get("active_key_id"),
        missing="versioned encrypted secret store lacks active_key_id",
        invalid="stored active secret key ID is invalid",
    )
    return {
        "legacy": False,
        "active_key_id": active_key_id,
        "records": validate_versioned_records(records),
        "rotation": _rotation_document(raw.get("rotation"), active_key_id),
    }


def build_v2_document(
    records: Mapping[str, Any],
    *,
    rotation: Mapping[str, Any] | None,
    active_key_id: str,
) -> dict[str, Any]:
    return {
        "$format": STORE_FORMAT,
        "version": STORE_VERSION,
        "active_key_id": active_key_id,
        "records": dict(records),
        "rotation": dict(rotation) if rotation is not None else None,
    }
