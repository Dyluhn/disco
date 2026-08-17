"""Governed Build Soak export comparison and artifact projection."""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import Any

from ..events import NormalizationError, normalize_events
from ..oracles.governed_admission import GovernedAdmissionOracle
from ..oracles.workspace_contract import (
    join_workspace_relative,
    normalized_workspace_relative_path,
    resolve_open_assertion_root,
)


def _archive_hashes(archive_bytes: bytes) -> tuple[dict[str, str] | None, str | None]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = [name for name in archive.namelist() if name and not name.endswith("/")]
            if len(names) != len(set(names)):
                return None, "duplicate_archive_path"
            return {
                name: hashlib.sha256(archive.read(name)).hexdigest() for name in sorted(names)
            }, None
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return None, "invalid_zip"


def _required_export_paths(
    declared_paths: list[str],
) -> tuple[list[str] | None, dict[str, Any] | None]:
    required = [path for path in declared_paths if not path.startswith((".pmx/", ".disco/"))]
    invalid = any(normalized_workspace_relative_path(path) is None for path in required)
    if invalid:
        return None, {
            "reason": "declared_export_path_not_workspace_relative",
            "required_paths": required,
        }
    return required, None


def _mismatched_export_paths(
    archive_shas: dict[str, str],
    workspace: dict[str, Any],
    resolved_required: dict[str, str],
) -> list[str]:
    mismatched = [
        path
        for path, archive_sha in archive_shas.items()
        if isinstance((item := workspace.get(path)), dict)
        and item.get("present") is True
        and item.get("sha256") != archive_sha
    ]
    for resolved in resolved_required.values():
        item = workspace.get(resolved)
        if (
            isinstance(item, dict)
            and item.get("present") is True
            and resolved in archive_shas
            and item.get("sha256") != archive_shas[resolved]
            and resolved not in mismatched
        ):
            mismatched.append(resolved)
    return sorted(mismatched)


def _export_matches_workspace(
    archive_bytes: bytes,
    workspace: dict[str, Any],
    declared_paths: list[str],
    *,
    verified_artifact_paths: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Compare an export with the exact governed target in the frozen workspace."""
    archive_shas, archive_error = _archive_hashes(archive_bytes)
    if archive_shas is None:
        return False, {"reason": archive_error}
    required, required_error = _required_export_paths(declared_paths)
    if required is None:
        return False, required_error or {}
    target_root, root_error = resolve_open_assertion_root(
        present_paths=workspace,
        declared_paths=required,
        verified_artifact_paths=verified_artifact_paths,
    )
    if target_root is None:
        return False, {
            "reason": "governed_export_target_root_unresolved",
            "required_paths": required,
            **(root_error or {}),
        }
    resolved_required = {path: join_workspace_relative(target_root, path) for path in required}
    missing = sorted(
        path for path, resolved in resolved_required.items() if resolved not in archive_shas
    )
    mismatched = _mismatched_export_paths(archive_shas, workspace, resolved_required)
    return not missing and not mismatched and bool(archive_shas), {
        "archive_file_count": len(archive_shas),
        "required_paths": required,
        "resolved_required_paths": resolved_required,
        "assertion_root": target_root,
        "missing_paths": missing,
        "mismatched_paths": mismatched,
    }


def _governed_artifact_paths(
    events: list[dict[str, Any]],
    *,
    scenario: dict[str, Any],
    conversation_id: str,
) -> list[str] | None:
    """Project exact artifact paths only from a fully admitted current receipt."""
    try:
        normalized = normalize_events(events)
    except (NormalizationError, ValueError, TypeError):
        return None
    for result in GovernedAdmissionOracle().check(
        normalized,
        scenario=scenario,
        conversation_id=conversation_id,
    ):
        paths = result.facts.get("verified_artifact_paths")
        if result.passed and isinstance(paths, list):
            return [path for path in paths if isinstance(path, str)]
    return None
