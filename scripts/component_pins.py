"""Regenerate a trusted component's manifest hash pins from bytes on disk.

Usage:
    uv run python scripts/component_pins.py \
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


def repin(component_dir: Path) -> None:
    manifest_path = component_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    core = component_dir / "core"
    if not core.is_dir():
        raise SystemExit(f"{component_dir} has no core/ directory — nothing to pin")
    files = {
        p.relative_to(component_dir).as_posix(): pin(p.read_bytes())
        for p in sorted(core.rglob("*"))
        if p.is_file()
    }
    raw["files"] = files
    # Validate BEFORE writing so a broken manifest never lands on disk.
    TrustedComponentManifest.model_validate(raw)
    manifest_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"pinned {len(files)} file(s) in {manifest_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    repin(Path(sys.argv[1]))
