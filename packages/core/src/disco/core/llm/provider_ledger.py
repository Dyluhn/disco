"""Best-effort, sanitized evidence for real outbound provider attempts.

The reliability harness uses ``DISCO_PROVIDER_LEDGER`` as an append-only JSONL
boundary.  Records deliberately describe only routing and attempt identity; this
module must never receive or retain request URLs, headers, payloads, credentials,
prompts, responses, or exceptions.
"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

_LABEL_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")


def _bounded_label(value: str | None) -> str | None:
    """Keep only short, machine-authored labels suitable for retained evidence."""

    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if _LABEL_RE.fullmatch(candidate) else None


def emit_provider_attempt(
    *,
    base_url: str,
    model: str,
    has_tools: bool,
    conversation_id: str | None,
    purpose: str | None = None,
    call_kind: str | None = None,
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

        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
    except Exception:  # noqa: BLE001 - evidence must never break provider traffic
        return


__all__ = ["emit_provider_attempt"]
