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
    # R6b: the purpose-built pytest reporting plugin the verifier loads into every governed
    # pytest lane (``-p closeout_pytest_report``) to prove every governed-selected test truly
    # PASSED (JUnit collapses xfail->skipped, hides a non-strict xpass, and omits a vanished
    # test). Frozen so a lane's truth-source cannot be silently weakened.
    "scripts/closeout_pytest_report.py",
    "packages/agent-server/tests/integration/test_export_track1_closeout_live.py",
    "packages/agent-server/tests/integration/_closeout_live_support.py",
    # acceptance-v5: the capture/structural-rendering regression suite the 2026-07-17
    # owner rulings (A / E) require — proves the exec_env exemption is narrow, ordinary
    # recorded output is still swept, and the structural rendering excludes ONLY
    # externally supplied env (an in-yaml secret still fails). Frozen so the ruled
    # guarantees cannot be silently weakened.
    "packages/agent-server/tests/integration/test_closeout_live_capture_regression.py",
    ".github/workflows/export-track1-closeout.yml",
    "docs/export-track1-closeout-work-orders.md",
    # The dedicated tsconfig for the G11 nullable-binding compile lane (acceptance-v4).
    # The contract file it checks lives under the frozen frontend/src/test dir glob.
    "frontend/tsconfig.closeout-g11.json",
    # R6 (G18): the owner-approved suppression baseline the anti-bypass diff scanner
    # reads. Freezing it means adding an approved suppression requires a manifest
    # regeneration + re-review (tamper-evident), not a silent edit.
    "docs/export-track1-closeout-suppression-baseline.json",
)

# The baseline the harness is authored against (plan header).
BASELINE_SHA = "2ec1ceba08e90bd1f45a19075d76975d44e90b7c"
ACCEPTANCE_TAG = "export-track1-closeout-acceptance-v6"

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
# C9-03: the governed A/E capture-regression lane — a SEPARATE required live selection
# with its own frozen exact node inventory (missing/skipped/extra/unexecuted is fatal).
CAPTURE_TEST_FILE = (
    "packages/agent-server/tests/integration/test_closeout_live_capture_regression.py"
)

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

# ---- R6 additions (plan §9): the frozen required command inventory (G19 / criterion 3)
# and the suppression-baseline path (G18 / criterion 4). These are CONSTANTS ONLY — they
# are NOT emitted into the manifest JSON, so adding them perturbs ONLY this script's own
# frozen file hash (regenerate + re-review), never the closeout test-dir hashes or the
# python/frontend inventories. The verifier imports them so the manifest stays the single
# source of truth for the lane definitions.

# The exact command each verifier lane runs, keyed by a stable ID. The verifier records
# the IDs it actually dispatched and gates on EXACT set-equality with
# REQUIRED_COMMAND_IDS: an omitted command OR a substitute/additional command is rejected
# (criterion 3). The browser and G11 descriptors name the e2e DIRECTORY and the dedicated
# tsconfig — never a nonexistent single ``e2e/export-track1-closeout.spec.ts`` file (G19).
# Descriptors use repo/frontend-relative paths for byte-stability across hosts.
COMMAND_INVENTORY: dict[str, str] = {
    "python_nonlive": (
        "python -m pytest packages/core/tests packages/tools/tests "
        "packages/agent-server/tests -o addopts= -m 'not integration' "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
    "python_closeout": (
        "python -m pytest packages/core/tests/export_track1_closeout "
        "packages/tools/tests/export_track1_closeout "
        "packages/agent-server/tests/export_track1_closeout -o addopts= "
        "-m 'export_track1_closeout and not integration' "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
    "python_closeout_collect": (
        "python -m pytest packages/core/tests/export_track1_closeout "
        "packages/tools/tests/export_track1_closeout "
        "packages/agent-server/tests/export_track1_closeout -o addopts= "
        "-m 'export_track1_closeout and not integration' --collect-only -q"
    ),
    "frontend_vitest": "npx vitest run src/test/export-track1-closeout --reporter=json",
    "frontend_typecheck": "npm run typecheck:build",
    "frontend_build": "npx vite build",
    "g11_typecheck": "npx tsc -p tsconfig.closeout-g11.json --noEmit",
    "browser_e2e": (
        "npx playwright test e2e/export-track1-closeout --reporter=json --project=firefox"
    ),
    "live_docker": (
        "python -m pytest "
        "packages/agent-server/tests/integration/test_export_track1_closeout_live.py "
        "-o addopts= -m 'export_track1_closeout and integration' -ra "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
    "live_capture": (
        "python -m pytest "
        "packages/agent-server/tests/integration/test_closeout_live_capture_regression.py "
        "-o addopts= -m 'export_track1_closeout and integration' -ra "
        "-p closeout_pytest_report --closeout-report-json <report> --junitxml"
    ),
}
REQUIRED_COMMAND_IDS: frozenset[str] = frozenset(COMMAND_INVENTORY)

# The committed, owner-approved suppression baseline the G18 diff scanner reads (plan
# §9.4 / criterion 4). Any suppression token newly added in the campaign diff
# (BASELINE_SHA..HEAD) whose (file, stripped-line) pair is NOT recorded here fails the
# anti-bypass scanner lane. Frozen (hashed above) so approving a suppression is
# tamper-evident.
SUPPRESSION_BASELINE_REL = "docs/export-track1-closeout-suppression-baseline.json"

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
    # ---- R0 acceptance additions: the independent-audit gap-ledger proving reds
    # (plan Consolidated Remediation Plan, gaps G02/G04/G05/G06/G07/G12/G13). One
    # representative index entry per gap; sibling nodes named in expected_failure. All
    # node IDs cross-checked against a real `pytest --collect-only` (the machine-truth
    # python_closeout_inventory). The PRODUCTION fixes are PARKED (R1–R6); R0 only
    # FREEZES these reds against 581d1fbe. See red_remediation_r0 below for the full
    # 15-node proving set + the R4-activated / record-only / ratified-baseline ledger.
    {
        "work_order": "R1/G02",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_g02_positional_credential.py"
            "::test_positional_credential_rejected_at_release_declare[leading]"
        ),
        "boundary": (
            "real release_declare ToolExecutor (execute_pi_tool) + real release route "
            "+ real bound /download zip bytes"
        ),
        "expected_failure": (
            "a bare POSITIONAL literal credential (npm _authToken operand — no flag, no "
            "'=', no shell metacharacter, not a ${NAME} ref) passes check_token_hygiene "
            "AND the positional-operand branch of check_declaration_argv, so it is "
            "ACCEPTED at declare, PERSISTS in release-intent.json, and SHIPS verbatim in "
            "the emitted Dockerfile CMD + release.json; it must fail closed like the C5 "
            "flag forms. Siblings cover the middle/trailing positions and the "
            "does_not_ship_through_release_download half (6 nodes total). Fix = R1."
        ),
    },
    {
        "work_order": "R2/G04",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_g04_interpreted_install.py"
            "::test_interpreted_candidate_with_deps_emits_dependency_install_layer"
            "[express_node_no_build]"
        ),
        "boundary": (
            "real release + /download routes; assertion on emitted Dockerfile bytes in the zip"
        ),
        "expected_failure": (
            "a typed INTERPRETED candidate with dependencies and NO build step emits an "
            "empty build_cmd, so local_compose._effective_install_cmd returns () and the "
            "Dockerfile is COPY -> CMD with NO npm/pip install layer -> unrunnable image "
            "('Cannot find module express' / uvicorn missing). Must emit a dependency "
            "install layer or fail closed. Sibling [fastapi_python_no_build] (2 nodes). "
            "= C8 finding F1. Fix = R2."
        ),
    },
    {
        "work_order": "R2/G05",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_g05_typed_pnpm_start.py"
            "::test_typed_pnpm_start_intent_is_rejected_or_provisions_pnpm"
        ),
        "boundary": (
            "real release + /download routes; assertion on /release JSON + emitted Dockerfile bytes"
        ),
        "blocker_code": "toolchain_unsupported",
        "expected_failure": (
            "a typed intent with start_cmd ('pnpm','start') is accepted as a candidate "
            "(pnpm is in detect._SUPPORTED_NODE_HEADS) and the emitted node image "
            "provisions NO pnpm (no corepack enable, no global install) -> 'pnpm start' "
            "fails at container start, though the SOURCE path fails such a workspace "
            "closed with toolchain_unsupported. Must fail closed the same way (or "
            "actually provision/pin pnpm). Fix = R2."
        ),
    },
    {
        "work_order": "R2/G06",
        "lane": "python-closeout",
        "node_id": (
            "packages/tools/tests/export_track1_closeout/test_g06_intent_output_dir.py"
            "::test_declared_static_output_dir_round_trips_into_sidecar"
        ),
        "boundary": (
            "real ConversationRuntime executing the real ReleaseDeclareTool; "
            "persisted sidecar bytes on disk"
        ),
        "blocker_code": "extra_forbidden",
        "expected_failure": (
            "ReleaseIntent has extra='forbid' and no output_dir field, so a declaration "
            "supplying output_dir='dist' is REJECTED with extra_forbidden and nothing "
            "persists — a static/Vite build's output dir is UNDECLARABLE through the "
            "typed intent though the internal ReleaseService spec DOES interpolate it "
            "into 'COPY --from=build /app/<output_dir>/'. Must round-trip output_dir into "
            "the sidecar. = C8 finding F2. Fix = R2 (add field + lower through the tool)."
        ),
    },
    {
        "work_order": "R3/G07",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_g07_root_persistent_path.py"
            "::test_g07_root_persistent_path_backed_or_fails_closed[root-file-app-db]"
        ),
        "boundary": (
            "real release + /download routes; emitted compose.yaml parsed in-process with PyYAML"
        ),
        "expected_failure": (
            "a resource with persistent_path '/app.db' (parent dir = '/') is accepted "
            "self_host:true but Compose mounts the named volume at /data (the "
            "local_mount_target root-file fallback, carried O1), NOT at '/', so /app.db "
            "lives on the container's EPHEMERAL layer and does not survive a restart "
            "while self_host:true promises it does. Must fail closed with a typed repair "
            "blocker OR emit a volume that actually backs /app.db. Sibling "
            "[root-file-db-sqlite] (2 nodes). = C7 residual O1. Fix = R3."
        ),
    },
    {
        "work_order": "R5/G12",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_g12_appkit_no_heredoc.py"
            "::test_g12_appkit_dockerfile_has_no_heredoc_copy"
        ),
        "boundary": (
            "real release + /download routes; emitted AppKit Dockerfile bytes "
            "read from the download zip"
        ),
        "expected_failure": (
            "the AppKit dev_server overlay (local_compose._dev_server_dockerfile) emits a "
            "heredoc 'COPY <<'DISCO_ENTRYPOINT' …' — a BuildKit/Buildx-only feature — but "
            "the bundle SELFHOST.md + the live guard declare only 'Docker Engine + "
            "Compose v2 plugin', so on a host meeting exactly that prerequisite the image "
            "fails to build. Must emit no heredoc COPY. Fix lands in local_compose, NOT "
            "the DO-NOT-TOUCH appkit generator. = C8 finding F3. Fix = R5."
        ),
    },
    {
        "work_order": "R6/G13",
        "lane": "python-closeout",
        "node_id": (
            "packages/agent-server/tests/export_track1_closeout/test_g13_verifier_truthfulness.py"
            "::test_docker_evidence_absent_engine_must_not_claim_live_production"
        ),
        "boundary": (
            "imports the REAL verify_export_track1_closeout.py module and calls its real "
            "functions on a real tmp filesystem — the code under test, not a mock"
        ),
        "expected_failure": (
            "(a) _write_docker_host_artifacts writes status="
            "'produced_by_live_lane_on_docker_host' even when NO Docker engine ran (the "
            "status string itself lies; only the separate 'available' flag betrays it); "
            "(b) sibling test_frontend_lane_unexecuted_browser_must_block_green — "
            "_run_frontend_lane computes lane.green from vitest+typecheck+build ONLY, so "
            "the Playwright browser e2e is reported green though it never executed, "
            "folding into all_lanes_green -> passed:true. The G13(b) mirror is a curated "
            "always-green vitest subset (c3/c6) so the ONLY reason green could differ is "
            "the un-gated browser lane — exactly the defect. Must not label unproduced "
            "evidence as live. Fix = R6."
        ),
    },
    {
        # acceptance-v4 (R0 reopen): the G08/G11 binding red at the REAL download
        # URL-construction boundary. SELF-DISCRIMINATING and non-rewriteable: it SUPPLIES
        # the binding to the boundary via the predeclared 2-arg contract, so the R4
        # production fix turns it green with NO test edit. INDEPENDENT of panel
        # reachability (no component render).
        "work_order": "R4/G08",
        "lane": "frontend",
        "node_id": (
            "frontend/src/test/export-track1-closeout/g08-download-url-binding.test.tsx"
            "::WO-A (G08) — the download URL binds to the release's version_seq + "
            "spec_digest > carries the SUPPLIED binding ['v7 / g08a-0007'] on the "
            "download URL"
        ),
        "boundary": (
            "the real downloadProject() URL construction (api/projects.ts:187) reached "
            "via the globalThis.__DISCO_ENV config seam + a real recording global fetch; "
            "no panel/component is rendered"
        ),
        "expected_failure": (
            "the test SUPPLIES the self-host binding to the real boundary via the "
            "predeclared future contract downloadProject(cid, {version_seq, spec_digest}) "
            "using a test-side compatibility cast against today's 1-arg implementation. "
            "Today the extra runtime arg is ignored and downloadProject builds a cid-only "
            "/download URL, so the two BOUND cases (materially different fixtures "
            "v7/sha256:g08a-0007 and v42/sha256:g08b-0042, run through the SAME assertions) "
            "FAIL at the exact-value assertion (sp.get('version_seq') === '7'/'42') and the "
            "URL-encoding assertion (raw contains spec_digest=sha256%3A...). The unbound "
            "case (binding=null) PASSES: no version_seq/spec_digest is fabricated. "
            "SELF-DISCRIMINATING: at R4 downloadProject(cid, binding) appends the SUPPLIED "
            "values (URL-encoded) and every case goes green through PRODUCTION ALONE — the "
            "frozen test is never edited (the expected values ARE the test's own inputs, so "
            "two distinct bindings cannot be satisfied by any hardcoded value). Fix = R4."
        ),
    },
    {
        # acceptance-v4 (R0 reopen): the REAL G11 nullable-binding proving red — a
        # dedicated TypeScript COMPILE lane (not vitest transpilation). The frozen
        # contract file is hashed under the frontend/src/test dir glob; the tsconfig is
        # a frozen file. Self-discriminating: the R4 production type fix flips it green
        # with no contract edit.
        "work_order": "R4/G11",
        "lane": "frontend-typecheck",
        "node_id": (
            "frontend/src/test/export-track1-closeout/g11-nullable-binding.contract.ts"
            " (checked by: cd frontend && "
            "npx tsc -p tsconfig.closeout-g11.json --noEmit)"
        ),
        "boundary": (
            "a real tsc --noEmit compile over a dedicated tsconfig (tsconfig.build.json "
            "excludes src/test, and vitest transpiles without type-checking, so a "
            "dedicated config is required); the contract annotates an unsnapshotted "
            "ReleaseResponse with null spec_digest/version_seq/tree_digest"
        ),
        "expected_diagnostics": [
            "src/test/export-track1-closeout/g11-nullable-binding.contract.ts(90,3): "
            "error TS2322: Type 'null' is not assignable to type 'number'.",
            "src/test/export-track1-closeout/g11-nullable-binding.contract.ts(92,3): "
            "error TS2322: Type 'null' is not assignable to type 'string'.",
        ],
        "expected_failure": (
            "the frontend ReleaseResponse types version_seq as number and tree_digest as "
            "string (only spec_digest is nullable), though the backend "
            "(routes/release.py) permits null for all three on an unsnapshotted release. "
            "The command exits NONZERO (exit 2) today with the two TS2322 diagnostics "
            "above (version_seq @90, tree_digest @92); spec_digest @89 raises nothing "
            "since it is already nullable — which is why the pre-existing spec_digest-only "
            "sibling never proved G11. SELF-DISCRIMINATING: the R4 production type "
            "correction (version_seq: number | null; tree_digest: string | null) makes "
            "the command exit 0 with the frozen contract unedited. Fix = R4."
        ),
    },
)


# ---- R0 acceptance remediation ledger (plan Consolidated Remediation Plan) --------
# The independent audit of candidate 581d1fbe found gaps the acceptance-v1 harness
# missed (G01-G19). R0 is the legitimate re-freeze: it LANDS the proving reds against
# 581d1fbe WITHOUT any production change (fixes R1-R6 are PARKED). This block is the
# machine-readable G-ID -> node-ID inventory the reviewer reads back. It is DOCUMENTARY
# (the verifier gates on `files` + `python_closeout_inventory` + `frontend_closeout_
# inventory`, never on this), so every node ID is hand-verified against a real
# `pytest --collect-only` (the machine-truth python_closeout_inventory).
_R0_AGENT = "packages/agent-server/tests/export_track1_closeout"
_R0_TOOLS = "packages/tools/tests/export_track1_closeout"
REMEDIATION_V6: dict[str, object] = {
    "authored": "2026-07-17",
    "supersedes": "acceptance-v5",
    "authority": (
        "Independent C9 review FAILED the acceptance-v5 candidate a1b5025e "
        "(evidence: /var/home/dylan/closeout-evidence-archive/"
        "2026-07-17-export-track1-c9-independent/C9-REVIEW.md). This v6 re-freezes the "
        "legitimately-changed frozen files after the bounded C9-01..C9-06 recovery: "
        "C9-01 (real-LibreOffice G17: OPC relationship-integrity pre-check + pinned "
        "Impress filters + isolated profile; frozen nonlive baseline reconciled to the "
        "exact §3.2 router-skip/appkit-xfail allowlist), C9-04 (a small recognized-shape/"
        "arity uvicorn contract failing closed on incoherent starts), C9-05 (build/"
        "runtime scope-conflict fails closed; a public-ONLY Docker proof of the §12.10 "
        "accepted branch), C9-02 (genuine per-fixture live evidence aggregated + "
        "validated before passed:true), C9-03 (the seven A/E capture regressions are a "
        "governed required lane with a frozen exact node inventory), C9-06 (the "
        "candidate receipt's --python/--base/deletion/evidence-dir bypasses closed with "
        "post-run rechecks). Product source changed ONLY detect.py + heavy_validators.py; "
        "the AppKit generator, session auth, and RBAC were NOT modified."
    ),
    "c9_failure_report": (
        "/var/home/dylan/closeout-evidence-archive/2026-07-17-export-track1-c9-independent/"
        "C9-REVIEW.md"
    ),
    "frozen_byte_changes_v5_to_v6": {
        "packages/agent-server/tests/integration/_closeout_live_support.py": {
            "before_v5": "e34e0f1092a9e31b012199df75df26b373fecd26892a2b3c94e7786358cbc093",
            "after_v6": "a41c379ec8920f383b85ddc6f03186ab5ded3e7b40d85f8a870fa51fac5d2378",
            "why": "C9-02 genuine evidence collection (record_bundle_digest, compose-ps / "
            "inspect / cleanup capture) + C9-05 public-only fixture",
        },
        "packages/agent-server/tests/integration/test_export_track1_closeout_live.py": {
            "before_v5": "920388773bcd14638d2562e813dfa40c835ca163b51433ec3b62c1ce3c4e1676",
            "after_v6": "4dba6fbdb813e31932442784eb10bdf38eb1c59807b323400552d8baff63b7a6",
            "why": "C9-02 record_bundle_digest calls + slug->family map; C9-05 public-only "
            "live test",
        },
        "packages/agent-server/tests/integration/test_closeout_live_capture_regression.py": {
            "before_v5": "55bdd6c0357bb9a260fbb5f23e346605200b0bdf03ec0ea8ed320aeeb68bed31",
            "after_v6": "d8db9e62b265bd98842a10af1506cf9a783fe6cce04e1c5f37d1b8f6a1dc192a",
            "why": "C9-03 export_track1_closeout marker so the capture lane is governed",
        },
        "scripts/verify_export_track1_closeout.py": {
            "before_v5": "5b22ef0973888d95817dc02fe9e42392063e1076e964eb249089c53a6d8ae37d",
            "after_v6": "c9b3bbe665be4eccfefee8f6d898958f7ec4aee183f238d5ae7ac365e3189f8c",
            "why": "C9-01 nonlive baseline allowlist; C9-02 live-evidence aggregate+"
            "validate gate; C9-03 governed capture lane",
        },
        "scripts/gen_closeout_acceptance_manifest.py": {
            "before_v5": "866140128a416a8a1c31ca30ff0945529cdf5a69fa31440ff611cbaa358ba9b8",
            "after_v6": "(self-referential: see the files map of this manifest)",
            "why": "acceptance-v6 authoring: tag, this remediation_v6 record, the "
            "governed_capture_inventory + live_capture command, C9-03 collector",
        },
    },
    "ratification_status": (
        "acceptance-v6 is a CANDIDATE for independent human review. No human has "
        "reviewed, signed, or protected any v6 tag; a v5 tag was never signed either. "
        "Ratification succeeds only when a human creates the signed annotated "
        "export-track1-closeout-acceptance-v6 tag and publishes it to the protected "
        "authoritative remote. This authoring claims NO human ratification and moves no "
        "tag."
    ),
}


REMEDIATION_V5: dict[str, object] = {
    "authored": "2026-07-17",
    "authority": (
        "Owner adjudication of 2026-07-17 (recovery campaign, evidence archive "
        "2026-07-17-export-track1-recovery/owner-adjudication/): ruling A (exec_env "
        "capture exemption), ruling B option (a) (§12.6 drives the generated app's "
        "documented session-auth model: register via Bearer ADMIN_TOKEN -> login -> "
        "session cookie -> record write -> unauth 401 -> restart read-back -> "
        "down/up persistence + idempotent migration), ruling E (topology rendering is "
        "the STRUCTURAL model via `config --no-interpolate --no-env-resolution "
        "--format json`, kept recorded and swept; `config --quiet` unchanged on the "
        "real .env). The generator/AppKit RBAC/session auth were NOT modified."
    ),
    "frozen_byte_changes_v4_to_v5": {
        "packages/agent-server/tests/integration/_closeout_live_support.py": {
            "before_v4": "ca596d4708bc78742abcc641f95391c9f0015e315410b27ccf45373d15b4c3ba",
            "after_v5": "e34e0f1092a9e31b012199df75df26b373fecd26892a2b3c94e7786358cbc093",
            "why": "rulings A + E: compose(record=) with exec_env as the SOLE "
            "record=False caller (probe asserts its own success); config_json "
            "renders the structural model; http_response/session_cookie_from "
            "helpers; appkit_record_plan additionally derives a valid role",
        },
        "packages/agent-server/tests/integration/test_export_track1_closeout_live.py": {
            "before_v4": "42dbd6c14ad689c89c9287e410d04874dc0f9b03499b51bfd2da05b85d09a546",
            "after_v5": "920388773bcd14638d2562e813dfa40c835ca163b51433ec3b62c1ce3c4e1676",
            "why": "ruling B(a): AppKit §12.6 exercises the documented session-auth "
            "model end-to-end; docstrings updated to match",
        },
        "packages/agent-server/tests/integration/test_closeout_live_capture_regression.py": {
            "before_v4": None,
            "after_v5": "55bdd6c0357bb9a260fbb5f23e346605200b0bdf03ec0ea8ed320aeeb68bed31",
            "why": "NEW frozen file: the seven ruled regressions for A + E (incl. the "
            "verifier-required AST source-shape pin, VERDICT-ABE §6)",
        },
        "scripts/gen_closeout_acceptance_manifest.py": {
            "before_v4": "4637fceff6d622d351cbf3fb89ede4d5ed709a3b1ef8867f297d925c96669fd8",
            "after_v5": "(self-referential: see the files map of this manifest)",
            "why": "acceptance-v5 authoring: tag, this remediation_v5 record, and the "
            "new frozen regression file",
        },
    },
    "live_lane_result_at_authoring": (
        "frozen R7 lane 7 passed / 0 failed on Docker 29.6.1 / Compose v5.3.1 "
        "(express, fastapi, imported-node, vite-static, appkit, public-build-env vite, "
        "availability)"
    ),
    "ratification_status": (
        "acceptance-v5 is a CANDIDATE for independent human review. No human has "
        "reviewed, signed, or protected any v5 tag; ratification succeeds only when a "
        "human creates the signed annotated export-track1-closeout-acceptance-v5 tag "
        "and publishes it to the protected authoritative remote."
    ),
}


REMEDIATION_R0: dict[str, object] = {
    "acceptance_version": "v4",
    "base_candidate": "581d1fbe",
    "plan_doc": "docs/export-track1-closeout-remediation-plan.md",
    "ratification_status": (
        "[HISTORICAL R0 RECORD — superseded by acceptance-v5; see remediation_v5.] "
        "acceptance-v4 is a CANDIDATE for independent human review. No human has reviewed, "
        "signed, or protected any acceptance tag in this campaign; v2/v3 are annotated but "
        "unsigned, local-only, and not protected; v1 was lightweight. R0 is NOT complete "
        "until an independent human inspects the semantic diff, reruns the gates, creates a "
        "signed annotated export-track1-closeout-acceptance-v4 tag, and publishes it to the "
        "protected authoritative remote. This manifest asserts NO human ratification has "
        "occurred."
    ),
    "summary": (
        "R0 legitimate re-freeze after the independent audit (gaps G01-G19). Lands the "
        "proving reds against 581d1fbe with NO production change; the R1-R6 production "
        "fixes are PARKED. acceptance-v1 (G01) was a LIGHTWEIGHT tag moved AFTER C1-C6; "
        "v2 and v3 are ANNOTATED reviewer(subagent)-created tags on acceptance-only "
        "(production-free) commits preceding all remediation, but are UNSIGNED, LOCAL-ONLY, "
        "and NOT PROTECTED. G19 fixed: the frozen frontend gate targets "
        "e2e/export-track1-closeout/** (a directory). G10 harness contradiction fixed. "
        "v4 (R0 reopen, v2/v3 tags unmoved) makes three residual harness weaknesses "
        "correct: (a) the G08 binding red is now SELF-DISCRIMINATING and non-rewriteable "
        "(it SUPPLIES the binding via the predeclared 2-arg contract, so R4 turns it green "
        "through production alone), (b) a REAL G11 compile red (proving_reds_compile) via a "
        "dedicated tsconfig proves the version_seq/tree_digest nullability gap the "
        "spec_digest-only sibling never did, (c) the evidence-hygiene gate is now "
        "REGRESSION-PROOF via committed mutation tests. Ruff/format zeroed; the canonical "
        "full remediation plan is committed."
    ),
    "proving_reds": [
        f"{_R0_AGENT}/test_g02_positional_credential.py"
        "::test_positional_credential_rejected_at_release_declare[leading]",
        f"{_R0_AGENT}/test_g02_positional_credential.py"
        "::test_positional_credential_rejected_at_release_declare[middle]",
        f"{_R0_AGENT}/test_g02_positional_credential.py"
        "::test_positional_credential_rejected_at_release_declare[trailing]",
        f"{_R0_AGENT}/test_g02_positional_credential.py"
        "::test_positional_credential_does_not_ship_through_release_download[leading]",
        f"{_R0_AGENT}/test_g02_positional_credential.py"
        "::test_positional_credential_does_not_ship_through_release_download[middle]",
        f"{_R0_AGENT}/test_g02_positional_credential.py"
        "::test_positional_credential_does_not_ship_through_release_download[trailing]",
        f"{_R0_AGENT}/test_g04_interpreted_install.py"
        "::test_interpreted_candidate_with_deps_emits_dependency_install_layer[express_node_no_build]",
        f"{_R0_AGENT}/test_g04_interpreted_install.py"
        "::test_interpreted_candidate_with_deps_emits_dependency_install_layer[fastapi_python_no_build]",
        f"{_R0_AGENT}/test_g05_typed_pnpm_start.py"
        "::test_typed_pnpm_start_intent_is_rejected_or_provisions_pnpm",
        f"{_R0_TOOLS}/test_g06_intent_output_dir.py"
        "::test_declared_static_output_dir_round_trips_into_sidecar",
        f"{_R0_AGENT}/test_g07_root_persistent_path.py"
        "::test_g07_root_persistent_path_backed_or_fails_closed[root-file-app-db]",
        f"{_R0_AGENT}/test_g07_root_persistent_path.py"
        "::test_g07_root_persistent_path_backed_or_fails_closed[root-file-db-sqlite]",
        f"{_R0_AGENT}/test_g12_appkit_no_heredoc.py"
        "::test_g12_appkit_dockerfile_has_no_heredoc_copy",
        f"{_R0_AGENT}/test_g13_verifier_truthfulness.py"
        "::test_docker_evidence_absent_engine_must_not_claim_live_production",
        f"{_R0_AGENT}/test_g13_verifier_truthfulness.py"
        "::test_frontend_lane_unexecuted_browser_must_block_green",
    ],
    "proving_reds_frontend": [
        "frontend/src/test/export-track1-closeout/g08-download-url-binding.test.tsx"
        "::WO-A (G08) — the download URL binds to the release's version_seq + spec_digest"
        " > carries the SUPPLIED binding [v7 / g08a-0007] on the download URL",
        "frontend/src/test/export-track1-closeout/g08-download-url-binding.test.tsx"
        "::WO-A (G08) — the download URL binds to the release's version_seq + spec_digest"
        " > carries the SUPPLIED binding [v42 / g08b-0042] on the download URL",
    ],
    "frontend_inventory_enforcement": (
        "The two load-bearing G08 bound nodes are now EXPLICIT it(...) cases with FULLY "
        "LITERAL titles (not it.each(...), which the source-parsing regex silently omitted "
        "from frontend_closeout_inventory), so the frozen inventory records exactly nine "
        "frontend leaf titles: six existing C3/C6 tests + the two bound G08 tests + the one "
        "unbound G08 test. The verifier no longer compares only test-FILE basenames: "
        "_parse_vitest extracts, per file, the set of EXECUTED (passed|failed) leaf titles "
        "(skipped/pending/todo are excluded, so a non-executed node reads as unreported) and "
        "_diff_frontend_titles (the single source of truth _run_frontend_lane uses) diffs "
        "them against the frozen `tests` list. Any missing, renamed, skipped, unreported, or "
        "extra title makes the frontend inventory check and lane NON-green — filename-set "
        "parity alone is insufficient. Proven regression-tight by the committed "
        "test_frontend_inventory_enforcement_regression.py (drop / rename / same-count swap / "
        "extra / skipped all rejected; exact match accepted; lane delegates to the helper)."
    ),
    "proving_reds_compile": [
        {
            "contract": "frontend/src/test/export-track1-closeout/g11-nullable-binding.contract.ts",
            "command": "cd frontend && npx tsc -p tsconfig.closeout-g11.json --noEmit",
            "expected_exit": 2,
            "expected_diagnostics": [
                "g11-nullable-binding.contract.ts(90,3): error TS2322: "
                "Type 'null' is not assignable to type 'number'.",
                "g11-nullable-binding.contract.ts(92,3): error TS2322: "
                "Type 'null' is not assignable to type 'string'.",
            ],
            "turns_green_at": "R4 (version_seq: number | null; tree_digest: string | null)",
        },
    ],
    "evidence_hygiene": (
        "G02's planted credential must never reach the acceptance evidence. The G02 test "
        "redacts every failure-message interpolation of the sentinel AND parametrizes on "
        "the position id (not the sentinel-bearing argv) so pytest's own funcarg repr "
        "cannot leak it; verify_export_track1_closeout.py has an evidence-hygiene gate that "
        "scans every written text artifact (JUnit XMLs, vitest json, anti-bypass scan, "
        "docker-versions, docker-host artifacts) for the registered marker and fails closed "
        "into `passed` (via _final_verdict) if it appears; it stores only the non-secret "
        "marker, never the literal. REGRESSION-PROOF via committed mutation tests: "
        "test_evidence_hygiene_regression.py proves per-artifact-class mutation trips the "
        "scan, the violation records only file/line/label (never the value), a hygiene "
        "violation forces the final verdict false, clean evidence is accepted, and the real "
        "G02 proving-red JUnit XML, stdout, AND stderr each independently contain zero marker "
        "and zero full-sentinel occurrences (capture_output=True splits stdout/stderr, so all "
        "three surfaces are scanned separately via the committed _evidence_surfaces helper; a "
        "planted-only-in-stderr regression pins the stderr lane) — without the regression "
        "test writing the full credential itself."
    ),
    "r4_activated_deferred": {
        "G08_e2e": (
            "frontend/e2e/export-track1-closeout/selfhost-download-binding.spec.ts — the "
            "self-host download-binding e2e. RED at reachability today (the candidate/"
            "needs-review SelfHostPanel is unreachable offline, same wall as G09); its "
            "version_seq+spec_digest binding teeth activate once R4 restores reachability. "
            "The binding itself is now ALSO proven directly by proving_reds_frontend "
            "(g08-download-url-binding.test.tsx), which fails at the URL boundary "
            "INDEPENDENT of panel reachability. The G11 null-binding concern is covered by "
            "that vitest's unbound-release guard + the R4 types/release.ts:73-74 "
            "nullability fix."
        ),
    },
    "record_only": {
        "G09_e2e_reachability": (
            "4 Firefox e2e reachability reds (candidate x2 + needs-review x2 SelfHostPanel "
            "unreachable in Build+Agent offline; the not-web path is reachable and passes) "
            "— confirmed via live Firefox. Activates at R4."
        ),
        "G14_live_matrix": (
            "the 7-node frozen live Docker matrix (integration-marked): 2 pass / 5 fail on "
            "581d1fbe (the C8 live run — first time the frozen live lane ever executed). "
            "Re-run to 7/7 is R7."
        ),
    },
    "ratified_baseline_suppression": (
        "packages/agent-server/tests/export_track1_closeout/test_c2_bound_download.py:349 "
        "'# noqa: BLE001' (G18) — the SINGLE ratified pre-existing suppression; it "
        "strengthens the test (catches a churner crash). No new suppressions added."
    ),
    "parked_production_fixes": (
        "R1(G02) R2(G03-G06) R3(G07) R4(G08-G11) R5(G12) R6(G13,G18,G19) R7(G14) R8(G15-G17)"
    ),
}


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
