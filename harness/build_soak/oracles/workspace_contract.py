"""Shared workspace-shape contract semantics for deterministic soak oracles."""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from typing import Any

_PLATFORM_MANAGED_ROOTS = frozenset({".disco", ".pmx"})


def is_platform_managed_path(path: str) -> bool:
    """Whether ``path`` belongs to a host-owned workspace namespace.

    Exact product-shape assertions exclude only these two namespaces. Arbitrary
    hidden files remain product files: treating every dotfile as bookkeeping
    would let an agent-created extra silently bypass an exact-file contract.
    """

    root = path.split("/", 1)[0]
    return root in _PLATFORM_MANAGED_ROOTS


def normalized_workspace_relative_path(value: Any) -> str | None:
    """Return one exact POSIX workspace-relative path, never a repaired path."""

    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or posixpath.normpath(value) != value
        or value in {".", ".."}
        or value.startswith("../")
    ):
        return None
    return value


def join_workspace_relative(root: str, path: str) -> str:
    return path if root == "." else f"{root}/{path}"


def resolve_open_assertion_root(
    *,
    present_paths: Iterable[str],
    declared_paths: list[str],
    verified_artifact_paths: list[str] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve target-relative assertions against one governed artifact boundary.

    ``workspace.files`` is an open-set target contract. A Freeform target may
    live below workspace root, while a strict target may use root-qualified
    paths. Only a current governed artifact entry may move the relative root.
    The artifact can be any delivery shape: a file entry point, native bundle,
    desktop package, or document.
    """

    paths = set(present_paths)
    if not declared_paths:
        return ".", None

    root_viable = all(path in paths for path in declared_paths)
    if verified_artifact_paths is None:
        return (".", None) if root_viable else (None, None)

    candidate_roots: set[str] = set()
    for raw_path in verified_artifact_paths:
        artifact_path = normalized_workspace_relative_path(raw_path)
        if artifact_path is None:
            return None, {
                "reason": "governed artifact entry is not a normalized workspace-relative path",
                "verified_artifact_path": raw_path,
            }
        candidate_roots.add(posixpath.dirname(artifact_path) or ".")
        # A directory-shaped delivery is represented by members below its
        # boundary in a file manifest. Keep both boundary vocabularies eligible;
        # the exact declared paths determine which one is viable.
        if any(path.startswith(f"{artifact_path}/") for path in paths):
            candidate_roots.add(artifact_path)

    viable = sorted(
        root
        for root in candidate_roots
        if all(join_workspace_relative(root, path) in paths for path in declared_paths)
    )
    if len(viable) == 1:
        return viable[0], None
    if len(viable) > 1:
        return None, {
            "reason": "multiple governed artifact roots satisfy the declared output contract",
            "viable_verified_roots": viable,
            "verified_artifact_paths": sorted(set(verified_artifact_paths)),
        }
    if root_viable:
        return ".", None
    return None, {
        "verified_candidate_roots": sorted(candidate_roots),
        "verified_artifact_paths": sorted(set(verified_artifact_paths)),
    }


def validate_exact_paths(value: Any) -> tuple[list[str] | None, str | None]:
    """Validate and return an exact product path list, or a stable error.

    Paths use the same POSIX workspace-relative vocabulary as scenario
    ``workspace.files[].path``. No normalization is performed on the operator's
    behalf: an ambiguous declaration is an unsatisfiable contract, not a path
    the harness should silently reinterpret.
    """

    if not isinstance(value, list) or not value:
        return None, "workspace.exact_paths must be a non-empty list"
    if not all(isinstance(path, str) and path for path in value):
        return None, "workspace.exact_paths entries must be non-empty strings"
    if len(set(value)) != len(value):
        return None, "workspace.exact_paths entries must be unique"

    for path in value:
        if normalized_workspace_relative_path(path) is None:
            return None, (
                "workspace.exact_paths entries must be normalized POSIX workspace-relative paths"
            )
        if is_platform_managed_path(path):
            return None, ("workspace.exact_paths cannot name host-owned .disco/ or .pmx/ paths")
    return list(value), None
