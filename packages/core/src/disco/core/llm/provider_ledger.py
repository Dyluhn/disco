"""Best-effort, sanitized evidence for real outbound provider attempts.

The reliability harness uses ``DISCO_PROVIDER_LEDGER`` as an append-only JSONL
boundary.  Records deliberately describe only routing/attempt identity and a
fixed allowlist of aggregate request-shape counts; this module must never receive
or retain request URLs, headers, payloads, credentials, prompts, responses, or
exceptions.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import httpx

_LABEL_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_REQUEST_ID_RE = re.compile(r"req_[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class ProviderRequestShape:
    """Sanitized scalar accounting for one already-shaped provider payload.

    The provider adapter computes these counts and passes only this value object
    across the evidence boundary.  Prompt content, tool schemas, URLs, headers,
    credentials, and arbitrary metadata must never enter this module.
    """

    request_id: str | None
    driver_context_window: int | None
    model_repair_attempt: int
    stream: bool
    max_output_tokens: int | None
    canonical_payload_bytes: int
    messages_json_bytes: int
    tools_json_bytes: int
    message_count: int
    tool_count: int
    system_message_count: int
    user_message_count: int
    assistant_message_count: int
    tool_message_count: int
    image_count: int
    image_url_chars: int


def provider_ledger_enabled() -> bool:
    """Whether request evidence is configured, without exposing its path."""

    return bool(os.environ.get("DISCO_PROVIDER_LEDGER"))


def _bounded_label(value: str | None) -> str | None:
    """Keep only short, machine-authored labels suitable for retained evidence."""

    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if _LABEL_RE.fullmatch(candidate) else None


def _machine_request_id(value: str | None) -> str | None:
    return value if value is not None and _REQUEST_ID_RE.fullmatch(value) else None


def _exact_positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _exact_nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _request_shape_record(shape: ProviderRequestShape) -> dict[str, object]:
    """Copy only the fixed scalar allowlist, validating exact runtime types."""

    record: dict[str, object] = {
        "request_id": _machine_request_id(shape.request_id),
        "driver_context_window": _exact_positive_int(shape.driver_context_window),
        "stream": shape.stream if type(shape.stream) is bool else False,
    }
    positive = {
        "model_repair_attempt": shape.model_repair_attempt,
    }
    optional_positive = {
        "max_output_tokens": shape.max_output_tokens,
    }
    nonnegative = {
        "canonical_payload_bytes": shape.canonical_payload_bytes,
        "messages_json_bytes": shape.messages_json_bytes,
        "tools_json_bytes": shape.tools_json_bytes,
        "message_count": shape.message_count,
        "tool_count": shape.tool_count,
        "system_message_count": shape.system_message_count,
        "user_message_count": shape.user_message_count,
        "assistant_message_count": shape.assistant_message_count,
        "tool_message_count": shape.tool_message_count,
        "image_count": shape.image_count,
        "image_url_chars": shape.image_url_chars,
    }
    for key, value in positive.items():
        record[key] = _exact_positive_int(value)
    for key, value in optional_positive.items():
        record[key] = _exact_positive_int(value)
    for key, value in nonnegative.items():
        record[key] = _exact_nonnegative_int(value)
    return record


def emit_provider_attempt(
    *,
    base_url: str,
    model: str,
    has_tools: bool,
    conversation_id: str | None,
    purpose: str | None = None,
    call_kind: str | None = None,
    request_shape: ProviderRequestShape | None = None,
) -> None:
    """Append one sanitized provider-attempt record when the ledger is enabled.

    ``purpose`` and ``call_kind`` are optional, bounded, machine-authored labels.
    Invalid labels are omitted rather than copied into retained evidence.  Every
    failure is best-effort: observability must never alter the provider request.
    """

    path = os.environ.get("DISCO_PROVIDER_LEDGER")
    if not path:
        return
    try:
        record: dict[str, object] = {
            "ts": time.time(),
            "host": httpx.URL(base_url).host or "",
            "model": model,
            "has_tools": bool(has_tools),
            "conversation_id": (str(conversation_id).strip() if conversation_id else None),
        }
        bounded_purpose = _bounded_label(purpose)
        if bounded_purpose is not None:
            record["purpose"] = bounded_purpose
        bounded_call_kind = _bounded_label(call_kind)
        if bounded_call_kind is not None:
            record["call_kind"] = bounded_call_kind
        if request_shape is not None:
            record.update(_request_shape_record(request_shape))

        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
    except Exception:  # noqa: BLE001 - evidence must never break provider traffic
        return


__all__ = ["ProviderRequestShape", "emit_provider_attempt", "provider_ledger_enabled"]
