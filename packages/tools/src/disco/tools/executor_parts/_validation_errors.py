"""Serializable, value-free diagnostics for rejected tool arguments."""

from __future__ import annotations

from typing import Any, get_args

from pydantic import ValidationError
from pydantic_core.core_schema import ErrorType

_BUILTIN_ERROR_TYPES = frozenset(get_args(ErrorType))


def _validation_errors(error: ValidationError) -> list[dict[str, Any]]:
    """Keep locations and known constraint codes, never exception/input payloads.

    Even a nominally built-in error code can be emitted by a custom validator
    with a value-bearing message. Reconstruct all messages from static text;
    the accompanying expected schema and nested-shape hints supply repair detail.
    """
    errors: list[dict[str, Any]] = []
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        kind = item["type"] if item["type"] in _BUILTIN_ERROR_TYPES else "value_error"
        errors.append(
            {
                "type": kind,
                "loc": item["loc"],
                "msg": f"Schema constraint {kind!r} was not satisfied; check the expected schema",
            }
        )
    return errors
