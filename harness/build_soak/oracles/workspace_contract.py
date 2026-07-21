"""Shared workspace-shape contract semantics for deterministic soak oracles."""

from __future__ import annotations

import posixpath
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
        if (
            "\\" in path
            or "\x00" in path
            or path.startswith("/")
            or posixpath.normpath(path) != path
            or path in {".", ".."}
            or path.startswith("../")
        ):
            return None, (
                "workspace.exact_paths entries must be normalized POSIX workspace-relative paths"
            )
        if is_platform_managed_path(path):
            return None, ("workspace.exact_paths cannot name host-owned .disco/ or .pmx/ paths")
    return list(value), None
