"""Deliverable byte reading for the dictated-content finish gate.

Owns the dual read strategy (sandbox read surface, else a host workspace
path) so `_ContentGateMixin` never reimplements it — the mixin method is a
thin, `self`-bound wrapper around `read_deliverable_bytes` below.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .errors import _DictatedContentInspectionIncomplete


async def read_deliverable_bytes(
    sbx: Any,
    path: str,
    *,
    strict: bool,
    max_file_bytes: int,
    log: logging.Logger,
) -> bytes | None:
    """Read one deliverable file, returning None only when no host/sandbox
    read surface is available. Missing/empty files return b"" so the content
    condition fails loudly against the named file."""

    if sbx is not None and hasattr(sbx, "read_file"):
        return await _read_via_sandbox(sbx, path, strict=strict, log=log)
    return await _read_via_workspace(
        sbx, path, strict=strict, max_file_bytes=max_file_bytes, log=log
    )


async def _read_via_sandbox(
    sbx: Any, path: str, *, strict: bool, log: logging.Logger
) -> bytes | None:
    try:
        data = await sbx.read_file(path)
    except FileNotFoundError:
        return b""
    except Exception as exc:  # noqa: BLE001 — becomes visible unverifiable evidence
        if not strict:
            log.warning("dictated-content read failed for %s: %s", path, exc)
            return b""
        raise _DictatedContentInspectionIncomplete(
            "a declared deliverable could not be read safely"
        ) from exc
    if isinstance(data, bytes):
        return data
    return str(data).encode("utf-8", "surrogatepass")


async def _read_via_workspace(
    sbx: Any,
    path: str,
    *,
    strict: bool,
    max_file_bytes: int,
    log: logging.Logger,
) -> bytes | None:
    workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
    if not workspace:
        return None
    try:
        return _read_workspace_file(
            Path(workspace), path, strict=strict, max_file_bytes=max_file_bytes, log=log
        )
    except OSError as exc:
        if not strict:
            log.warning("dictated-content workspace read failed for %s: %s", path, exc)
            return b""
        raise _DictatedContentInspectionIncomplete(
            "a declared deliverable could not be read from the workspace"
        ) from exc


def _read_workspace_file(
    root: Path,
    path: str,
    *,
    strict: bool,
    max_file_bytes: int,
    log: logging.Logger,
) -> bytes:
    resolved_root = root.resolve()
    candidate = (resolved_root / path).resolve()
    root_s = str(resolved_root)
    cand_s = str(candidate)
    if not (cand_s == root_s or cand_s.startswith(root_s.rstrip("/") + "/")):
        return b""
    if not candidate.is_file():
        return b""
    if candidate.stat().st_size > max_file_bytes:
        if not strict:
            log.warning("dictated-content file exceeds inspection budget: %s", path)
            return b""
        raise _DictatedContentInspectionIncomplete(
            "a text deliverable exceeds the per-file inspection budget"
        )
    return candidate.read_bytes()
