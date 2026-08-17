"""Public-surface extraction and exact-row comparison.

Pure functions over scanned surfaces: no Git access, no repository authority
loading, no monkeypatched state.  Relocated verbatim from ``public_api`` in
Epic 10-D; behaviour is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..public_surface import extract_initializer
from ._constants import TargetKey


def extract_init_surface(path: Path) -> dict[str, Any]:
    root = path
    while root != root.parent and not (root / ".git").exists():
        root = root.parent
    if not (root / ".git").exists():
        raise ValueError("initializer must be inside an explicit Git root")
    rel = path.relative_to(root).as_posix()
    surface = extract_initializer(root, rel)
    return {key: value for key, value in surface.items() if key != "path"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def exact_rows(
    owner: dict[str, Any], key: str, label: str, problems: list[str],
) -> list[dict[str, Any]]:
    rows = owner.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        problems.append(f"{label} must be an exact object list")
        return []
    paths = [row.get("path") for row in rows]
    if any(not isinstance(path, str) or not path for path in paths):
        problems.append(f"{label} paths must be non-empty strings")
    elif paths != sorted(paths) or len(paths) != len(set(paths)):
        problems.append(f"{label} paths must be sorted and unique")
    return rows


def compare_exact(
    label: str, stored: list[dict[str, Any]],
    actual: list[dict[str, Any]], problems: list[str],
) -> None:
    if stored == actual:
        return
    stored_map = {row.get("path"): row for row in stored}
    actual_map = {row.get("path"): row for row in actual}
    deleted = sorted(set(stored_map) - set(actual_map))
    added = sorted(set(actual_map) - set(stored_map))
    common = set(stored_map) & set(actual_map)
    changed = sorted(path for path in common if stored_map[path] != actual_map[path])
    problems.append(
        f"{label} drift: deleted={deleted}, added={added}, changed={changed}"
    )


def target_key(row: dict[str, Any]) -> TargetKey:
    return (row["surface"], row["path"], row["public_name"], row["target_sha256"])


def _python_descriptors(
    python_surface: list[dict[str, Any]], targets: dict[TargetKey, dict[str, Any]],
) -> None:
    for row in python_surface:
        path = row["path"]
        occurrences: dict[str, int] = {}
        for public_name in row["public_names"]:
            descriptor = {
                "surface": "python",
                "path": bucket_normalized(path),
                "public_name": public_name,
                "public_signature": row["public_signatures"][public_name],
                "import_origins": [
                    origin
                    for origin in row["import_origins"]
                    if origin["public_name"] == public_name
                ],
            }
            descriptor_key = canonical(descriptor)
            occurrences[descriptor_key] = occurrences.get(descriptor_key, 0) + 1
            descriptor["occurrence"] = occurrences[descriptor_key]
            digest = hashlib.sha256(canonical(descriptor).encode("utf-8")).hexdigest()
            targets[("python", bucket_normalized(path), public_name, digest)] = descriptor


def _frontend_descriptors(
    frontend_surface: list[dict[str, Any]], targets: dict[TargetKey, dict[str, Any]],
) -> None:
    for row in frontend_surface:
        path = row["path"]
        occurrences: dict[str, int] = {}
        for declaration in row["public_declarations"]:
            descriptor = {
                "surface": "frontend",
                "path": bucket_normalized(path),
                "public_name": declaration["name"],
                "declaration": declaration,
            }
            descriptor_key = canonical(descriptor)
            occurrences[descriptor_key] = occurrences.get(descriptor_key, 0) + 1
            descriptor["occurrence"] = occurrences[descriptor_key]
            digest = hashlib.sha256(canonical(descriptor).encode("utf-8")).hexdigest()
            targets[("frontend", bucket_normalized(path), declaration["name"], digest)] = descriptor


def bucket_normalized(path: str) -> str:
    """Drop the top-level bucket so a pure directory move is not a rename.

    The 2026-08-17 restructure moved every tracked path under current/ or
    development/ without touching a byte of the surface. Target identity is
    (surface, path, name, digest); keying on the bucketed path would read that
    move as deleting every public target and adding it back, which the
    immutability rule refuses. Normalising the bucket away keeps a genuine
    rename or deletion detectable while letting the move through.
    """
    for bucket in ("current/", "development/"):
        if path.startswith(bucket):
            return path[len(bucket):]
    return path


def surface_targets(
    python_surface: list[dict[str, Any]], frontend_surface: list[dict[str, Any]],
) -> dict[TargetKey, dict[str, Any]]:
    targets: dict[TargetKey, dict[str, Any]] = {}
    _python_descriptors(python_surface, targets)
    _frontend_descriptors(frontend_surface, targets)
    return targets


def initializer_module(path: Any) -> str | None:
    if (
        not isinstance(path, str)
        or not bucket_normalized(path).startswith("packages/")
        or "/src/" not in path
        or not path.endswith("/__init__.py")
    ):
        return None
    return path.split("/src/", 1)[1][:-3].replace("/", ".").removesuffix(".__init__")


def from_origin(source: str, origin: dict[str, Any]) -> str | None:
    module = origin.get("module")
    name = origin.get("name")
    level = origin.get("level")
    if (
        not isinstance(name, str)
        or not name
        or module is not None
        and not isinstance(module, str)
        or not isinstance(level, int)
        or level < 0
    ):
        return None
    if not level:
        resolved = module or ""
        return f"{resolved}.{name}" if resolved else name
    parts = source.split(".")
    parent_count = level - 1
    if parent_count > len(parts):
        return None
    parts = parts[: len(parts) - parent_count] if parent_count else parts
    if module:
        parts.extend(module.split("."))
    resolved = ".".join(parts)
    return f"{resolved}.{name}" if resolved else name


def target_origin(target: dict[str, Any]) -> str | None:
    origins = target.get("import_origins")
    source = initializer_module(target.get("path"))
    if (
        not isinstance(origins, list)
        or len(origins) != 1
        or source is None
        or not isinstance(origins[0], dict)
    ):
        return None
    origin = origins[0]
    module = origin.get("module")
    if origin.get("kind") == "import":
        return module if isinstance(module, str) and module else None
    if origin.get("kind") != "from":
        return None
    return from_origin(source, origin)


def stored_surface(baseline: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = baseline.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError(f"accepted {key} authority is missing")
    return rows
