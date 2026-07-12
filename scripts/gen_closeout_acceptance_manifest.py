#!/usr/bin/env python3
"""Generate `docs/export-track1-closeout-acceptance.sha256` — the frozen-harness
SHA-256 manifest + red-test node-ID inventory (WO-C0, plan §1.1 / §4 criterion 2).

The manifest hashes EVERY frozen acceptance file listed in plan §1.1 EXCEPT
itself (a file cannot hash itself), and records the red-test node-ID inventory and
each work order's intended failure. `scripts/verify_export_track1_closeout.py`
reads it back to (a) prove the frozen files are byte-unchanged since the
acceptance tag and (b) compare the live `--collect-only` node IDs against the
frozen inventory, so changing pytest discovery cannot hide a test.

SCAFFOLD (this tranche): the C3–C8 matrices, the Playwright spec, and its snapshot
baselines do not exist yet, so their frozen globs are recorded under
`missing_frozen_paths` rather than hashed. Regenerate (finalize) once the red-test
matrices are complete, then a reviewer creates the protected acceptance tag over
the manifest itself. Run:

    uv run python scripts/gen_closeout_acceptance_manifest.py           # write
    uv run python scripts/gen_closeout_acceptance_manifest.py --check   # verify fresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# ---- frozen harness definition (plan §1.1) ------------------------------------

MANIFEST_REL = "docs/export-track1-closeout-acceptance.sha256"

# Directory globs (recursively hashed) and single files that make up the frozen
# acceptance harness. The manifest itself is EXCLUDED (it cannot hash itself).
FROZEN_DIR_GLOBS: tuple[str, ...] = (
    "packages/core/tests/export_track1_closeout",
    "packages/tools/tests/export_track1_closeout",
    "packages/agent-server/tests/export_track1_closeout",
    "packages/agent-server/tests/fixtures/export_track1_closeout",
    "frontend/src/test/export-track1-closeout",
    "frontend/e2e/export-track1-closeout.spec.ts-snapshots",
)
FROZEN_FILES: tuple[str, ...] = (
    "scripts/verify_export_track1_closeout.py",
    "scripts/gen_closeout_acceptance_manifest.py",
    "packages/agent-server/tests/integration/test_export_track1_closeout_live.py",
    "frontend/e2e/export-track1-closeout.spec.ts",
    ".github/workflows/export-track1-closeout.yml",
    "docs/export-track1-closeout-work-orders.md",
)

# The baseline the harness is authored against (plan header).
BASELINE_SHA = "2ec1ceba08e90bd1f45a19075d76975d44e90b7c"
ACCEPTANCE_TAG = "export-track1-closeout-acceptance-v1"

# The FROZEN python closeout node-ID inventory (compared against `--collect-only`).
PYTHON_CLOSEOUT_INVENTORY: tuple[str, ...] = (
    "packages/tools/tests/export_track1_closeout/test_c1_intent_store_custom_root.py"
    "::test_release_declare_writes_under_configured_custom_root",
    "packages/agent-server/tests/export_track1_closeout/test_c2_release_source_binding.py"
    "::test_release_never_returns_a_speculative_version_seq",
)

# The prove-the-plumbing red tests shipped in this tranche, with each work order's
# INTENDED failure (plan §4 criterion 2). Later tranches append the full matrices.
RED_TESTS: tuple[dict[str, str], ...] = (
    {
        "work_order": "C1",
        "node_id": (
            "packages/tools/tests/export_track1_closeout/test_c1_intent_store_custom_root.py"
            "::test_release_declare_writes_under_configured_custom_root"
        ),
        "boundary": "real DefaultToolExecutor via ConversationRuntime.execute_pi_tool",
        "expected_failure": (
            "release_declare writes the sidecar under the DISCO_DATA_DIR default root "
            "(ProjectStore('')) instead of the configured custom projects root"
        ),
    },
    {
        "work_order": "C2",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_c2_release_source_binding.py"
            "::test_release_never_returns_a_speculative_version_seq"
        ),
        "boundary": "real FastAPI GET /api/projects/{cid}/release router",
        "expected_failure": (
            "route returns a speculative version_seq (max+1) that names no VersionRecord "
            "when the live tree diverges from the committed version"
        ),
    },
    {
        "work_order": "C3",
        "node_id": (
            "frontend/src/test/export-track1-closeout/c3-candidate-copy.test.tsx"
            "::renders 'Bundle available' + 'Not runtime-verified' and no case-insensitive 'ready'"
        ),
        "boundary": "SelfHostPanel rendered through the real useProjectRelease hook",
        "expected_failure": (
            "candidate panel renders 'Ready to self-host' instead of the exact strings "
            "'Bundle available' and 'Not runtime-verified' (DOM still contains 'ready')"
        ),
    },
)


def repo_root() -> Path:
    """The checkout root (two levels up from this script: <root>/scripts/<this>)."""
    return Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_frozen_files(root: Path) -> tuple[dict[str, str], list[str]]:
    """Return `(files, missing)`: a repo-relative-path → sha256 map for every frozen
    file that exists (excluding the manifest + __pycache__), plus the sorted list of
    frozen globs that do not exist yet (the deferred C3–C8 / Playwright paths)."""
    files: dict[str, str] = {}
    missing: list[str] = []

    def _add_file(rel: str) -> None:
        p = root / rel
        if p.is_file():
            files[rel] = _sha256_file(p)
        else:
            missing.append(rel)

    for rel in FROZEN_FILES:
        _add_file(rel)

    for glob in FROZEN_DIR_GLOBS:
        base = root / glob
        if not base.exists():
            missing.append(glob)
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            files[p.relative_to(root).as_posix()] = _sha256_file(p)

    return files, sorted(missing)


def build_manifest(root: Path) -> dict[str, object]:
    files, missing = _iter_frozen_files(root)
    return {
        "schema": "export-track1-closeout-acceptance/v1",
        "note": (
            "SCAFFOLD — hashes every frozen acceptance file in plan §1.1 EXCEPT this "
            "manifest. Finalized when the C1–C7 red-test matrices are complete; a "
            "reviewer then creates the protected tag over this file."
        ),
        "acceptance_tag": ACCEPTANCE_TAG,
        "baseline_sha": BASELINE_SHA,
        "seed": {"env": "CLOSEOUT_SEED", "default": "export-track1-closeout-v1"},
        "manifest_excludes_self": MANIFEST_REL,
        "files": dict(sorted(files.items())),
        "missing_frozen_paths": missing,
        "python_closeout_inventory": list(PYTHON_CLOSEOUT_INVENTORY),
        "red_tests": list(RED_TESTS),
    }


def render(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the on-disk manifest matches a fresh regeneration; do not write",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    manifest = build_manifest(root)
    rendered = render(manifest)
    target = root / MANIFEST_REL

    if args.check:
        if not target.is_file():
            print(f"MISSING: {MANIFEST_REL} does not exist", file=sys.stderr)
            return 1
        current = target.read_text()
        if current != rendered:
            print(
                f"STALE: {MANIFEST_REL} differs from a fresh regeneration "
                "(a frozen file changed — re-run without --check and re-review)",
                file=sys.stderr,
            )
            return 1
        checked = manifest["files"]
        n_checked = len(checked) if isinstance(checked, dict) else 0
        print(f"OK: {MANIFEST_REL} is fresh ({n_checked} frozen files hashed)")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered)
    file_map = manifest["files"]
    n = len(file_map) if isinstance(file_map, dict) else 0
    missing = manifest["missing_frozen_paths"]
    print(f"WROTE {MANIFEST_REL}: {n} frozen files hashed; missing={missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
