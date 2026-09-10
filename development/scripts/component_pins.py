"""Regenerate a trusted component's manifest hash pins from bytes on disk.

Usage:
    uv run python development/scripts/component_pins.py \
        packages/core/src/disco/core/trusted_components/registry_data/auth-kit/1.0.0

Pins are NEVER hand-written (spec §6.4): this script rewrites the manifest's
`files` map to cover every file under core/, using the SAME `pin()` helper the
verify path uses. The CI tripwire (test_registry_pins_match_shipped_bytes)
fails if shipped bytes and pins ever disagree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from disco.core.trusted_components import TrustedComponentManifest
from disco.core.trusted_components.verify import pin


def repin(component_dir: Path, *, allow_removals: bool = False) -> None:
    component_dir = component_dir.resolve()
    manifest_path = component_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Layout guard (review finding): the manifest must belong to THIS dir, or
    # the registry will refuse it at load ("layout mismatch") after we exit 0.
    if raw.get("name") != component_dir.parent.name or raw.get("version") != component_dir.name:
        raise SystemExit(
            f"manifest says {raw.get('name')}/{raw.get('version')} but the directory is "
            f"{component_dir.parent.name}/{component_dir.name} — fix the manifest first"
        )
    core = component_dir / "core"
    if not core.is_dir():
        raise SystemExit(f"{component_dir} has no core/ directory — nothing to pin")
    symlinks = [q for q in core.rglob("*") if q.is_symlink()]
    if symlinks:
        raise SystemExit(f"symlinks under core/ are forbidden: {symlinks}")
    files = {
        q.relative_to(component_dir).as_posix(): pin(q.read_bytes())
        for q in sorted(core.rglob("*"))
        if q.is_file()
    }
    # Removal guard (review finding): a deleted/renamed core file must never be
    # silently unpinned — that is exactly how an integrity hole ships.
    removed = sorted(set(raw.get("files", {})) - set(files))
    if removed and not allow_removals:
        raise SystemExit(
            f"these previously-pinned files are GONE from core/: {removed} — if the "
            "removal is intentional, rerun with --allow-removals"
        )
    raw["files"] = files
    # Validate BEFORE writing so a broken manifest never lands on disk.
    TrustedComponentManifest.model_validate(raw)
    manifest_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    suffix = f" (removed: {removed})" if removed else ""
    print(f"pinned {len(files)} file(s) in {manifest_path}{suffix}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--allow-removals"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    repin(Path(args[0]), allow_removals="--allow-removals" in sys.argv)
