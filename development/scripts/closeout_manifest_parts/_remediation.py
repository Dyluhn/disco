"""Frozen acceptance remediation ledgers (R0/v5/v6) — extracted from :mod:`._data`.

Split out solely so no single frozen-data module exceeds the 700-logical-line
module budget; ``_data`` re-exports these names so
``gen_closeout_acceptance_manifest`` keeps one import surface. The VALUES here
are FROZEN ACCEPTANCE IDENTITY and must stay byte-identical.
"""

from __future__ import annotations

# ---- R0 acceptance remediation ledger (plan Consolidated Remediation Plan) --------
# The independent audit of candidate 581d1fbe found gaps the acceptance-v1 harness
# missed (G01-G19). R0 is the legitimate re-freeze: it LANDS the proving reds against
# 581d1fbe WITHOUT any production change (fixes R1-R6 are PARKED). This block is the
# machine-readable G-ID -> node-ID inventory the reviewer reads back. It is DOCUMENTARY
# (the verifier gates on `files` + `python_closeout_inventory` + `frontend_closeout_
# inventory`, never on this), so every node ID is hand-verified against a real
# `pytest --collect-only` (the machine-truth python_closeout_inventory).
_R0_AGENT = "current/packages/agent-server/tests/export_track1_closeout"
_R0_TOOLS = "current/packages/tools/tests/export_track1_closeout"
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
        "current/packages/agent-server/tests/integration/_closeout_live_support.py": {
            "before_v5": "e34e0f1092a9e31b012199df75df26b373fecd26892a2b3c94e7786358cbc093",
            "after_v6": "a41c379ec8920f383b85ddc6f03186ab5ded3e7b40d85f8a870fa51fac5d2378",
            "why": "C9-02 genuine evidence collection (record_bundle_digest, compose-ps / "
            "inspect / cleanup capture) + C9-05 public-only fixture",
        },
        "current/packages/agent-server/tests/integration/test_export_track1_closeout_live.py": {
            "before_v5": "920388773bcd14638d2562e813dfa40c835ca163b51433ec3b62c1ce3c4e1676",
            "after_v6": "4dba6fbdb813e31932442784eb10bdf38eb1c59807b323400552d8baff63b7a6",
            "why": "C9-02 record_bundle_digest calls + slug->family map; C9-05 public-only "
            "live test",
        },
        "current/packages/agent-server/tests/integration/test_closeout_live_capture_regression.py": {
            "before_v5": "55bdd6c0357bb9a260fbb5f23e346605200b0bdf03ec0ea8ed320aeeb68bed31",
            "after_v6": "d8db9e62b265bd98842a10af1506cf9a783fe6cce04e1c5f37d1b8f6a1dc192a",
            "why": "C9-03 export_track1_closeout marker so the capture lane is governed",
        },
        "development/scripts/verify_export_track1_closeout.py": {
            "before_v5": "5b22ef0973888d95817dc02fe9e42392063e1076e964eb249089c53a6d8ae37d",
            "after_v6": "c9b3bbe665be4eccfefee8f6d898958f7ec4aee183f238d5ae7ac365e3189f8c",
            "why": "C9-01 nonlive baseline allowlist; C9-02 live-evidence aggregate+"
            "validate gate; C9-03 governed capture lane",
        },
        "development/scripts/gen_closeout_acceptance_manifest.py": {
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
        "current/packages/agent-server/tests/integration/_closeout_live_support.py": {
            "before_v4": "ca596d4708bc78742abcc641f95391c9f0015e315410b27ccf45373d15b4c3ba",
            "after_v5": "e34e0f1092a9e31b012199df75df26b373fecd26892a2b3c94e7786358cbc093",
            "why": "rulings A + E: compose(record=) with exec_env as the SOLE "
            "record=False caller (probe asserts its own success); config_json "
            "renders the structural model; http_response/session_cookie_from "
            "helpers; appkit_record_plan additionally derives a valid role",
        },
        "current/packages/agent-server/tests/integration/test_export_track1_closeout_live.py": {
            "before_v4": "42dbd6c14ad689c89c9287e410d04874dc0f9b03499b51bfd2da05b85d09a546",
            "after_v5": "920388773bcd14638d2562e813dfa40c835ca163b51433ec3b62c1ce3c4e1676",
            "why": "ruling B(a): AppKit §12.6 exercises the documented session-auth "
            "model end-to-end; docstrings updated to match",
        },
        "current/packages/agent-server/tests/integration/test_closeout_live_capture_regression.py": {
            "before_v4": None,
            "after_v5": "55bdd6c0357bb9a260fbb5f23e346605200b0bdf03ec0ea8ed320aeeb68bed31",
            "why": "NEW frozen file: the seven ruled regressions for A + E (incl. the "
            "verifier-required AST source-shape pin, VERDICT-ABE §6)",
        },
        "development/scripts/gen_closeout_acceptance_manifest.py": {
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
    "plan_doc": "current/docs/export-track1-closeout-remediation-plan.md",
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
        "current/frontend/src/test/export-track1-closeout/g08-download-url-binding.test.tsx"
        "::WO-A (G08) — the download URL binds to the release's version_seq + spec_digest"
        " > carries the SUPPLIED binding [v7 / g08a-0007] on the download URL",
        "current/frontend/src/test/export-track1-closeout/g08-download-url-binding.test.tsx"
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
            "contract": "current/frontend/src/test/export-track1-closeout/g11-nullable-binding.contract.ts",
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
            "current/frontend/e2e/export-track1-closeout/selfhost-download-binding.spec.ts — the "
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
        "current/packages/agent-server/tests/export_track1_closeout/test_c2_bound_download.py:349 "
        "'# noqa: BLE001' (G18) — the SINGLE ratified pre-existing suppression; it "
        "strengthens the test (catches a churner crash). No new suppressions added."
    ),
    "parked_production_fixes": (
        "R1(G02) R2(G03-G06) R3(G07) R4(G08-G11) R5(G12) R6(G13,G18,G19) R7(G14) R8(G15-G17)"
    ),
}
