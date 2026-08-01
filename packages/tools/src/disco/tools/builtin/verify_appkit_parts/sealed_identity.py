"""`_sealed_appkit_output_identity` — the sha256-tree-manifest digest over the
bounded ``dist`` closure produced by the strict AppKit verifier.

BINDING BYTE-IDENTITY BAR: this digest ships to real users and MUST be
bit-for-bit identical for identical inputs. The guard clauses (per-entry
classification, per-file/total-size budget enforcement, manifest-entry
construction) are split into small named helpers purely to bring the parent
function's cyclomatic complexity down — the traversal order (a LIFO stack via
``pending.pop()``, ``sorted()`` directory entries, one entry processed at a
time), the manifest's field order/content, and the final
``json.dumps(..., sort_keys=True)`` encoding are byte-for-byte unchanged from
the pre-split version. Do not reorder the traversal or change what lands in
``manifest``.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from typing import TYPE_CHECKING, Any

from ...appkit_scope import APPKIT_CANONICAL_ENTRY_RELPATH
from .constants import (
    _SEALED_OUTPUT_MAX_FILE_BYTES,
    _SEALED_OUTPUT_MAX_FILES,
    _SEALED_OUTPUT_MAX_TOTAL_BYTES,
)

if TYPE_CHECKING:
    from ...anatomy import ToolContext


async def _classify_seal_child(sandbox: Any, child: str) -> tuple[str, str]:
    """Classify one directory child as a regular file or a subdirectory to
    descend into. Returns ``(kind, evidence)``: ``kind`` is one of ``"file"``,
    ``"dir"``, or ``"error"`` (``evidence`` only holds the failure reason when
    ``kind == "error"``)."""
    try:
        regular_file = bool(await sandbox.file_exists(child))
    except Exception as exc:  # noqa: BLE001 — an unclassifiable entry is unsealable
        return "error", f"cannot classify verifier output {child!r}: {exc}"
    if regular_file:
        return "file", ""
    try:
        await sandbox.list_dir(child)
    except Exception as exc:  # noqa: BLE001 — symlinks/special entries fail closed
        return "error", f"verifier output contains an unsupported entry {child!r}: {exc}"
    return "dir", ""


async def _seal_regular_file_entry(
    sandbox: Any, child: str, total_bytes: int
) -> tuple[dict[str, object] | None, int, str]:
    """Bound + hash one regular verifier-output file against the per-file and
    running total-bytes budgets. Returns ``(manifest entry, its size, "")`` on
    success, or ``(None, 0, evidence)`` when a guard trips."""
    size_probe = await sandbox.exec_shell(f"wc -c < {shlex.quote(child)}", timeout_s=10)
    try:
        size = int(str(getattr(size_probe, "stdout", "") or "").strip())
    except ValueError:
        return None, 0, f"cannot bound verifier output file {child!r}"
    if (
        size < 0
        or size > _SEALED_OUTPUT_MAX_FILE_BYTES
        or total_bytes + size > _SEALED_OUTPUT_MAX_TOTAL_BYTES
    ):
        return None, 0, f"verifier output exceeds the bounded seal budget at {child!r}"
    try:
        data = await sandbox.read_file(child)
    except Exception as exc:  # noqa: BLE001 — incomplete output fails closed
        return None, 0, f"cannot read verifier output file {child!r}: {exc}"
    if len(data) != size:
        return None, 0, f"verifier output changed while sealing {child!r}"
    return {"path": child, "size": size, "sha256": hashlib.sha256(data).hexdigest()}, size, ""


async def _sealed_appkit_output_identity(
    ctx: ToolContext,
) -> tuple[dict[str, str] | None, str]:
    """Hash the exact bounded ``dist`` closure produced by the strict verifier."""

    assert ctx.sandbox is not None
    sandbox = ctx.sandbox
    pending = ["dist"]
    manifest: list[dict[str, object]] = []
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(await sandbox.list_dir(directory))
        except Exception as exc:  # noqa: BLE001 — an unreadable output is unsealable
            return None, f"cannot enumerate verifier output {directory!r}: {exc}"
        for name in entries:
            child = f"{directory}/{name}"
            kind, evidence = await _classify_seal_child(sandbox, child)
            if kind == "error":
                return None, evidence
            if kind == "dir":
                pending.append(child)
                continue
            entry, size, evidence = await _seal_regular_file_entry(sandbox, child, total_bytes)
            if entry is None:
                return None, evidence
            total_bytes += size
            manifest.append(entry)
            if len(manifest) > _SEALED_OUTPUT_MAX_FILES:
                return None, "verifier output exceeds the bounded file-count seal budget"
    if not manifest or not any(item["path"] == APPKIT_CANONICAL_ENTRY_RELPATH for item in manifest):
        return None, "verifier output has no canonical AppKit entry"
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return (
        {
            "scheme": "sha256-tree-manifest-v1",
            "digest": f"sha256:{digest}",
            "entry_reference": APPKIT_CANONICAL_ENTRY_RELPATH,
            "producer_id": "disco.appkit_strict_verifier@1",
        },
        f"sealed {len(manifest)} files ({total_bytes} bytes) as sha256:{digest}",
    )
