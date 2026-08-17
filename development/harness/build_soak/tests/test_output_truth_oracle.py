"""OutputTruthOracle unit tests (guidelines §11.5, PR S2)."""

from __future__ import annotations

import pytest
from _eventlog import clean_smoke_log

from harness.build_soak.events import normalize_events
from harness.build_soak.oracles.output_truth import OutputTruthOracle

_SCN = {
    "id": "static_html_minimal",
    "assertions": {
        "workspace": {"files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}]},
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}


def _run(
    scenario=_SCN,
    workspace=None,
    preview=None,
    events=None,
    verified_claim_results=None,
    verified_observed_facts=None,
    verified_artifact_paths=None,
):
    return OutputTruthOracle().check(
        normalize_events(events or clean_smoke_log()),
        scenario=scenario,
        workspace_manifest=workspace,
        preview=preview,
        verified_claim_results=verified_claim_results,
        verified_observed_facts=verified_observed_facts,
        verified_artifact_paths=verified_artifact_paths,
    )


def test_finished_with_required_file_and_content_passes():
    results = _run(workspace={"index.html": "<h1>Build Smoke OK</h1>"})
    assert results[0].passed, results[0].to_dict()


def test_finished_with_missing_file_is_false_finish():
    results = _run(workspace={"other.html": "x"})
    assert results[0].code == "FALSE_FINISH_NO_OUTPUT"
    assert results[0].facts["required_path"] == "index.html"


def test_finished_with_missing_content_is_artifact_mismatch():
    results = _run(workspace={"index.html": "<h1>nope</h1>"})
    assert results[0].code == "ARTIFACT_TRUTH_MISMATCH"
    assert results[0].facts["missing_substring"] == "Build Smoke OK"


def test_exact_paths_rejects_unexpected_agent_created_product_file():
    scenario = {
        "id": "exact_three_file_shape",
        "assertions": {
            "workspace": {
                "exact_paths": ["index.html", "styles.css", "app.js"],
                "files": [
                    {"path": "index.html", "must_contain": ["Build Smoke OK"]},
                    {"path": "styles.css"},
                    {"path": "app.js"},
                ],
            },
            "terminal_status_in": ["FINISHED"],
        },
    }
    workspace = {
        ".disco/context/current_goal.md": "host state",
        ".pmx/screenshots/0001.png": {"content": "", "size": 3},
        "index.html": "<h1>Build Smoke OK</h1>",
        "styles.css": "body {}",
        "app.js": "console.log('ok')",
        "pricing.css": ".pricing {}",
    }

    result = _run(scenario=scenario, workspace=workspace)[0]

    assert result.code == "ARTIFACT_TRUTH_MISMATCH"
    assert result.first_broken_link == "finish -> exact_workspace_shape"
    assert result.facts == {
        "expected_paths": ["app.js", "index.html", "styles.css"],
        "missing_paths": [],
        "unexpected_paths": ["pricing.css"],
    }


def test_exact_paths_ignores_only_host_owned_namespaces_and_passes_exact_product_shape():
    scenario = {
        "id": "exact_arbitrary_shape",
        "assertions": {
            "workspace": {
                "exact_paths": ["src/main.rs", ".env.example"],
                "files": [{"path": "src/main.rs", "must_contain": ["fn main"]}],
            },
            "terminal_status_in": ["FINISHED"],
        },
    }
    workspace = {
        ".disco/context/todo.md": "host state",
        ".pmx/job.json": "{}",
        ".env.example": "PORT=8080\n",
        "src/main.rs": "fn main() {}\n",
    }

    result = _run(scenario=scenario, workspace=workspace)[0]

    assert result.passed, result.to_dict()


def test_exact_paths_treats_arbitrary_dotfile_as_product_output():
    scenario = {
        "id": "no_unlisted_dotfiles",
        "assertions": {
            "workspace": {"exact_paths": ["index.html"]},
            "terminal_status_in": ["FINISHED"],
        },
    }

    result = _run(
        scenario=scenario,
        workspace={"index.html": "ok", ".agent-created": "not host owned"},
    )[0]

    assert result.code == "ARTIFACT_TRUTH_MISMATCH"
    assert result.facts["unexpected_paths"] == [".agent-created"]


def test_exact_paths_missing_member_is_false_finish():
    scenario = {
        "id": "missing_exact_member",
        "assertions": {
            "workspace": {"exact_paths": ["index.html", "app.js"]},
            "terminal_status_in": ["FINISHED"],
        },
    }

    result = _run(scenario=scenario, workspace={"index.html": "ok"})[0]

    assert result.code == "FALSE_FINISH_NO_OUTPUT"
    assert result.first_broken_link == "finish -> exact_workspace_shape"
    assert result.facts == {
        "expected_paths": ["app.js", "index.html"],
        "missing_paths": ["app.js"],
        "unexpected_paths": [],
    }


def test_without_exact_paths_preserves_open_file_set_semantics():
    workspace = {
        "index.html": "<h1>Build Smoke OK</h1>",
        "legitimate-extra.css": "body {}",
    }

    result = _run(workspace=workspace)[0]

    assert result.passed, result.to_dict()


def test_governed_nested_application_root_satisfies_relative_file_contract():
    scenario = {
        "id": "nested-target",
        "assertions": {
            "workspace": {
                "files": [
                    {"path": "package.json", "must_contain": ["react", "vite"]},
                    {"path": "index.html"},
                ]
            },
            "terminal_status_in": ["FINISHED"],
        },
    }
    workspace = {
        "react-continue/package.json": '{"dependencies":{"react":"latest","vite":"latest"}}',
        "react-continue/index.html": "<main id='root'></main>",
    }

    result = _run(
        scenario=scenario,
        workspace=workspace,
        verified_artifact_paths=["react-continue/index.html"],
    )[0]

    assert result.passed, result.to_dict()
    assert result.facts["assertion_root"] == "react-continue"


def test_untrusted_nested_files_do_not_move_the_assertion_root():
    workspace = {"unrelated/index.html": "<h1>Build Smoke OK</h1>"}

    result = _run(workspace=workspace)[0]

    assert result.code == "FALSE_FINISH_NO_OUTPUT"
    assert result.facts["required_path"] == "index.html"


def test_governed_root_wins_over_stale_workspace_root_scaffold():
    workspace = {
        "package.json": '{"name":"stale"}',
        "index.html": "stale scaffold",
        "react-continue/package.json": '{"dependencies":{"react":"latest","vite":"latest"}}',
        "react-continue/index.html": "<h1>current app</h1>",
    }
    scenario = {
        "id": "canonical-target-wins",
        "assertions": {
            "workspace": {
                "files": [
                    {"path": "package.json", "must_contain": ["react", "vite"]},
                    {"path": "index.html", "must_contain": ["current app"]},
                ]
            },
            "terminal_status_in": ["FINISHED"],
        },
    }

    result = _run(
        scenario=scenario,
        workspace=workspace,
        verified_artifact_paths=["react-continue/index.html"],
    )[0]

    assert result.passed, result.to_dict()
    assert result.facts["assertion_root"] == "react-continue"


def test_two_viable_governed_application_roots_fail_closed():
    scenario = {
        "id": "ambiguous-target",
        "assertions": {
            "workspace": {"files": [{"path": "index.html"}]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    workspace = {
        "one/index.html": "one",
        "two/index.html": "two",
    }

    result = _run(
        scenario=scenario,
        workspace=workspace,
        verified_artifact_paths=["one/index.html", "two/index.html"],
    )[0]

    assert result.code == "WORKSPACE_SNAPSHOT_UNVERIFIED"
    assert result.facts["viable_verified_roots"] == ["one", "two"]


@pytest.mark.parametrize("path", ["/workspace/index.html", "../index.html", "app/../index.html"])
def test_unsafe_governed_artifact_path_never_becomes_a_lookup_root(path):
    result = _run(
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        verified_artifact_paths=[path],
    )[0]

    assert result.code == "WORKSPACE_SNAPSHOT_UNVERIFIED"
    assert result.first_broken_link == "governed_artifact -> output_contract_root"


def test_root_qualified_contract_falls_back_to_workspace_root():
    scenario = {
        "id": "root-qualified-target",
        "assertions": {
            "workspace": {
                "files": [
                    {"path": ".disco/appspec.json", "must_contain": ["fixture"]},
                    {"path": ".disco/designspec.json"},
                ]
            },
            "terminal_status_in": ["FINISHED"],
        },
    }
    workspace = {
        ".disco/appspec.json": '{"name":"fixture"}',
        ".disco/designspec.json": "{}",
    }

    result = _run(
        scenario=scenario,
        workspace=workspace,
        verified_artifact_paths=[".disco/appspec.json"],
    )[0]

    assert result.passed, result.to_dict()
    assert result.facts["assertion_root"] == "."


def test_directory_shaped_artifact_can_anchor_target_relative_assertions():
    scenario = {
        "id": "future-native-bundle",
        "assertions": {
            "workspace": {
                "files": [{"path": "Info.plist", "must_contain": ["CFBundleIdentifier"]}]
            },
            "terminal_status_in": ["FINISHED"],
        },
    }
    workspace = {
        "build/Fixture.app/Info.plist": "<key>CFBundleIdentifier</key>",
        "build/Fixture.app/Fixture": "binary",
    }

    result = _run(
        scenario=scenario,
        workspace=workspace,
        verified_artifact_paths=["build/Fixture.app"],
    )[0]

    assert result.passed, result.to_dict()
    assert result.facts["assertion_root"] == "build/Fixture.app"


def test_no_output_assertion_skips():
    results = _run(scenario={"id": "x", "assertions": {}})
    assert results[0].skipped


# ---- HARNESS-FIX-4 rev2: proof + content-stability precedence fold -----------------------

_SCN2 = {
    "id": "two_files",
    "assertions": {
        "workspace": {
            "files": [
                {"path": "a.html", "must_contain": ["AAA"]},
                {"path": "b.html", "must_contain": ["BBB"]},
            ]
        },
        "terminal_status_in": ["FINISHED"],
    },
}

_CHECK_MATRIX = [
    (
        "must_contain",
        {"must_contain": ["TARGET"]},
        "plain text",
        "missing_substring",
        "TARGET",
    ),
    (
        "must_not_contain",
        {"must_not_contain": ["SECRET"]},
        "has SECRET inside",
        "forbidden_substring",
        "SECRET",
    ),
    (
        "equals",
        {"equals": "TARGET"},
        "different",
        None,
        None,
    ),
]

_AUTHORITY_MATRIX = [
    (
        "authoritative-proof",
        {"proof": "raw_sha", "content_stable": False},
        "ARTIFACT_TRUTH_MISMATCH",
        "raw_sha",
        False,
    ),
    (
        "stable-unproven",
        {"proof": "unproven_extended_stability", "content_stable": True},
        "ARTIFACT_TRUTH_MISMATCH",
        "unproven_extended_stability",
        True,
    ),
    (
        "churning-unproven",
        {"proof": "unproven_extended_stability", "content_stable": False},
        "WORKSPACE_SNAPSHOT_UNVERIFIED",
        "unproven_extended_stability",
        False,
    ),
    (
        "unknown+stable",
        {"proof": "unknown", "content_stable": True},
        "ARTIFACT_TRUTH_MISMATCH",
        "unknown",
        True,
    ),
    (
        "unknown+churning",
        {"proof": "unknown", "content_stable": False},
        "WORKSPACE_SNAPSHOT_UNVERIFIED",
        "unknown",
        False,
    ),
    (
        "legacy-no-field",
        {},
        "ARTIFACT_TRUTH_MISMATCH",
        None,
        False,
    ),
]


def _scenario_for_file_spec(spec):
    return {
        "id": "content_check",
        "assertions": {
            "workspace": {"files": [{"path": "index.html", **spec}]},
            "terminal_status_in": ["FINISHED"],
        },
    }


@pytest.mark.parametrize(
    ("check", "spec", "bad_content", "evidence_key", "evidence_value"), _CHECK_MATRIX
)
@pytest.mark.parametrize(
    ("authority_case", "entry_fields", "expected_code", "expected_proof", "expected_stable"),
    _AUTHORITY_MATRIX,
)
def test_content_mismatch_authority_matrix(
    check,
    spec,
    bad_content,
    evidence_key,
    evidence_value,
    authority_case,
    entry_fields,
    expected_code,
    expected_proof,
    expected_stable,
):
    del authority_case
    ws = {"index.html": {"content": bad_content, **entry_fields}}
    results = _run(scenario=_scenario_for_file_spec(spec), workspace=ws)

    assert results[0].code == expected_code
    mismatch = results[0].facts["mismatches"][0]
    assert mismatch["check"] == check
    assert mismatch["proof"] == expected_proof
    assert mismatch["content_stable"] is expected_stable
    if evidence_key is not None:
        assert mismatch[evidence_key] == evidence_value


def test_mixed_multi_file_authoritative_mismatch_wins():
    # PRECEDENCE: one authoritative mismatch (b.html raw_sha) + one churning-unproven (a.html)
    # → the authoritative regression wins; both are recorded as evidence.
    ws = {
        "a.html": {
            "content": "x",
            "proof": "unproven_extended_stability",
            "content_stable": False,
        },
        "b.html": {"content": "x", "proof": "raw_sha", "content_stable": False},
    }
    results = _run(scenario=_SCN2, workspace=ws)
    assert results[0].code == "ARTIFACT_TRUTH_MISMATCH"
    assert results[0].facts["proven_mismatch_count"] == 1
    assert results[0].facts["unverified_mismatch_count"] == 1
    assert {m["proof"] for m in results[0].facts["mismatches"]} == {
        "unproven_extended_stability",
        "raw_sha",
    }


def test_all_churning_unproven_multi_file_mismatch_is_unverified():
    ws = {
        "a.html": {
            "content": "x",
            "proof": "unproven_extended_stability",
            "content_stable": False,
        },
        "b.html": {"content": "x", "proof": "unknown", "content_stable": False},
    }
    results = _run(scenario=_SCN2, workspace=ws)
    assert results[0].code == "WORKSPACE_SNAPSHOT_UNVERIFIED"
    assert len(results[0].facts["mismatches"]) == 2


def test_stable_unproven_content_present_still_passes():
    # Correct content passes even when sha identity is unproven; stability only promotes
    # mismatches, it does not create a failure on matching content.
    ws = {
        "index.html": {
            "content": "<h1>Build Smoke OK</h1>",
            "proof": "unproven_extended_stability",
            "content_stable": True,
        }
    }
    assert _run(workspace=ws)[0].passed


def test_preview_404_is_false_finish():
    scenario = {
        "id": "p",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Build Smoke OK"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    results = _run(scenario=scenario, preview={"health": {"status": 404}, "content": ""})
    assert results[0].code == "FALSE_FINISH_PREVIEW_BROKEN"


def test_preview_content_mismatch():
    scenario = {
        "id": "p",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Grand Opening"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    preview = {"health": {"status": 200}, "content": "<h1>old</h1>"}
    results = _run(scenario=scenario, preview=preview)
    assert results[0].code == "PREVIEW_TRUTH_MISMATCH"


def test_governed_visible_text_claim_proves_client_rendered_preview_content():
    scenario = {
        "id": "client-rendered",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Live steer 440023"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    preview = {
        "health": {"status": 200},
        "content": "<div id='root'></div><script src='/assets/app.js'></script>",
    }
    results = _run(
        scenario=scenario,
        preview=preview,
        verified_claim_results=[
            {
                "kind": "visible_text",
                "expected": "Live steer 440023",
                "status": "pass",
                "evidence_modalities": ["dom_accessibility"],
            }
        ],
    )

    assert results[0].passed, results[0].to_dict()


def test_current_host_observed_fact_proves_text_not_declared_as_product_claim():
    scenario = {
        "id": "host-observed-target-text",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Node Paused 403113"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    results = _run(
        scenario=scenario,
        preview={"health": {"status": 200}, "content": ""},
        verified_claim_results=[{"kind": "rendered_content", "expected": "", "status": "pass"}],
        verified_observed_facts=[
            {
                "fact_id": "web.observed.visible_text",
                "kind": "visible_text",
                "value": "Node Paused 403113",
                "verifier_id": "disco.host_web_verifier@1",
                "evidence_modalities": ["dom_accessibility"],
            }
        ],
    )

    assert results[0].passed, results[0].to_dict()


def test_governed_claim_set_never_falls_back_to_matching_source_bytes():
    scenario = {
        "id": "typed-claims-authoritative",
        "assertions": {
            "preview": {"required": True, "must_contain": ["Trusted text"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    preview = {"health": {"status": 200}, "content": "<!-- Trusted text --><div id='root'></div>"}
    results = _run(
        scenario=scenario,
        preview=preview,
        verified_claim_results=[
            {"kind": "visible_text", "expected": "Trusted text", "status": "fail"}
        ],
    )

    assert results[0].code == "PREVIEW_TRUTH_MISMATCH"
    assert results[0].facts["evidence_basis"] == "current_governed_visible_text_claims"


def test_non_text_and_model_authored_claim_shapes_do_not_prove_visible_text():
    scenario = {
        "id": "claim-kind-boundary",
        "assertions": {
            "preview": {"required": True, "must_contain": ["I verified it"]},
            "terminal_status_in": ["FINISHED"],
        },
    }
    results = _run(
        scenario=scenario,
        preview={"health": {"status": 200}, "content": ""},
        verified_claim_results=[
            {"kind": "rendered_content", "expected": "I verified it", "status": "pass"},
            {"kind": "assistant_prose", "expected": "I verified it", "status": "pass"},
        ],
    )

    assert results[0].code == "PREVIEW_TRUTH_MISMATCH"


def test_not_finished_with_required_output_fails_closed():
    # migration 2026_06_24_paused_incomplete_not_pass: a run that REQUIRES output but
    # never reached a finished terminal is NOT a "skip into PASS" — it is
    # BUILD_DID_NOT_FINISH (the build did not complete). (Previously this SKIPPED, which
    # let a paused-incomplete run score PASS — surfaced-bugs Bug 8.)
    unfinished = clean_smoke_log()[:-1]  # drop the FINISHED status
    results = _run(workspace={"other.html": "x"}, events=unfinished)
    assert results[0].code == "BUILD_DID_NOT_FINISH"
    assert results[0].facts["terminal_status"] is None


def test_not_finished_without_output_assertion_still_skips():
    # The gate only fires where a finish was actually REQUIRED. A scenario asserting no
    # output truth still SKIPs on a non-finished run (unchanged).
    unfinished = clean_smoke_log()[:-1]
    results = _run(scenario={"id": "x", "assertions": {}}, events=unfinished)
    assert results[0].skipped


# ---- CXT-5: destructive-elision scan over final deliverables ------------------

_SCN_ELISION = {
    "id": "elision",
    "assertions": {
        "workspace": {"files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}]},
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}

_SCN_ELISION_WAIVED = {
    "id": "elision_waived",
    "assertions": {
        "workspace": {
            "allow_elision": True,
            "files": [{"path": "index.html", "must_contain": ["Build Smoke OK"]}],
        },
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}


def test_destructive_elision_in_deliverable_fails():
    results = _run(
        scenario=_SCN_ELISION,
        workspace={"index.html": "<h1>Build Smoke OK</h1>\n<!-- ...(elided)... -->"},
    )
    assert results[0].code == "DESTRUCTIVE_ELISION", results[0].to_dict()
    assert results[0].facts["path"] == "index.html"


def test_recoverable_marker_in_deliverable_passes():
    # a destructive phrase paired with a recover cue on the same line is fine
    results = _run(
        scenario=_SCN_ELISION,
        workspace={
            "index.html": "<h1>Build Smoke OK</h1>\n<!-- content omitted — file_read app.js -->"
        },
    )
    assert results[0].passed, results[0].to_dict()


def test_elision_waiver_allows_marker():
    results = _run(
        scenario=_SCN_ELISION_WAIVED,
        workspace={"index.html": "<h1>Build Smoke OK</h1>\n<!-- truncated for brevity -->"},
    )
    assert results[0].passed, results[0].to_dict()


def test_must_contain_is_case_insensitive():
    """2026-07-09 overnight soak: the model wrote 'Hearth & Crumb Bakery' and the
    case-sensitive 'bakery' check FAILed a correct page. must_contain asserts
    natural-language CONTENT; capitalization is content-neutral."""
    results = _run(
        scenario={
            "id": "bakery",
            "assertions": {
                "workspace": {"files": [{"path": "index.html", "must_contain": ["bakery"]}]},
                "terminal_status_in": ["FINISHED"],
            },
        },
        workspace={"index.html": "<title>Hearth &amp; Crumb Bakery</title>"},
    )
    assert results[0].passed, (
        f"capitalized 'Bakery' must satisfy must_contain ['bakery']: {results[0].to_dict()}"
    )


# ---- governed artifact lineage resolution (counted seed 440026) -------------


def test_project_root_resolves_from_built_artifact_lineage() -> None:
    """Counted seed 440026: the verified entry names the build OUTPUT
    (react-continue/dist/index.html) while the open-set contract describes the
    PROJECT that produced it. Every enclosing directory of the verified entry is
    governed lineage; the contract binds to the deepest viable root."""
    from harness.build_soak.oracles.workspace_contract import resolve_open_assertion_root

    root, error = resolve_open_assertion_root(
        present_paths=[
            "react-continue/package.json",
            "react-continue/index.html",
            "react-continue/src/App.jsx",
            "react-continue/dist/index.html",
            "react-continue/dist/assets/index-abc.js",
        ],
        declared_paths=["package.json", "index.html"],
        verified_artifact_paths=["react-continue/dist/index.html"],
    )

    assert error is None
    assert root == "react-continue"


def test_nested_viable_roots_bind_to_the_deepest() -> None:
    """When both the artifact directory and its ancestor satisfy the contract,
    the binding stays closest to the verified bytes — never a looser ancestor."""
    from harness.build_soak.oracles.workspace_contract import resolve_open_assertion_root

    root, error = resolve_open_assertion_root(
        present_paths=[
            "app/index.html",
            "app/dist/index.html",
        ],
        declared_paths=["index.html"],
        verified_artifact_paths=["app/dist/index.html"],
    )

    assert error is None
    assert root == "app/dist"


def test_sibling_lineages_still_refuse_to_guess() -> None:
    """Control: two distinct sibling lineages both satisfying the contract stay
    an explicit refusal — the lineage widening never guesses across roots."""
    from harness.build_soak.oracles.workspace_contract import resolve_open_assertion_root

    root, error = resolve_open_assertion_root(
        present_paths=["one/index.html", "two/index.html"],
        declared_paths=["index.html"],
        verified_artifact_paths=["one/index.html", "two/index.html"],
    )

    assert root is None
    assert error is not None
    assert error["reason"] == (
        "multiple governed artifact roots satisfy the declared output contract"
    )


def test_scaffold_outside_verified_lineage_never_satisfies() -> None:
    """Control: a stale scaffold that is not on the verified artifact's lineage
    cannot satisfy the contract even when its files exist."""
    from harness.build_soak.oracles.workspace_contract import resolve_open_assertion_root

    root, error = resolve_open_assertion_root(
        present_paths=[
            "old-scaffold/package.json",
            "old-scaffold/index.html",
            "app/dist/index.html",
        ],
        declared_paths=["package.json", "index.html"],
        verified_artifact_paths=["app/dist/index.html"],
    )

    assert root is None


def test_visible_text_match_is_whitespace_normalized_but_not_content_blind():
    """Rendered text comes from the accessibility tree, which emits a newline at
    element boundaries.

    `<h1>Node Seed <span>440028</span></h1>` visibly reads exactly "Node Seed
    440028" but extracts as "Node Seed\\n440028", so whether a `must_contain`
    passed depended on whether the model wrapped the seed in a block-level
    element — a styling choice. Content-neutral whitespace must not decide the
    verdict; intervening CONTENT still must.
    """
    from harness.build_soak.oracles.output_truth import _content_normalized

    rendered = "Node Seed\n440028\nA warm-mesh welcome from your minimal Node service"

    # the exact failure from counted seed 440028
    assert _content_normalized("Node Seed 440028") in _content_normalized(rendered)
    # capitalization stays content-neutral too (the pre-existing relaxation)
    assert _content_normalized("node seed 440028") in _content_normalized(rendered)

    # ...but the check is NOT weakened: separated by other content, absent, or wrong
    assert _content_normalized("Node Seed 999999") not in _content_normalized(rendered)
    assert _content_normalized("Node 440028") not in _content_normalized(rendered)
    assert _content_normalized("Seed welcome") not in _content_normalized(rendered)


_NODE_RESTART_SCN = {
    "id": "node_restart_shape",
    "assertions": {
        "workspace": {
            "files": [
                {"path": "server.js", "must_contain": ["Node Restart 450001"]},
                {"path": "package.json"},
            ]
        },
        "terminal_status_in": ["FINISHED", "VERIFIED"],
    },
}


def test_dependency_free_node_service_passes_without_package_json():
    """POSITIVE CONTROL: counted restart seed 450001, with the contract corrected.

    A dependency-free service is `server.js` and nothing else. With package.json no
    longer asserted (it was never requested by that prompt), the same workspace
    that FAILED must pass.
    """
    scenario = {
        "id": "node_restart_shape",
        "assertions": {
            "workspace": {
                "files": [{"path": "server.js", "must_contain": ["Node Restart 450001"]}]
            },
            "terminal_status_in": ["FINISHED", "VERIFIED"],
        },
    }
    results = _run(
        scenario=scenario,
        workspace={"server.js": "// Node Restart 450001\nprocess.env.PORT"},
        verified_artifact_paths=["server.js"],
    )

    assert results[0].passed, results[0].to_dict()


def test_unresolvable_root_never_blames_a_present_verified_file():
    """The defect from counted restart seed 450001.

    `server.js` was present, served, HTTP 200 and governed-verified; `package.json`
    was genuinely absent. Root resolution failed, so every declared path resolved to
    None and the per-spec loop blamed spec[0] — `required_path: server.js`. The
    report must name the file that is actually missing, and must never name a file
    the run demonstrably produced.
    """
    results = _run(
        scenario=_NODE_RESTART_SCN,
        workspace={"server.js": "// Node Restart 450001"},
        verified_artifact_paths=["server.js"],
    )

    facts = results[0].facts
    assert results[0].code == "FALSE_FINISH_NO_OUTPUT", facts
    assert facts.get("required_path") == "package.json", facts
    assert facts.get("required_path") != "server.js"
    assert "package.json" in facts.get("missing_declared_paths", []), facts
    assert "server.js" in facts.get("present_declared_paths", []), facts


def test_a_genuinely_missing_requested_file_still_fails():
    """NEGATIVE CONTROL: honest attribution must not become leniency.

    When the asserted file really is absent, the run must still FAIL — narrowing
    who gets blamed must not narrow what gets caught.
    """
    results = _run(
        scenario=_NODE_RESTART_SCN,
        workspace={"package.json": "{}"},
        verified_artifact_paths=["package.json"],
    )

    assert results[0].code == "FALSE_FINISH_NO_OUTPUT", results[0].facts
    assert results[0].facts.get("required_path") == "server.js", results[0].facts
