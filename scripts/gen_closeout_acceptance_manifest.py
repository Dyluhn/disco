#!/usr/bin/env python3
"""Generate ``docs/export-track1-closeout-acceptance.sha256`` — the frozen-harness
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

``scripts/verify_export_track1_closeout.py`` reads it back to (a) prove the frozen
files are byte-unchanged since the acceptance tag and (b) compare the frozen node-ID
inventory against the live ``--collect-only``.

    uv run python scripts/gen_closeout_acceptance_manifest.py           # write
    uv run python scripts/gen_closeout_acceptance_manifest.py --check   # verify fresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# ---- frozen harness definition (plan §1.1) ------------------------------------

MANIFEST_REL = "docs/export-track1-closeout-acceptance.sha256"

# Directory globs (recursively hashed) and single files that make up the frozen
# acceptance harness. The manifest itself is EXCLUDED (it cannot hash itself). This
# set matches REALITY on the acceptance branch: three closeout test dirs, the fixture
# README, the vitest dir, and the Playwright e2e DIR (three ``.spec.ts`` proofs).
FROZEN_DIR_GLOBS: tuple[str, ...] = (
    "packages/core/tests/export_track1_closeout",
    "packages/tools/tests/export_track1_closeout",
    "packages/agent-server/tests/export_track1_closeout",
    "packages/agent-server/tests/fixtures/export_track1_closeout",
    "frontend/src/test/export-track1-closeout",
    "frontend/e2e/export-track1-closeout",
)
FROZEN_FILES: tuple[str, ...] = (
    "scripts/verify_export_track1_closeout.py",
    "scripts/gen_closeout_acceptance_manifest.py",
    "packages/agent-server/tests/integration/test_export_track1_closeout_live.py",
    "packages/agent-server/tests/integration/_closeout_live_support.py",
    ".github/workflows/export-track1-closeout.yml",
    "docs/export-track1-closeout-work-orders.md",
)

# The baseline the harness is authored against (plan header).
BASELINE_SHA = "2ec1ceba08e90bd1f45a19075d76975d44e90b7c"
ACCEPTANCE_TAG = "export-track1-closeout-acceptance-v1"

# ---- lane definitions (single source of truth; the verifier imports these) ----

# The focused closeout pytest lane (plan §3.2, second command).
CLOSEOUT_TEST_DIRS: tuple[str, ...] = (
    "packages/core/tests/export_track1_closeout",
    "packages/tools/tests/export_track1_closeout",
    "packages/agent-server/tests/export_track1_closeout",
)
CLOSEOUT_MARKER_NONLIVE = "export_track1_closeout and not integration"

# The live Docker lane (plan §3.4).
LIVE_TEST_FILE = "packages/agent-server/tests/integration/test_export_track1_closeout_live.py"
CLOSEOUT_MARKER_LIVE = "export_track1_closeout and integration"

# The frontend lanes (plan §3.3).
FRONTEND_VITEST_DIR = "frontend/src/test/export-track1-closeout"
FRONTEND_E2E_DIR = "frontend/e2e/export-track1-closeout"

# The non-live pytest suite (plan §3.2, first command) — the FULL non-integration
# tree (the closeout reds run inside it too, so a green run proves nothing regressed).
NONLIVE_TEST_PATHS: tuple[str, ...] = (
    "packages/core/tests",
    "packages/tools/tests",
    "packages/agent-server/tests",
)
NONLIVE_MARKER = "not integration"

# ---- the anti-bypass operational reading (§4.4), quoted verbatim by the scanner
# docstring, this manifest ``note``, and ``anti-bypass-scan.json`` -----------------

OPERATIONAL_READING = (
    "A closeout acceptance test may seam the system ONLY at (1) the config loader — "
    "monkeypatch.setattr(cfg_store, 'load', ...), the same injection the settings PUT "
    "performs — and (2) the environment — monkeypatch.setenv/delenv (e.g. "
    "DISCO_DATA_DIR). No other monkeypatch.setattr target is permitted; the code under "
    "test (detect / spec / emit / release-route / tool / store) is never patched. "
    "unittest.mock / MagicMock / Mock() / create_autospec / patch() are forbidden "
    "entirely. In the frontend, vi.fn() is allowed ONLY as a callback/prop value (e.g. "
    "onDownload={vi.fn()}) or a locally-declared const passed as one; "
    "vi.mock / vi.spyOn / vi.stubGlobal / vi.stubEnv / jest.mock (module replacement) "
    "are forbidden. Production source may not reference closeout fixture names, the C8 "
    "secret/build sentinels, CLOSEOUT_SEED, a force-verdict switch, or "
    "PYTEST_CURRENT_TEST outside the ratified pre-existing auth/workflow baseline. A "
    "lexical/AST scan cannot prove the absence of a semantically-equivalent indirect "
    "branch; the independent diff review (plan §4.9) is the required backstop."
)

# ---- red-test inventory (plan §4.2 criterion 3) -------------------------------
# One representative FAILING public-boundary test per work order C1–C8, plus the
# frontend C3/C6 reds. Each names the intended typed blocker/behavior. The full
# per-work-order matrices live in the frozen test files (hashed above); these are the
# reviewer's index into them.

RED_TESTS: tuple[dict[str, object], ...] = (
    {
        "work_order": "C1",
        "lane": "python-closeout",
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
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_c2_release_source_binding.py"
            "::test_release_never_returns_a_speculative_version_seq"
        ),
        "boundary": "real FastAPI GET /api/projects/{cid}/release router",
        "blocker_code": "source_not_snapshotted",
        "expected_failure": (
            "with only version 1 stored and dirty live bytes, the route returns a "
            "speculative version_seq (max+1) naming no VersionRecord instead of "
            "needs_review + self_host:false + blocker source_not_snapshotted"
        ),
    },
    {
        "work_order": "C3",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_c3_detection_matrix.py"
            "::test_negative_matrix_fails_closed_with_exact_blocker[node_undeclared_env]"
        ),
        "boundary": "real FastAPI release router over the negative detection fixture matrix",
        "blocker_codes": [
            "required_env_unresolved",
            "port_contract_unresolved",
            "entrypoint_unresolved",
            "toolchain_unsupported",
            "output_dir_unresolved",
            "health_path_unresolved",
            "runtime_conflict",
        ],
        "expected_failure": (
            "predictably broken runtime contracts (undeclared env, literal port, nested "
            "entrypoint, unsupported toolchain, dynamic outDir, missing health route, "
            "competing runtime evidence) are guessed into a false candidate instead of "
            "failing closed with the exact typed blocker; representative red asserts "
            "required_env_unresolved for the undeclared-env fixture"
        ),
    },
    {
        "work_order": "C4",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_c4_env_build_toolchain_matrix.py"
            "::test_secret_build_var_uses_secret_mount_or_fails_closed"
        ),
        "boundary": "real release spec assembly + Compose/Dockerfile emission",
        "blocker_codes": [
            "secret_build_env_unsupported",
            "intent_upgrade_required",
            "package_manager_conflict",
        ],
        "expected_failure": (
            "a secret-classed build var is lowered to an ordinary ARG (image/history "
            "leakage) instead of a real build-secret mount OR a typed "
            "secret_build_env_unsupported; sibling reds "
            "test_lockfile_package_manager_disagreement_fails_closed (package_manager_conflict) "
            "and test_v1_intent_sidecar_is_migrated_or_rejected_not_crashed "
            "(intent_upgrade_required)"
        ),
    },
    {
        "work_order": "C5",
        "lane": "python-closeout",
        "node_id": (
            "packages/tools/tests/export_track1_closeout/test_c5_injection_reject.py"
            "::test_argv_injection_corpus_rejected[start_cmd-cmd_subst]"
        ),
        "boundary": "real release-intent validation at the tool boundary (pre-emission)",
        "expected_failure": (
            "a $()-command-substitution token in start_cmd reaches shell/argv lowering "
            "instead of being rejected before emission (representative of the full "
            "argv/path/health/resource injection corpus)"
        ),
    },
    {
        "work_order": "C6",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_c6_collision_matrix.py"
            "::test_c6_overlay_path_collision_matrix"
        ),
        "boundary": "real release route + zip writer over the overlay-collision matrix",
        "blocker_code": "overlay_path_conflict",
        "expected_failure": (
            "a workspace collision with a generated overlay path yields a partial / "
            "self_host:true overlay instead of needs_review + self_host:false + "
            "spec_digest:null + blocker overlay_path_conflict with the exact path"
        ),
    },
    {
        "work_order": "C7",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_c7_topology_matrix.py"
            "::test_web_worker_bound_db_env_and_mount_isolated_to_consumer"
        ),
        "boundary": "real multi-service Compose emission (web + worker fixture)",
        "expected_failure": (
            "the db mount and bound DATABASE_URL fan out to every service instead of "
            "only the declared consumer (worker receives neither)"
        ),
    },
    {
        "work_order": "C8",
        "lane": "live-docker",
        "node_id": (
            "packages/agent-server/tests/integration/test_export_track1_closeout_live.py"
            "::test_live_express_node_bundle_lifecycle"
        ),
        "boundary": (
            "real authenticated /release -> bound /download -> Docker clean-room "
            "lifecycle on a self-hosted Docker host"
        ),
        "expected_failure": (
            "on a host WITHOUT a real Docker engine the lane FAILS (never skips) via "
            "require_live_runtime / test_docker_and_compose_are_available_for_the_live_lane; "
            "on a Docker host at baseline the bound-download, health-contract and "
            "secret-absence gaps fail until C1–C7 land"
        ),
    },
    {
        "work_order": "C3",
        "lane": "frontend",
        "node_id": (
            "frontend/src/test/export-track1-closeout/c3-candidate-copy.test.tsx"
            "::renders 'Bundle available' + 'Not runtime-verified' and no "
            "case-insensitive 'ready'"
        ),
        "boundary": "SelfHostPanel rendered through the real useProjectRelease hook",
        "expected_failure": (
            "the candidate panel renders 'Ready to self-host' instead of the exact "
            "strings 'Bundle available' and 'Not runtime-verified' (DOM still contains "
            "case-insensitive 'ready')"
        ),
    },
    {
        "work_order": "C6",
        "lane": "frontend",
        "node_id": (
            "frontend/src/test/export-track1-closeout/c6-collision-blockers.test.tsx"
            "::renders every collision blocker (code + exact path) and only the plain "
            "download"
        ),
        "boundary": "SelfHostPanel rendered with an overlay-collision release verdict",
        "expected_failure": (
            "an overlay-collision verdict still exposes a self-host action / hides "
            "collision blockers instead of rendering every blocker (code + exact path) "
            "with only the plain source download"
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
    """Return ``(files, missing)``: a repo-relative-path -> sha256 map for every
    frozen file that exists (excluding the manifest + ``__pycache__``), plus the
    sorted list of frozen globs/files that do not exist. On a finalized manifest
    ``missing`` MUST be empty — a perpetual ``missing_frozen_paths`` entry means the
    frozen set no longer matches reality."""
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
        found_any = False
        for p in sorted(base.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            files[p.relative_to(root).as_posix()] = _sha256_file(p)
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
        "EXCEPT this manifest (a file cannot hash itself); the reviewer-created "
        f"protected tag {ACCEPTANCE_TAG} freezes the manifest itself. "
        "python_closeout_inventory is generated deterministically from "
        "`pytest --collect-only` over the closeout dirs (marker "
        "'export_track1_closeout and not integration'); the verifier reads it back FROM "
        "THIS JSON and compares to a fresh collect-only so changing pytest discovery "
        "cannot hide a test. frontend_closeout_inventory lists the frozen vitest files "
        "(+ titles) the frontend lane compares. red_tests names one representative "
        "failing public-boundary test per work order C1–C8 plus the frontend C3/C6 "
        "reds. Anti-bypass operational reading (§4.4): " + OPERATIONAL_READING
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
        "frontend_closeout_inventory": frontend_closeout_inventory(root),
        "red_tests": list(RED_TESTS),
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
