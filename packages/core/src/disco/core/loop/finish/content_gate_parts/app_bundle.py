"""Bounded app-bundle path enumeration for the dictated-content finish gate.

A multi-file app's explicit handoff names its entry file, not every external
CSS/JS/SVG/service-worker sibling. `bounded_app_bundle_paths` boundedly
enumerates text candidates belonging to handed-off app roots: it treats the
entry's directory as the bundle root while excluding dependency, VCS,
runtime-state, hidden, and known-binary paths. The hard limits below keep a
large project from turning a finish check into an unbounded workspace crawl.

Owns every constant and helper this traversal needs; `content_gates.py`
re-imports the limit constants it must keep exposing (see module docstring
there) and passes the shared `_DICTATED_CONTENT_BINARY_SUFFIXES` set in
explicitly, since that set is also used outside the bundle walk.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from typing import Any

from .errors import _DictatedContentInspectionIncomplete

_DICTATED_CONTENT_BUNDLE_SKIP_DIRS = frozenset(
    {
        ".disco",
        ".git",
        ".pmx",
        ".venv",
        "__pycache__",
        # Node's module compile cache (the JS analogue of __pycache__): the
        # sandbox homes node processes in the workspace, so node deposits a
        # non-hidden `node-compile-cache/v<ver>-<arch>-<hash>/` tree with
        # hundreds of flat entries that grows with every node execution. It is
        # runtime state, never app content; left in the walk it nondeterministically
        # trips the per-directory inspection bound whenever the handed-off entry
        # lives at the workspace root (dev-mode serving) rather than in dist/
        # (counted seed 440023).
        "node-compile-cache",
        "node_modules",
        "venv",
        "vendor",
    }
)
_DICTATED_CONTENT_BUNDLE_MAX_DIRS = 64
_DICTATED_CONTENT_BUNDLE_MAX_FILES = 512
_DICTATED_CONTENT_BUNDLE_MAX_DEPTH = 8
_DICTATED_CONTENT_BUNDLE_MAX_ENTRIES_PER_DIR = 256
_DICTATED_CONTENT_BUNDLE_MAX_ENTRIES = 1024


async def bounded_app_bundle_paths(
    sbx: Any,
    roots: list[str],
    *,
    binary_suffixes: frozenset[str],
    safe_path: Callable[[str], str | None],
) -> list[str]:
    """Boundedly enumerate text candidates belonging to `roots`.

    Raises `_DictatedContentInspectionIncomplete` (fail-closed) the moment any
    bound is exceeded or the sandbox lacks the bounded inspection APIs.
    """

    _require_bundle_inspection_apis(sbx)
    await _verify_bundle_roots(sbx, roots)
    return await _walk_bundle_tree(sbx, roots, binary_suffixes=binary_suffixes, safe_path=safe_path)


async def resolve_sandboxed_app_entry(
    sbx: Any, selected_entry: str, legacy_index: str | None
) -> str:
    """Confirm `selected_entry` (or the legacy directory index) exists as a file.

    `serve` records an explicit entry FILE path. Older persisted events and
    direct integrations may still name an app directory, so this keeps a
    fail-closed compatibility bridge: it only accepts the legacy index when
    the sandbox positively proves that file exists. Without sandbox evidence
    (no `file_exists` API) the entry is trusted as-is.
    """

    if sbx is None or not hasattr(sbx, "file_exists"):
        return selected_entry
    try:
        if await sbx.file_exists(selected_entry):
            return selected_entry
        if (
            legacy_index is not None
            and legacy_index != selected_entry
            and await sbx.file_exists(legacy_index)
        ):
            return legacy_index
    except Exception as exc:  # noqa: BLE001 — strict selected-app evidence boundary
        raise _DictatedContentInspectionIncomplete(
            "the selected app entry could not be verified as a regular file"
        ) from exc
    raise _DictatedContentInspectionIncomplete(
        "the selected app entry does not exist as a regular file"
    )


def _require_bundle_inspection_apis(sbx: Any) -> None:
    required = ("list_dir_bounded", "resolve_relpath")
    if sbx is None or any(not hasattr(sbx, method) for method in required):
        raise _DictatedContentInspectionIncomplete(
            "the sandbox lacks bounded app-bundle inspection APIs"
        )


async def _verify_bundle_roots(sbx: Any, roots: list[str]) -> None:
    for root in roots:
        try:
            resolved_root = posixpath.normpath(str(await sbx.resolve_relpath(root)))
        except Exception as exc:  # noqa: BLE001 — becomes visible unverifiable evidence
            raise _DictatedContentInspectionIncomplete(
                "the selected app root could not be resolved safely"
            ) from exc
        if resolved_root != posixpath.normpath(root):
            raise _DictatedContentInspectionIncomplete(
                "the selected app root resolves through an alias"
            )


async def _list_bundle_dir(sbx: Any, directory: str, max_entries_per_dir: int) -> list[Any]:
    try:
        entries, truncated = await sbx.list_dir_bounded(directory, max_entries_per_dir)
    except Exception as exc:  # noqa: BLE001 — becomes visible unverifiable evidence
        raise _DictatedContentInspectionIncomplete(
            "an app-bundle directory could not be listed safely"
        ) from exc
    if truncated:
        raise _DictatedContentInspectionIncomplete(
            "an app-bundle directory exceeds the entry inspection limit"
        )
    return entries


def _is_ignorable_bundle_name(name: str) -> bool:
    if not name or name in {".", ".."} or name in _DICTATED_CONTENT_BUNDLE_SKIP_DIRS:
        return True
    if name.startswith(".") or "/" in name or "\x00" in name:
        return True
    return any(ord(character) < 32 or ord(character) == 127 for character in name)


def _record_bundle_entry(
    directory: str,
    name: str,
    entry_kind: str,
    depth: int,
    *,
    binary_suffixes: frozenset[str],
    safe_path: Callable[[str], str | None],
    files: list[str],
    stack: list[tuple[str, int]],
) -> None:
    if _is_ignorable_bundle_name(name):
        return
    child = posixpath.normpath(posixpath.join(directory, name))
    safe = safe_path(child)
    if safe is None:
        return
    if entry_kind == "other":
        return
    if entry_kind == "file":
        if posixpath.splitext(safe)[1].lower() in binary_suffixes:
            return
        if safe not in files:
            files.append(safe)
        if len(files) > _DICTATED_CONTENT_BUNDLE_MAX_FILES:
            raise _DictatedContentInspectionIncomplete(
                "the app bundle exceeds the file inspection limit"
            )
        return
    if entry_kind != "directory":
        raise _DictatedContentInspectionIncomplete(
            "an app-bundle entry has an invalid type classification"
        )
    if depth >= _DICTATED_CONTENT_BUNDLE_MAX_DEPTH:
        raise _DictatedContentInspectionIncomplete(
            "the app bundle exceeds the depth inspection limit"
        )
    stack.append((safe, depth + 1))


async def _walk_bundle_tree(
    sbx: Any,
    roots: list[str],
    *,
    binary_suffixes: frozenset[str],
    safe_path: Callable[[str], str | None],
) -> list[str]:
    files: list[str] = []
    visited_dirs: set[str] = set()
    stack: list[tuple[str, int]] = [(root, 0) for root in reversed(roots)]
    entries_seen = 0
    while stack:
        directory, depth = stack.pop()
        if directory in visited_dirs:
            continue
        if len(visited_dirs) >= _DICTATED_CONTENT_BUNDLE_MAX_DIRS:
            raise _DictatedContentInspectionIncomplete(
                "the app bundle exceeds the directory inspection limit"
            )
        visited_dirs.add(directory)
        entries = await _list_bundle_dir(
            sbx, directory, _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES_PER_DIR
        )
        entries_seen += len(entries)
        if entries_seen > _DICTATED_CONTENT_BUNDLE_MAX_ENTRIES:
            raise _DictatedContentInspectionIncomplete(
                "the app bundle exceeds the total entry inspection limit"
            )
        for raw_name, entry_kind in entries:
            _record_bundle_entry(
                directory,
                str(raw_name),
                entry_kind,
                depth,
                binary_suffixes=binary_suffixes,
                safe_path=safe_path,
                files=files,
                stack=stack,
            )
    return files
