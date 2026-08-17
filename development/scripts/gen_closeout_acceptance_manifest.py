#!/usr/bin/env python3
"""Generate ``current/docs/export-track1-closeout-acceptance.sha256`` — the frozen-harness
SHA-256 manifest + red-test inventory (WO-C0, plan §1.1 / §4.2).

The manifest hashes EVERY frozen acceptance file listed in plan §1.1 EXCEPT itself
(a file cannot hash itself) and records:

* ``python_closeout_inventory`` — the FULL node-ID inventory of the focused closeout
  lane, generated deterministically at manifest-build time from
  ``pytest --collect-only`` over the closeout dirs (marker
  ``export_track1_closeout and not integration``), sorted for byte-stability. The
  verifier reads this list back FROM THIS JSON (never a Python constant) and compares
  it to a fresh collect-only, so changing pytest discovery/config cannot hide a test.
* ``frontend_closeout_inventory`` — the frozen vitest files (+ their describe/it
  titles) so the frontend lane can compare the discovered test-file set.
* ``red_tests`` — one representative failing public-boundary test per work order
  C1–C8 plus the frontend C3/C6 reds, each with its intended typed blocker/behavior.

``development/scripts/verify_export_track1_closeout.py`` reads it back to (a) prove the frozen
files are byte-unchanged since the acceptance tag and (b) compare the frozen node-ID
inventory against the live ``--collect-only``.

    uv run python development/scripts/gen_closeout_acceptance_manifest.py           # write
    uv run python development/scripts/gen_closeout_acceptance_manifest.py --check   # verify fresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from closeout_manifest_parts._data import CLOSEOUT_TEST_DIRS as CLOSEOUT_TEST_DIRS
from closeout_manifest_parts._data import COMMAND_INVENTORY as COMMAND_INVENTORY
from closeout_manifest_parts._data import FROZEN_DIR_GLOBS as FROZEN_DIR_GLOBS
from closeout_manifest_parts._data import FROZEN_FILES as FROZEN_FILES
from closeout_manifest_parts._data import NONLIVE_TEST_PATHS as NONLIVE_TEST_PATHS
from closeout_manifest_parts._data import OPERATIONAL_READING as OPERATIONAL_READING
from closeout_manifest_parts._data import RED_TESTS as RED_TESTS
from closeout_manifest_parts._data import REMEDIATION_R0 as REMEDIATION_R0
from closeout_manifest_parts._data import REMEDIATION_V5 as REMEDIATION_V5
from closeout_manifest_parts._data import REMEDIATION_V6 as REMEDIATION_V6
from closeout_manifest_parts._data import REQUIRED_COMMAND_IDS as REQUIRED_COMMAND_IDS

# ---- frozen harness definition (plan §1.1) ------------------------------------
# FROZEN_DIR_GLOBS + FROZEN_FILES (imported above from closeout_manifest_parts._data)
# are the directory globs (recursively hashed) and single files that make up the
# frozen acceptance harness. The manifest itself is EXCLUDED (it cannot hash itself).

MANIFEST_REL = "current/docs/export-track1-closeout-acceptance.sha256"

# The baseline the harness is authored against (plan header).
BASELINE_SHA = "2ec1ceba08e90bd1f45a19075d76975d44e90b7c"
ACCEPTANCE_TAG = "export-track1-closeout-acceptance-v6"

# ---- lane definitions (single source of truth; the verifier imports these) ----
# CLOSEOUT_TEST_DIRS (imported above): the focused closeout pytest lane (plan §3.2,
# second command).
CLOSEOUT_MARKER_NONLIVE = "export_track1_closeout and not integration"

# The live Docker lane (plan §3.4).
LIVE_TEST_FILE = "current/packages/agent-server/tests/integration/test_export_track1_closeout_live.py"
CLOSEOUT_MARKER_LIVE = "export_track1_closeout and integration"
# C9-03: the governed A/E capture-regression lane — a SEPARATE required live selection
# with its own frozen exact node inventory (missing/skipped/extra/unexecuted is fatal).
CAPTURE_TEST_FILE = (
    "current/packages/agent-server/tests/integration/test_closeout_live_capture_regression.py"
)

# The frontend lanes (plan §3.3).
FRONTEND_VITEST_DIR = "current/frontend/src/test/export-track1-closeout"
FRONTEND_E2E_DIR = "current/frontend/e2e/export-track1-closeout"

# NONLIVE_TEST_PATHS (imported above): the non-live pytest suite (plan §3.2, first
# command) — the FULL non-integration tree (the closeout reds run inside it too, so a
# green run proves nothing regressed).
NONLIVE_MARKER = "not integration"

# ---- R6 additions (plan §9): the frozen required command inventory (G19 / criterion 3)
# (COMMAND_INVENTORY / REQUIRED_COMMAND_IDS, imported above) and the suppression-baseline
# path (G18 / criterion 4, below). These are CONSTANTS ONLY — they are NOT emitted into
# the manifest JSON, so adding them perturbs ONLY this script's own frozen file hash
# (regenerate + re-review), never the closeout test-dir hashes or the python/frontend
# inventories. The verifier imports them so the manifest stays the single source of
# truth for the lane definitions.

# The committed, owner-approved suppression baseline the G18 diff scanner reads (plan
# §9.4 / criterion 4). Any suppression token newly added in the campaign diff
# (BASELINE_SHA..HEAD) whose (file, stripped-line) pair is NOT recorded here fails the
# anti-bypass scanner lane. Frozen (hashed above) so approving a suppression is
# tamper-evident.
SUPPRESSION_BASELINE_REL = "current/docs/export-track1-closeout-suppression-baseline.json"

# ---- the anti-bypass operational reading (§4.4) and the red-test inventory (plan
# §4.2 criterion 3) are imported above (OPERATIONAL_READING, RED_TESTS) from
# closeout_manifest_parts._data, quoted verbatim by the scanner docstring, this
# manifest ``note``, and ``anti-bypass-scan.json``. RED_TESTS names one representative
# FAILING public-boundary test per work order C1–C8, plus the frontend C3/C6 reds; the
# full per-work-order matrices live in the frozen test files (hashed above).


def repo_root() -> Path:
    """The checkout root (two levels up from this script: <root>/scripts/<this>)."""
    return Path(__file__).resolve().parents[2]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_frozen_files(root: Path) -> tuple[dict[str, str], list[str]]:
    """Return ``(files, missing)``: a repo-relative-path -> sha256 map for every
    frozen file that exists (excluding the manifest + ``__pycache__``), plus the
    sorted list of frozen globs/files that do not exist. On a finalized manifest
    ``missing`` MUST be empty — a perpetual ``missing_frozen_paths`` entry means the
    frozen set no longer matches reality."""
    files: dict[str, str] = {}
    missing: list[str] = []

    def _logical(rel: str) -> str:
        for bucket in ("current/", "development/"):
            if rel.startswith(bucket):
                return rel[len(bucket):]
        return rel

    def _resolve(rel: str) -> Path:
        """Locate a frozen path in either layout.

        The manifest records post-restructure paths, but it is also verified
        against the ratified pre-restructure tree, where the same file sits at
        the bare label. Try the recorded path, then the unbucketed one.
        """
        p = root / rel
        if p.is_file() or p.is_dir():
            return p
        for bucket in ("current/", "development/"):
            if rel.startswith(bucket):
                alt = root / rel[len(bucket):]
                if alt.is_file() or alt.is_dir():
                    return alt
        return p

    def _add_file(rel: str) -> None:
        p = _resolve(rel)
        if p.is_file():
            files[_logical(rel)] = _sha256_file(p)
        else:
            missing.append(rel)

    for rel in FROZEN_FILES:
        _add_file(rel)

    for glob in FROZEN_DIR_GLOBS:
        base = _resolve(glob)
        if not base.exists():
            missing.append(glob)
            continue
        found_any = False
        for p in sorted(base.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            # Record under the manifest's own path spelling, so the same manifest
            # verifies against both the ratified and restructured trees.
            rel_key = _logical(glob) + "/" + p.relative_to(base).as_posix()
            files[rel_key] = _sha256_file(p)
            found_any = True
        if not found_any:
            # A frozen dir glob that exists but contributes no hashable file is a
            # harness-drift signal (an empty dir cannot be frozen meaningfully).
            missing.append(glob)

    return files, sorted(missing)


def parse_collect_only_ids(stdout: str) -> list[str]:
    """Extract node IDs from ``pytest --collect-only -q`` stdout. Used by BOTH this
    generator (to freeze the inventory) and the verifier (to compare live), so the
    parse must be identical on both sides."""
    ids: list[str] = []
    skip_prefixes = ("=", "warning", "no tests", "ERROR", "<", "-")
    for raw in stdout.splitlines():
        line = raw.strip()
        if "::" in line and not line.startswith(skip_prefixes):
            ids.append(line)
    return ids


def collect_python_closeout_ids(root: Path) -> list[str]:
    """The sorted node-ID inventory of the focused closeout lane, via a real
    ``pytest --collect-only``. Deterministic (sorted; parametrize IDs are static
    literals; the seeded RNG runs at test time, not collection)."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *CLOSEOUT_TEST_DIRS,
        "-o",
        "addopts=",
        "-m",
        CLOSEOUT_MARKER_NONLIVE,
        "--collect-only",
        "-q",
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    return sorted(parse_collect_only_ids(proc.stdout))


def collect_capture_node_ids(root: Path) -> list[str]:
    """The sorted node-ID inventory of the governed A/E capture lane (C9-03), via a real
    ``pytest --collect-only`` over ``CAPTURE_TEST_FILE`` under the live marker. Frozen so
    a missing/renamed/dropped capture test makes the lane non-green."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        CAPTURE_TEST_FILE,
        "-o",
        "addopts=",
        "-m",
        CLOSEOUT_MARKER_LIVE,
        "--collect-only",
        "-q",
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    return sorted(parse_collect_only_ids(proc.stdout))


_DESCRIBE_RE = re.compile(r"""\bdescribe\(\s*(["'])((?:\\.|(?!\1).)*)\1""")
_IT_RE = re.compile(r"""\b(?:it|test)\(\s*(["'])((?:\\.|(?!\1).)*)\1""")


def frontend_closeout_inventory(root: Path) -> dict[str, dict[str, object]]:
    """The frozen vitest closeout files (+ their first ``describe`` title and every
    ``it``/``test`` title). Keyed by BASENAME so the verifier can compare the file set
    the vitest JSON reports against this frozen set. Parsed from source (cheap regex),
    not by running vitest — deterministic because the files are hashed/frozen."""
    inventory: dict[str, dict[str, object]] = {}
    base = root / FRONTEND_VITEST_DIR
    if not base.exists():
        return inventory
    for path in sorted(base.glob("*.test.tsx")):
        text = path.read_text(encoding="utf-8")
        describe = _DESCRIBE_RE.search(text)
        tests = [m.group(2) for m in _IT_RE.finditer(text)]
        inventory[path.name] = {
            "describe": describe.group(2) if describe else None,
            "tests": tests,
        }
    return inventory


def build_manifest(root: Path) -> dict[str, object]:
    files, missing = _iter_frozen_files(root)
    note = (
        "Frozen SHA-256 manifest for the Export Track-1 Closeout acceptance harness "
        "(WO-C0, plan §1.1 / §4.2). Hashes every frozen acceptance file in plan §1.1 "
        "EXCEPT this manifest (a file cannot hash itself). The intended acceptance tag "
        f"{ACCEPTANCE_TAG} is a CANDIDATE: an independent human must create the signed "
        "annotated tag and protect it in the authoritative remote — it does not yet exist "
        "and no human has ratified it (the CURRENT candidacy is acceptance-v6 — see "
        "remediation_v6.ratification_status; remediation_r0 is a HISTORICAL record). "
        "python_closeout_inventory is generated deterministically from "
        "`pytest --collect-only` over the closeout dirs (marker "
        "'export_track1_closeout and not integration'); the verifier reads it back FROM "
        "THIS JSON and compares to a fresh collect-only so changing pytest discovery "
        "cannot hide a test. frontend_closeout_inventory lists the frozen vitest files "
        "(+ every it(...) leaf title) the frontend lane compares PER FILE — the verifier "
        "diffs each file's EXECUTED (passed|failed) leaf-title set against the frozen list, "
        "so a dropped/renamed/skipped/extra title is non-green even when the filename set "
        "matches (see remediation_r0.frontend_inventory_enforcement). red_tests names one "
        "representative "
        "failing public-boundary test per work order C1–C8 plus the frontend C3/C6 "
        "reds, AND (acceptance-v4 / R0) one per independent-audit gap "
        "G02/G04/G05/G06/G07/G12/G13 plus the self-discriminating G08 URL-binding red and "
        "the G11 nullable-binding COMPILE red; remediation_r0 carries the 15-node backend "
        "proving set, the frontend proving reds, the G11 compile red + evidence-hygiene "
        "regression, the ratification_status (the v4 candidacy is SUPERSEDED by "
        "acceptance-v5 — see remediation_v5; no human has signed/protected any tag), and "
        "the R4-activated / record-only / ratified-baseline "
        "ledger (the R1–R6 production fixes are PARKED — R0 only freezes the reds). "
        "Anti-bypass operational reading (§4.4): " + OPERATIONAL_READING
    )
    return {
        "schema": "export-track1-closeout-acceptance/v1",
        "note": note,
        "acceptance_tag": ACCEPTANCE_TAG,
        "baseline_sha": BASELINE_SHA,
        "seed": {"env": "CLOSEOUT_SEED", "default": "export-track1-closeout-v1"},
        "manifest_excludes_self": MANIFEST_REL,
        "anti_bypass_operational_reading": OPERATIONAL_READING,
        "files": dict(sorted(files.items())),
        "missing_frozen_paths": missing,
        "python_closeout_inventory": collect_python_closeout_ids(root),
        "governed_capture_inventory": collect_capture_node_ids(root),
        "frontend_closeout_inventory": frontend_closeout_inventory(root),
        "red_tests": list(RED_TESTS),
        "remediation_r0": REMEDIATION_R0,
        "remediation_v5": REMEDIATION_V5,
        "remediation_v6": REMEDIATION_V6,
    }


def render(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


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
        current = target.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"STALE: {MANIFEST_REL} differs from a fresh regeneration "
                "(a frozen file changed — re-run without --check and re-review)",
                file=sys.stderr,
            )
            return 1
        checked = manifest["files"]
        n_checked = len(checked) if isinstance(checked, dict) else 0
        missing = manifest["missing_frozen_paths"]
        if missing:
            print(f"DRIFT: frozen paths missing from reality: {missing}", file=sys.stderr)
            return 1
        print(f"OK: {MANIFEST_REL} is fresh ({n_checked} frozen files hashed, 0 missing)")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    file_map = manifest["files"]
    n = len(file_map) if isinstance(file_map, dict) else 0
    inv = manifest["python_closeout_inventory"]
    n_inv = len(inv) if isinstance(inv, list) else 0
    missing = manifest["missing_frozen_paths"]
    print(
        f"WROTE {MANIFEST_REL}: {n} frozen files hashed; "
        f"python inventory={n_inv} node IDs; missing={missing}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
