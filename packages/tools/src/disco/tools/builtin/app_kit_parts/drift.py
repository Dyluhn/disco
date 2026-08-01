"""Generated-tree drift computation for `app_snapshot_version`.

Compares the spec-regenerated tree against the current on-disk bytes, so a
snapshot can report whether the workspace is still an exact projection of its
specs.
"""

from __future__ import annotations

from typing import Any

from ...anatomy import ToolContext


async def _read_disk_text(ctx: ToolContext, relpath: str) -> str | None:
    """Read a generated file's CURRENT on-disk bytes for drift comparison. Absent /
    unreadable → None (treated as a missing file)."""
    assert ctx.sandbox is not None
    try:
        if not await ctx.sandbox.file_exists(relpath):
            return None
        return (await ctx.sandbox.read_file(relpath)).decode("utf-8")
    except Exception:  # noqa: BLE001 — unreadable / vanished → treat as missing
        return None


def _compute_drift(tree: dict[str, str], on_disk: dict[str, str | None]) -> dict[str, Any]:
    """Compare the spec-regenerated tree against what's actually on disk.

    `modified` = a generated file whose on-disk bytes differ; `missing` = a
    generated file absent from disk. `clean` iff neither — i.e. the on-disk tree
    is exactly the projection of the current specs (no hand-edits / no staleness)."""
    modified: list[str] = []
    missing: list[str] = []
    for path in sorted(tree):
        actual = on_disk.get(path)
        if actual is None:
            missing.append(path)
        elif actual != tree[path]:
            modified.append(path)
    return {
        "clean": not modified and not missing,
        "modified": modified,
        "missing": missing,
    }
