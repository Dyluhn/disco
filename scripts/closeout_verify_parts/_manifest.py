"""Frozen-manifest tamper check — extracted from :mod:`verify_export_track1_closeout`."""

from __future__ import annotations

import json
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod


def _load_manifest(repo: Path) -> tuple[dict[str, object] | None, str]:
    manifest_path = repo / manifest_mod.MANIFEST_REL
    if not manifest_path.is_file():
        return None, "acceptance manifest is absent (run gen_closeout_acceptance_manifest.py)"
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"acceptance manifest is not valid JSON: {exc}"
    if not isinstance(stored, dict):
        return None, "acceptance manifest is not a JSON object"
    return stored, "loaded"


def _verify_frozen_manifest(
    repo: Path, stored: dict[str, object] | None, note: str
) -> tuple[bool, str]:
    if stored is None:
        return False, note
    stored_files = stored.get("files", {})
    if not isinstance(stored_files, dict):
        return False, "manifest 'files' is not a JSON object"
    fresh_files, missing = manifest_mod._iter_frozen_files(repo)
    if missing:
        return False, f"frozen paths missing from reality: {missing[:6]}"
    if stored_files != fresh_files:
        changed = sorted(
            set(stored_files) ^ set(fresh_files)
            | {k for k in set(stored_files) & set(fresh_files) if stored_files[k] != fresh_files[k]}
        )
        return False, f"frozen file hash drift on: {changed[:8]}"
    return True, "frozen files match the manifest"
