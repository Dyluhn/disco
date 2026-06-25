"""PR I3 — the sanitized Pi session artifact writer.

The Pi sidecar's raw outbound frame stream (`ready` / `agent_event` / `error` /
`exit`) is useful DEBUG EVIDENCE — what the inner Pi loop actually did — but it is
NOT product truth (the truth is the Disco event store the bridge + the I1 mapper
append into). This module persists that frame stream under a per-conversation
evidence folder, as a sidecar to the event DB, with EVERY secret stripped first.

Two redaction layers, defense-in-depth (mirrors the gateway's egress redaction):

  1. Structural — :func:`disco.core.evidence.schema.redact` deep-traverses each
     frame and blanks any value under a secret-named key (``api_key``/``token``/
     ``authorization``/…) AND scrubs ``sk-``/``Bearer``-shaped substrings.
  2. Literal — the ephemeral run token (and any provider key) is a bare
     ``token_urlsafe`` value with NO marker prefix, so the structural pass can
     miss it. We additionally string-replace every (encoded-aware) form of each
     known secret via the gateway's own ``_redact_text`` needle set.

The file is JSONL (one frame per line) so a long session streams without holding
the whole thing in memory at read time. The artifact is best-effort: a write
failure never propagates (it must not break a build's teardown).

TODO(evidence-dir): the canonical evidence surface today is the on-demand
``/api/debug/evidence/{cid}`` bundle + the verify-harness dossier — neither is a
durable per-conversation directory. This writer uses the same DB-sidecar
convention the runtime already uses for uploads (``{DISCO_DB}.pi_sessions/``);
when a first-class per-conversation evidence directory lands, point ``base_dir``
at it (the redaction contract here is unchanged).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from disco.core.evidence.schema import redact

from ..routes.pi_inference import _redact_text

__all__ = ["sanitize_frame", "write_pi_session_artifact"]

_LOG = logging.getLogger("disco.pi_session_artifact")


def sanitize_frame(frame: Mapping[str, Any], redact_secrets: Iterable[str]) -> Any:
    """Return a deep-copied, fully-sanitized view of one sidecar frame.

    Structural redaction first (secret-named keys + ``sk-``/``Bearer`` patterns),
    then a literal pass that strips every encoded form of each known secret string
    (the ephemeral run token / provider key) — caught even when it appears as a
    bare value the structural scrubber can't recognize."""
    cleaned = redact(frame)
    secrets = [s for s in redact_secrets if s]
    if not secrets:
        return cleaned
    # Re-serialize, string-strip each secret's encoded forms, re-parse. Bounded by
    # the frame size; a frame is already JSON (it came off the wire as JSON).
    try:
        text = json.dumps(cleaned, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(cleaned)
    for secret in secrets:
        text = _redact_text(text, secret)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # A secret straddled a JSON delimiter and the re-parse failed — fall back
        # to the redacted STRING (still secret-free), never the raw object.
        return text


def write_pi_session_artifact(
    *,
    base_dir: str | Path,
    conversation_id: str,
    kernel_id: str,
    frames: Iterable[Mapping[str, Any]],
    redact_secrets: Iterable[str] = (),
) -> Path | None:
    """Write the sanitized Pi frame stream for one run to a JSONL evidence file.

    Returns the path written, or None on any failure (best-effort — an evidence
    write must never break a build's finish/cancel teardown). Each line is one
    sanitized frame; a small header line records the conversation/kernel/time so a
    reader can attribute the file without trusting the path. NO secret (run token
    or provider key) ever reaches disk — both redaction layers run per frame."""
    try:
        secrets = list(redact_secrets)
        out_dir = Path(base_dir) / conversation_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"pi-session-{kernel_id}.jsonl"
        lines: list[str] = [
            json.dumps(
                {
                    "_artifact": "pi_session",
                    "conversation_id": conversation_id,
                    "kernel_id": kernel_id,
                    "written_at": time.time(),
                    "note": "sanitized Pi sidecar frame stream — DEBUG EVIDENCE, not product truth",
                },
                ensure_ascii=False,
            )
        ]
        for frame in frames:
            sanitized = sanitize_frame(frame, secrets)
            lines.append(json.dumps(sanitized, default=str, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 — evidence is best-effort, never crash teardown
        _LOG.warning("could not write Pi session artifact for %s", conversation_id, exc_info=True)
        return None
