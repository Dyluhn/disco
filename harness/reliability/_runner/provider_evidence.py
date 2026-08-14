"""Private provider-ledger and conversation-scope evidence validation."""

from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.reliability.state import FAIL, INVALID, PASS

_EXPECTED_PROVIDER_HOST_ENV = "DISCO_RELIABILITY_EXPECTED_PROVIDER_HOST"
_EXPECTED_PROVIDER_MODEL_ENV = "DISCO_RELIABILITY_EXPECTED_PROVIDER_MODEL"
_EXPECTED_VISION_PROVIDER_HOST_ENV = "DISCO_RELIABILITY_EXPECTED_VISION_PROVIDER_HOST"
_EXPECTED_VISION_MODEL_ENV = "DISCO_RELIABILITY_EXPECTED_VISION_MODEL"
_PROVIDER_CONVERSATION_MANIFEST_ENV = "DISCO_RELIABILITY_PROVIDER_CONVERSATION_MANIFEST"
_PROVIDER_LEDGER_REQUIRED_KEYS = {
    "ts",
    "host",
    "model",
    "has_tools",
    "conversation_id",
}
_PROVIDER_LEDGER_OPTIONAL_KEYS = {"purpose", "call_kind"}
_PROVIDER_LEDGER_REQUEST_SHAPE_KEYS = {
    "request_id",
    "driver_context_window",
    "model_repair_attempt",
    "stream",
    "max_output_tokens",
    "canonical_payload_bytes",
    "messages_json_bytes",
    "tools_json_bytes",
    "message_count",
    "tool_count",
    "system_message_count",
    "user_message_count",
    "assistant_message_count",
    "tool_message_count",
    "image_count",
    "image_url_chars",
}
_PROVIDER_LEDGER_LABEL_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_PROVIDER_LEDGER_REQUEST_ID_RE = re.compile(r"req_[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class _ProviderScope:
    conversations: frozenset[str]
    tool_conversations: frozenset[str]
    auxiliary_calls: int


def _provider_records(path: Path) -> tuple[list[dict[str, Any]] | None, str]:
    ledger_data, ledger_file_reason = _read_private_regular_evidence_file(path, "provider ledger")
    if ledger_file_reason is not None:
        return None, ledger_file_reason
    assert ledger_data is not None
    records: list[dict[str, Any]] = []
    try:
        for line_number, raw in enumerate(ledger_data.decode("utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return None, f"provider ledger line {line_number} is not an object"
            records.append(parsed)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"provider ledger is unreadable: {type(exc).__name__}"
    if not records:
        return None, "provider ledger contains no calls"
    return records, ""


def _provider_route(
    record: dict[str, Any],
    index: int,
    *,
    required_host: str,
    required_model: str,
    required_vision_host: str,
    required_vision_model: str,
) -> tuple[str, str, str, bool, str]:
    is_visual = record.get("purpose") == "visual_inspection"
    if not is_visual:
        if required_vision_host and record.get("image_count") != 0:
            return (
                INVALID,
                "",
                "",
                False,
                f"provider ledger record {index} does not identify its image-bearing route",
            )
        return PASS, required_host, required_model, False, ""
    bounded = (
        record.get("call_kind") == "observer"
        and record.get("has_tools") is False
        and record.get("stream") is False
        and record.get("image_count") == 1
        and record.get("tool_count") == 0
    )
    if not bounded:
        return (
            INVALID,
            "",
            "",
            True,
            f"provider ledger record {index} is not a bounded visual observer",
        )
    if not required_vision_host or not required_vision_model:
        return PASS, required_host, required_model, True, ""
    return PASS, required_vision_host, required_vision_model, True, ""


def _collect_provider_scope(
    records: list[dict[str, Any]],
    required_host: str,
    required_model: str,
    required_vision_host: str,
    required_vision_model: str,
) -> tuple[str, _ProviderScope | None, str]:
    conversations: set[str] = set()
    tool_conversations: set[str] = set()
    auxiliary_calls = 0
    for index, record in enumerate(records):
        status, normalized, reason = _provider_ledger_record_result(record, index)
        if status != PASS or normalized is None:
            return status, None, reason
        host, model, has_tools, conversation_id = normalized
        route_status, expected_host, expected_model, is_visual, route_reason = _provider_route(
            record,
            index,
            required_host=required_host,
            required_model=required_model,
            required_vision_host=required_vision_host,
            required_vision_model=required_vision_model,
        )
        if route_status != PASS:
            return route_status, None, route_reason
        if is_visual:
            auxiliary_calls += 1
        if host != expected_host:
            return FAIL, None, (f"provider fallback detected: host {host!r} != {expected_host!r}")
        if model != expected_model:
            return (
                FAIL,
                None,
                (f"provider fallback detected: model {model!r} != {expected_model!r}"),
            )
        if conversation_id is None:
            if has_tools:
                return (
                    INVALID,
                    None,
                    (
                        f"provider ledger record {index} has tools or unknown call type "
                        "but no conversation_id"
                    ),
                )
            if not is_visual:
                auxiliary_calls += 1
            continue
        conversations.add(conversation_id)
        if has_tools:
            tool_conversations.add(conversation_id)
    return (
        PASS,
        _ProviderScope(
            conversations=frozenset(conversations),
            tool_conversations=frozenset(tool_conversations),
            auxiliary_calls=auxiliary_calls,
        ),
        "",
    )


def _provider_scope_result(
    scope: _ProviderScope,
    *,
    manifest_conversations: frozenset[str] | None,
    expected_conversations: int | None,
    units: int,
    required_conversation_ids: frozenset[str] | None,
) -> tuple[str, str]:
    conversations = scope.conversations
    if manifest_conversations is not None and conversations != manifest_conversations:
        missing = len(manifest_conversations - conversations)
        extra = len(conversations - manifest_conversations)
        details: list[str] = []
        if missing:
            details.append(f"missing {missing} manifest conversation ID(s)")
        if extra:
            details.append(f"extra {extra} provider-ledger conversation ID(s)")
        return INVALID, "; ".join(details)
    without_tools = (
        len(manifest_conversations - scope.tool_conversations)
        if manifest_conversations is not None
        else 0
    )
    if without_tools:
        return INVALID, (
            f"{without_tools} manifest conversation ID(s) have no tool-bearing provider call"
        )
    if required_conversation_ids is not None:
        if len(required_conversation_ids) != units:
            return INVALID, (
                f"required PASS conversation scope has {len(required_conversation_ids)}/{units} IDs"
            )
        missing_required = required_conversation_ids - conversations
        if missing_required:
            return INVALID, (f"missing {len(missing_required)} required PASS conversation ID(s)")
        required_without_tools = required_conversation_ids - scope.tool_conversations
        if required_without_tools:
            return INVALID, (
                f"{len(required_without_tools)} required PASS conversation ID(s) have no "
                "tool-bearing provider call"
            )
    if expected_conversations is not None and len(conversations) != expected_conversations:
        return INVALID, (
            f"provider ledger covers {len(conversations)}/{expected_conversations} "
            "required successful-suite conversation(s)"
        )
    if len(conversations) < units:
        return INVALID, (
            f"provider ledger covers only {len(conversations)}/{units} required conversation(s)"
        )
    return PASS, ""


def _provider_evidence_result(
    path: Path,
    *,
    expected_host: str,
    expected_model: str,
    expected_vision_host: str = "",
    expected_vision_model: str = "",
    units: int,
    conversation_manifest_path: Path | None = None,
    expected_conversations: int | None = None,
    required_conversation_ids: frozenset[str] | None = None,
) -> tuple[str, int, str]:
    """Fail closed on absent, malformed, or fallback provider-call evidence."""

    manifest_conversations: frozenset[str] | None = None
    if conversation_manifest_path is not None:
        manifest_status, manifest_conversations, manifest_reason = (
            _provider_conversation_manifest_result(conversation_manifest_path)
        )
        if manifest_status != PASS:
            return manifest_status, 0, manifest_reason

    records, ledger_reason = _provider_records(path)
    if records is None:
        return INVALID, 0, ledger_reason
    required_host = expected_host.strip().lower()
    required_model = expected_model.strip()
    if not required_host or not required_model:
        return INVALID, 0, "expected provider host/model is empty"
    required_vision_host = expected_vision_host.strip().lower()
    required_vision_model = expected_vision_model.strip()
    if bool(required_vision_host) != bool(required_vision_model):
        return INVALID, 0, "expected visual provider host/model must be configured together"
    scope_status, scope, scope_reason = _collect_provider_scope(
        records,
        required_host,
        required_model,
        required_vision_host,
        required_vision_model,
    )
    if scope_status != PASS or scope is None:
        return scope_status, 0, scope_reason
    validation_status, validation_reason = _provider_scope_result(
        scope,
        manifest_conversations=manifest_conversations,
        expected_conversations=expected_conversations,
        units=units,
        required_conversation_ids=required_conversation_ids,
    )
    if validation_status != PASS:
        return validation_status, 0, validation_reason
    return (
        PASS,
        units,
        f"{len(records)} provider call(s) ({scope.auxiliary_calls} auxiliary) across "
        f"{len(scope.conversations)} conversation(s) "
        f"used {required_host}/{required_model}",
    )


def _provider_record_keys_reason(record: dict[str, Any], index: int) -> str:
    keys = set(record)
    missing = sorted(_PROVIDER_LEDGER_REQUIRED_KEYS - keys)
    unexpected = sorted(
        keys
        - _PROVIDER_LEDGER_REQUIRED_KEYS
        - _PROVIDER_LEDGER_OPTIONAL_KEYS
        - _PROVIDER_LEDGER_REQUEST_SHAPE_KEYS
    )
    if missing or unexpected:
        return (
            f"provider ledger record {index} has invalid fields "
            f"(missing={missing}, unexpected={unexpected})"
        )
    return ""


def _valid_provider_timestamp(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _valid_conversation_id(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(value) and value == value.strip())


def _provider_ledger_identity(
    record: dict[str, Any],
    index: int,
) -> tuple[tuple[str, str, bool, str | None] | None, str]:
    if reason := _provider_record_keys_reason(record, index):
        return None, reason
    timestamp = record["ts"]
    if not _valid_provider_timestamp(timestamp):
        return None, f"provider ledger record {index} has invalid ts"
    host = record["host"]
    if not isinstance(host, str) or not host or host != host.strip().lower():
        return None, f"provider ledger record {index} has invalid host"
    model = record["model"]
    if not isinstance(model, str) or not model or model != model.strip():
        return None, f"provider ledger record {index} has invalid model"
    has_tools = record["has_tools"]
    if not isinstance(has_tools, bool):
        return None, f"provider ledger record {index} has invalid has_tools"
    conversation_id = record["conversation_id"]
    if not _valid_conversation_id(conversation_id):
        return None, f"provider ledger record {index} has invalid conversation_id"
    for label in _PROVIDER_LEDGER_OPTIONAL_KEYS & set(record):
        value = record[label]
        if not isinstance(value, str) or _PROVIDER_LEDGER_LABEL_RE.fullmatch(value) is None:
            return None, f"provider ledger record {index} has invalid {label}"
    return (host, model, has_tools, conversation_id), ""


def _provider_request_count_reason(record: dict[str, Any], index: int) -> str:
    count_labels = _PROVIDER_LEDGER_REQUEST_SHAPE_KEYS - {
        "request_id",
        "driver_context_window",
        "model_repair_attempt",
        "stream",
        "max_output_tokens",
    }
    for label in count_labels:
        value = record[label]
        if type(value) is not int or value < 0:  # noqa: E721
            return f"provider ledger record {index} has invalid {label}"
    return ""


def _provider_request_identity_reason(record: dict[str, Any], index: int) -> str:
    request_id = record["request_id"]
    if request_id is not None and (
        not isinstance(request_id, str)
        or _PROVIDER_LEDGER_REQUEST_ID_RE.fullmatch(request_id) is None
    ):
        return f"provider ledger record {index} has invalid request_id"
    for label in ("driver_context_window", "max_output_tokens"):
        value = record[label]
        if value is not None and (type(value) is not int or value <= 0):  # noqa: E721
            return f"provider ledger record {index} has invalid {label}"
    if type(record["model_repair_attempt"]) is not int or record["model_repair_attempt"] <= 0:  # noqa: E721
        return f"provider ledger record {index} has invalid model_repair_attempt"
    if type(record["stream"]) is not bool:  # noqa: E721
        return f"provider ledger record {index} has invalid stream"
    return ""


def _provider_request_shape_reason(
    record: dict[str, Any],
    index: int,
    has_tools: bool,
) -> str:
    keys = set(record)
    shape_keys = keys & _PROVIDER_LEDGER_REQUEST_SHAPE_KEYS
    if shape_keys and shape_keys != _PROVIDER_LEDGER_REQUEST_SHAPE_KEYS:
        missing_shape = sorted(_PROVIDER_LEDGER_REQUEST_SHAPE_KEYS - shape_keys)
        return (
            f"provider ledger record {index} has incomplete request shape (missing={missing_shape})"
        )
    if not shape_keys:
        return ""
    if reason := _provider_request_identity_reason(record, index):
        return reason
    if reason := _provider_request_count_reason(record, index):
        return reason
    if record["canonical_payload_bytes"] < (
        record["messages_json_bytes"] + record["tools_json_bytes"]
    ):
        return f"provider ledger record {index} has inconsistent request byte counts"
    if has_tools != (record["tool_count"] > 0):
        return f"provider ledger record {index} has inconsistent tool count"
    return ""


def _provider_ledger_record_result(
    record: dict[str, Any], index: int
) -> tuple[str, tuple[str, str, bool, str | None] | None, str]:
    """Validate one sanitized provider record without coercing evidence types."""

    identity, reason = _provider_ledger_identity(record, index)
    if identity is None:
        return INVALID, None, reason
    shape_reason = _provider_request_shape_reason(record, index, identity[2])
    if shape_reason:
        return INVALID, None, shape_reason
    return PASS, identity, ""


def _provider_manifest_payload(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_absolute():
        return None, "provider conversation manifest path is not absolute"
    manifest_data, manifest_file_reason = _read_private_regular_evidence_file(
        path, "provider conversation manifest"
    )
    if manifest_file_reason is not None:
        return None, manifest_file_reason
    assert manifest_data is not None
    try:
        manifest = json.loads(manifest_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, (f"provider conversation manifest is unreadable: {type(exc).__name__}")
    if not isinstance(manifest, dict):
        return None, "provider conversation manifest must be an object"
    expected_keys = {"schema_version", "conversation_ids"}
    if set(manifest) != expected_keys:
        return None, (
            "provider conversation manifest must contain exactly "
            "schema_version and conversation_ids"
        )
    if manifest.get("schema_version") != 1 or isinstance(manifest.get("schema_version"), bool):
        return None, "provider conversation manifest schema_version must be 1"
    return manifest, ""


def _provider_manifest_conversation_ids(
    raw_ids: Any,
) -> tuple[frozenset[str] | None, str]:
    if not isinstance(raw_ids, list) or not raw_ids:
        return (
            None,
            "provider conversation manifest conversation_ids must be a nonempty list",
        )
    conversation_ids: list[str] = []
    for index, value in enumerate(raw_ids):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return None, (
                f"provider conversation manifest conversation_ids[{index}] "
                "must be an exact nonempty string"
            )
        conversation_ids.append(value)
    if len(set(conversation_ids)) != len(conversation_ids):
        return None, "provider conversation manifest contains duplicate IDs"
    return frozenset(conversation_ids), ""


def _provider_conversation_manifest_result(
    path: Path,
) -> tuple[str, frozenset[str], str]:
    """Parse the runner-owned exact provider conversation scope manifest."""

    empty: frozenset[str] = frozenset()
    manifest, reason = _provider_manifest_payload(path)
    if manifest is None:
        return INVALID, empty, reason
    parsed, reason = _provider_manifest_conversation_ids(manifest.get("conversation_ids"))
    if parsed is None:
        return INVALID, empty, reason
    return PASS, parsed, f"provider conversation manifest declares {len(parsed)} ID(s)"


def _read_private_regular_evidence_file(
    path: Path,
    label: str,
) -> tuple[bytes | None, str | None]:
    """Open once with no-follow, validate that descriptor, and read from it."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except FileNotFoundError:
        return None, f"{label} was not produced"
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO}:
            return None, f"{label} must be a regular non-symlink file"
        return None, f"{label} metadata is unreadable: {type(exc).__name__}"
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, f"{label} must be a regular non-symlink file"
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            return None, f"{label} permissions must be 0600"
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), None
    except OSError as exc:
        return None, f"{label} is unreadable: {type(exc).__name__}"
    finally:
        if descriptor is not None:
            os.close(descriptor)
