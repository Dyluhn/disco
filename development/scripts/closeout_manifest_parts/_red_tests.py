"""Frozen red-test inventory — extracted from :mod:`._data`.

Split out solely so no single frozen-data module exceeds the 700-logical-line
module budget; ``_data`` re-exports ``RED_TESTS`` so
``gen_closeout_acceptance_manifest`` keeps one import surface. The VALUES here
are FROZEN ACCEPTANCE IDENTITY and must stay byte-identical.
"""

from __future__ import annotations

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
        "boundary": "real DefaultToolExecutor via ConversationRuntime.execute_disco_tool",
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
            "real release_declare ToolExecutor (execute_disco_tool) + real release route "
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
