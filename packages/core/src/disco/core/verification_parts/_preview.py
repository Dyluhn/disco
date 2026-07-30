"""Preview selection identity parsing — private helper for verification.

The structured-preview parsing logic that builds a
:class:`~disco.core.verification.PreviewSelectionIdentity` from a trusted
observation payload.  Extracted from ``verification.py`` to reduce the
``from_structured`` classmethod complexity; the public classmethod delegates
here unchanged.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from typing import Any, cast


def _structured_fields_valid(
    command: Any,
    name: Any,
    port: Any,
    projection_id: Any,
    sandbox_instance_id: Any,
    sandbox_generation: Any,
    launch_kind: Any,
    intent: dict[str, Any],
) -> bool:
    """True iff the structured payload fields are the right types."""

    return (
        isinstance(command, str)
        and isinstance(name, str)
        and type(port) is int
        and isinstance(projection_id, str)
        and isinstance(sandbox_instance_id, str)
        and type(sandbox_generation) is int
        and launch_kind in {"static", "custom", "framework"}
        and intent.get("launch_kind") == launch_kind
    )


def _structured_preview_identity(
    *,
    action_id: str,
    action_seq: int,
    observation_id: str,
    observation_seq: int,
    structured: dict[str, Any],
) -> tuple[
    str,
    str,
    int,
    str,
    str,
    str,
    str,
    int,
    str | None,
    str,
    int,
    str,
    int,
] | None:
    """Extract the validated fields for a ``PreviewSelectionIdentity``.

    Returns the constructor kwargs or ``None`` when the structured payload is
    not a valid preview selection.
    """

    intent = structured.get("intent")
    if not isinstance(intent, dict):
        return None
    command = structured.get("command")
    name = structured.get("name")
    port = structured.get("port")
    projection_id = structured.get("projection_id")
    sandbox_instance_id = structured.get("sandbox_instance_id")
    sandbox_generation = structured.get("sandbox_generation")
    launch_kind = structured.get("launch_kind")
    if not _structured_fields_valid(
        command, name, port, projection_id, sandbox_instance_id, sandbox_generation,
        launch_kind, intent,
    ):
        return None
    exec_dir = structured.get("exec_dir")
    digest = hashlib.sha256(
        json.dumps(
            {
                "command": command,
                "exec_dir": exec_dir,
                "intent": intent,
                "name": name,
                "port": port,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if structured.get("intent_digest") != digest:
        return None
    static_serve_dir: str | None = None
    if launch_kind == "static":
        normalized_serve_dir = posixpath.normpath(str(intent.get("serve_dir") or "."))
        if normalized_serve_dir == "/workspace":
            static_serve_dir = "."
        elif normalized_serve_dir.startswith("/workspace/"):
            static_serve_dir = normalized_serve_dir.removeprefix("/workspace/")
        elif not normalized_serve_dir.startswith("/"):
            static_serve_dir = normalized_serve_dir
        else:
            return None
    return (
        cast(str, projection_id),
        cast(str, name),
        cast(int, port),
        str(structured.get("url") or ""),
        cast(str, launch_kind),
        digest,
        cast(str, sandbox_instance_id),
        cast(int, sandbox_generation),
        static_serve_dir,
        action_id,
        action_seq,
        observation_id,
        observation_seq,
    )
