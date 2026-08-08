"""Mutation coverage for the exact static and runtime test inventory."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import assert_problem_contains, null_advance_row, write

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from architecture.test_inventory_parts import _transitions  # noqa: E402

from architecture import inventory_static, test_inventory  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

_MAPPING_FIELDS = (
    "python_test_file_count",
    "python_static_test_id_count",
    "python_test_files",
    "python_static_test_ids",
    "typescript_test_file_count",
    "typescript_static_test_id_count",
    "typescript_test_files",
    "typescript_static_test_ids",
    "fixtures",
    "markers",
)


def _canonical_row(row: Any) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _transition_for(authority: dict[str, Any], package: str) -> dict[str, Any]:
    matches = [row for row in authority["additive_transitions"] if row.get("package") == package]
    assert len(matches) == 1
    return matches[0]


def _without_later_transition_additions(authority: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(authority)
    for transition in authority["additive_transitions"]:
        if transition["package"] == "PKG-02-GATE":
            continue
        for root, row in transition["collected_roots"].items():
            additions = set(row["added_ids"])
            result["collected"]["roots"][root] = [
                node_id
                for node_id in result["collected"]["roots"][root]
                if node_id not in additions
            ]
        for key, additions in transition["mapping_static_additions"].items():
            if key == "fixtures":
                claimed = {_canonical_row(row) for row in additions}
                result["mapping_static"][key] = [
                    row
                    for row in result["mapping_static"][key]
                    if _canonical_row(row) not in claimed
                ]
            else:
                claimed = set(additions)
                result["mapping_static"][key] = [
                    item for item in result["mapping_static"][key] if item not in claimed
                ]
    collected = result["collected"]
    collected["counts"] = {root: len(node_ids) for root, node_ids in collected["roots"].items()}
    collected["total"] = sum(collected["counts"].values())
    mapping = result["mapping_static"]
    for prefix in ("python", "typescript"):
        mapping[f"{prefix}_test_file_count"] = len(mapping[f"{prefix}_test_files"])
        mapping[f"{prefix}_static_test_id_count"] = len(mapping[f"{prefix}_static_test_ids"])
    return result


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        input=input_text,
        text=True,
    ).strip()


def _checkpoint(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _tree_commit(root: Path, tree: str, parent: str, message: str) -> str:
    return _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit-tree",
        tree,
        "-p",
        parent,
        input_text=f"{message}\n",
    )


def _mapping_actual(baseline: dict[str, Any]) -> dict[str, Any]:
    mapping = baseline["mapping_static"]
    return {
        **{key: copy.deepcopy(mapping[key]) for key in _MAPPING_FIELDS},
        "parse_errors": [],
    }


def _check_static(
    monkeypatch: pytest.MonkeyPatch,
    baseline: dict[str, Any],
    actual: dict[str, Any] | None = None,
) -> list[str]:
    scanned = _mapping_actual(baseline) if actual is None else actual
    monkeypatch.setattr(
        test_inventory,
        "scan_mapping_static",
        lambda _root: copy.deepcopy(scanned),
    )
    problems: list[str] = []
    test_inventory._check_mapping_static(baseline, REPO_ROOT, problems)
    return problems


def _frontend_actual(
    baseline: dict[str, Any],
) -> tuple[list[str], list[str], dict[str, dict[str, Any]], str]:
    frontend = baseline["frontend_real_collection"]
    return (
        copy.deepcopy(frontend["vitest_ids_list"]),
        copy.deepcopy(frontend["vitest_files_list"]),
        copy.deepcopy(frontend["playwright_configs"]),
        "",
    )


def _check_frontend(
    monkeypatch: pytest.MonkeyPatch,
    baseline: dict[str, Any],
    actual: tuple[list[str], list[str], dict[str, dict[str, Any]], str] | None = None,
    identities: dict[str, str] | None = None,
) -> list[str]:
    collected = _frontend_actual(baseline) if actual is None else actual
    actual_identities = (
        copy.deepcopy(baseline["frontend_real_collection"]["config_identities"])
        if identities is None
        else copy.deepcopy(identities)
    )
    monkeypatch.setattr(
        test_inventory,
        "_collect_frontend",
        lambda _root: copy.deepcopy(collected),
    )
    monkeypatch.setattr(
        test_inventory,
        "_config_identities",
        lambda _root: copy.deepcopy(actual_identities),
    )
    problems: list[str] = []
    test_inventory._check_frontend_collection(baseline, REPO_ROOT, problems)
    return problems


def _check_exact_live(
    monkeypatch: pytest.MonkeyPatch,
    stored: dict[str, Any],
    live: dict[str, Any],
) -> dict[str, Any]:
    mapping = _mapping_actual(live)
    frontend = _frontend_actual(live)
    collected = live["collected"]["roots"]
    sandbox = sorted(test_inventory.SANDBOX_INTEGRATION_DESELECTED_IDS)
    monkeypatch.setattr(
        test_inventory,
        "load_test_inventory",
        lambda _root: copy.deepcopy(stored),
    )
    monkeypatch.setattr(
        inventory_static,
        "candidate_problems",
        lambda _root, _identity: [],
    )
    monkeypatch.setattr(
        test_inventory,
        "scan_mapping_static",
        lambda _root: copy.deepcopy(mapping),
    )
    monkeypatch.setattr(
        test_inventory,
        "_collect_frontend",
        lambda _root: copy.deepcopy(frontend),
    )
    monkeypatch.setattr(
        test_inventory,
        "_config_identities",
        lambda _root: copy.deepcopy(live["frontend_real_collection"]["config_identities"]),
    )

    def collect(_root: Path, root_dir: str, marker: str = ""):
        if (root_dir, marker) == ("packages", "sandbox_integration"):
            return copy.deepcopy(sandbox), ""
        return copy.deepcopy(collected[root_dir]), ""

    monkeypatch.setattr(test_inventory, "_collect_pytest_ids", collect)
    return test_inventory.check_test_inventory(REPO_ROOT)


class TestMappingStatic:
    def test_mapping_static_counts(self):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        mapping = baseline["mapping_static"]
        expected_counts = {
            # 787 from Epic 11-C: packages/tools/tests/_appkit_verify_doubles.py,
            # the doubles extracted out of test_verify_appkit_app.py to clear
            # PY-0889. It defines no test — the collected node-id set is
            # unchanged at 9,278 — but it is a file under a tests/ root.
            # 788 from the A6 boundary: harness/build_soak/tests/
            # test_browser_capability.py, the A6.1 scope-gauge tests. Its 16
            # collected node ids are 13 static test ids — one case is
            # parametrized four ways — which is why the two counts move by
            # different amounts.
            # 789 from the HARN-1b boundary: packages/tools/tests/
            # test_shipped_standalone_artifacts.py, the register #7
            # standalone-execution guard. Its 6 collected node ids are 4 static
            # test ids — one case is parametrized over the three discovered
            # trusted-component probes — so the same two-count split applies
            # again, in different proportions.
            # 791 from the Epic 12-A boundary: tests/architecture/
            # test_frontend_declarations.py, the mutation battery for the fourth
            # public-API authority. Its 27 static test ids are 37 collected node
            # ids — one case is parametrized over eleven invalid-row mutations —
            # so the split runs the other way here than it did for A6.
            # 792 from the Epic 13-A boundary: packages/core/tests/
            # test_loop_ports.py, the guard holding the eight loop ports derived
            # from the coupling inventory. Its 4 static test ids are 4 collected
            # node ids — nothing is parametrized — so for once the two counts
            # move by the same amount.
            # 801 from Epic 15: the eight Next.js target/connector test files
            # added by slices 15-T2..15-C3, 15-P1 corrective, and 15-L1.
            # 802 from Epic 16-S1: the Reference Pack registry focused tests.
            # 803 from Epic 16-S2 corrective: the Starter Recipe registry
            # focused tests, which bind the frozen R-LATENT/v1 Starter corpus to
            # resolved production behavior. Its 38 static test ids are 38
            # collected node ids — nothing is parametrized — so both counts move
            # by the same amount.
            # 808 from PKG-16 steps 3–7: the shared R-LATENT/v1 classifier, the
            # Library Recipe seam, the Reference Pack value surface, and the
            # library composition/catalog suites — five files binding the whole
            # 24-case frozen corpus (all three kinds) plus trust evaluation,
            # capability intersection, precedence, provenance/BOM, ejection,
            # progressive disclosure and guided authoring. Here the two counts
            # move by DIFFERENT amounts: +118 static ids against +125 collected
            # node ids, because the Library Recipe mount and namespace tests are
            # parametrized (4 + 5 cases from 2 static ids).
            # 811 from PKG-17-HOST: the host capability probe/evidence vocabulary,
            # the versioned host profile registry with its persisted-reference
            # compatibility descriptors, and the host-side probe adapter — three
            # files. The two counts again move by different amounts: +93 static
            # ids against +94 collected node ids, because the advertising-grade
            # test is parametrized (2 cases from 1 static id).
            # 815 from PKG-18-REGISTRATION: the mobile- and desktop-shaped
            # registration proof — two shape fixtures, their cross-cutting
            # change-locality/command-inventory/rollback proofs, and the shared
            # test-owned fixture module. FOUR files but only THREE contribute
            # ids: `build_platform_registration_shapes.py` is a helper module
            # with no `test_` function, and the inventory counts any `.py` under
            # a `/tests/` directory as a test file. Here the two counts move by
            # the SAME amount — +31 static ids against +31 collected node ids —
            # because nothing in this package is parametrized.
            # 816 from PKG-19-CERT-STRUCTURAL (continuation): the adversarial
            # suite for the new inventory execution-coverage gate
            # (tests/architecture/test_inventory_execution.py). ONE file, +10
            # static ids and +10 collected node ids — nothing parametrized. The
            # gate it guards is the comparator that closes findings F1/F2/F6:
            # it fails when the inventory certifies an id no sanctioned command
            # can collect, which was true of 1213 ids at the parent identity.
            # 818 from PKG-03-HARNESS-ORACLES (A11): two files —
            # `harness/build_soak/tests/test_thrash_shapes.py` (the shape
            # classifier's regression fixtures, built from register #5's three
            # real firings) and `packages/core/tests/test_freshness_memo.py`
            # (the memo's deterministic acceptance). +20 static ids against +24
            # collected node ids: the gap is the six PARAMETRIZED historical
            # cases (two tests x three fixtures) counted once statically and
            # three times each when collected, less the one id the retargeted
            # `test_assist_off_never_reminds` keeps. That id was deliberately
            # NOT renamed: the inventory authorises deletions only through
            # `module_split_transitions`, whose contract states "a rename is not
            # a relocation", so renaming would have meant inventing governance
            # authority or weakening the ratchet.
            # 819 from PKG-03-EDIT-EVIDENCE: ONE file,
            # `harness/build_soak/tests/test_edit_evidence_producer.py` — the
            # acceptance of the five P8D edit-evidence producers, the producer
            # the oracles' own docstrings name and that had never been built.
            # +24 static ids against +24 collected node ids: nothing is
            # parametrized, so the two counts move together for once. Five of
            # the 24 are the NEGATIVE CONTROLS — one per producer — which are
            # what make the other adjudications mean anything: each nullifies a
            # single observed behaviour and asserts the oracle's verdict flips.
            #
            # 9943 from PKG-19-GROUNDED-FEEDBACK: +12 static ids under
            # `packages`. `test_receipt_currency_shared.py` adds 9 — the shared
            # currency predicate's enforcement (object identity of the harness
            # re-export, the vocabulary's reachability, and the owner's two
            # clauses as behaviour). `test_serve_duplicate_escalation.py` moves
            # +6/-3: three assertions that pinned the two-branch finish message
            # are REPLACED by six that pin the projection rule, because the owner
            # record names that message as the canonical counter-example.
            # `python_test_file_count` advances 822 -> 823 for the one new file.
            #
            # 9931 from PKG-19-PILOT-REPAIR: +28 static ids against +28 collected
            # node ids — again nothing parametrized, so the two move together. The
            # two new files are the F47 and F49 regression evidence; the other four
            # ids extend `test_thrash_shapes.py`, which is where F47's general
            # invariant is ENFORCED (a repetition shape with no declared
            # `disco.core` identity owner fails there). `python_test_file_count`
            # advances 820 → 822 for the two new files.
            #
            # 9965 from PKG-19-CONSTRAINT4-DYNAMIC-REPAIR: +22 static ids against
            # +24 collected node ids. The gap is the ONE parametrized case
            # (`test_repeat_preamble_names_the_count_and_the_cost`, three repeat
            # counts) counted once statically and three times when collected.
            # ONE new file, `packages/core/tests/test_f51_constraint4_multifire.py`
            # (15 ids) — the twice-rule regression evidence for the five F51
            # surfaces that emitted byte-identical bodies across 24 live cells at
            # 2026-08-07d, one of them fifteen times in a single run. The other
            # nine extend two existing files: +6 in
            # `test_f47_answered_question_notice.py` for the plan-tracking notice
            # class, and +3 in `test_thrash_shapes.py` for the notice-REACH duty —
            # the file whose own green hid F47's asymmetry, because it proved the
            # two sides share an identity OWNER while the notice remained
            # unreachable for every non-shell tool. `python_test_file_count`
            # advances 823 -> 824 for the one new file.
            #
            # 9983 from PKG-19-CONSTRAINT4-CONFIRMED-REPAIR: +18 static ids against
            # +18 collected node ids — nothing parametrized this time, so the two
            # move together and there is no gap to explain. ONE new file,
            # `packages/core/tests/test_f53_constraint4_confirmed_repeats.py`, the
            # regression evidence for the four repeats CONFIRMED on live cells at
            # 2026-08-07h: `emit_plan_verifier_failure_notice` (twice, in two
            # different cells), `_plan_nudge` (whose count reset on every re-entry
            # into planning), `_blocked_prompt` (a pure function of its arguments),
            # and F53's cross-seam leak, where a REPAIRED surface's authored text
            # repeated byte-identically by riding an unrepaired rendering seam.
            # Twelve of the eighteen are red on `2aa67140`; the other six are
            # invariance guards that must be green on BOTH trees, because the
            # first firing of every repaired surface is byte-identical to the
            # pre-repair text and `_PLAN_NUDGE` must not move at all.
            # `python_test_file_count` advances 824 -> 825 for the one new file.
            #
            # 9996 from PKG-19-CONSTRAINT4-REACH: +13 static ids against +13
            # collected node ids — nothing parametrized, so the two move together
            # and there is no gap to explain. ONE new file,
            # `packages/core/tests/test_f55_f56_weakening_repeats_and_ordinals.py`,
            # the regression evidence for the FIFTH surface confirmed multi-fire on
            # live cells (F55: `reject_plan_weakening` rendering
            # `weakening_guidance` verbatim, caught at 3x and 2x in 07j's own fresh
            # corpus) and for the ungrammatical ordinal that repair would otherwise
            # have shipped again (F56: "the 2th time", read out of the raw bodies of
            # the one target 07j confirmed).
            # Seven of the thirteen are red on `b8aa2554`; the other six are
            # invariance guards that must be green on BOTH trees, because the first
            # firing of the weakening refusal is byte-identical to the pre-repair
            # text and the `repeats <= 1` branch of each ordinal seam must not move
            # at all.
            # `python_test_file_count` advances 825 -> 826 for the one new file.
            #
            # 10000 from PKG-19-SEAL-AND-DRAW: +4 static ids against +4 collected
            # node ids — nothing parametrized, so the two move together and there
            # is no gap to explain. ONE new file,
            # `packages/core/tests/test_w39_shell_escalation.py`, the guard for the
            # 2026-08-07n repair this seal carries: the answered-question notice
            # and the repeat cap are ONE STEP OUT OF PHASE, so occurrence 2 is
            # never warned and the notice's single shot lands immediately before
            # the occurrence `allowed=2` already fails the run on. That shot cannot
            # be moved earlier, so it was made to CARRY — the shell/script notice
            # is now repetition-aware and names the consequence, as the plan class
            # has since F47.
            # Three of the four are red on `f0a0fb29` (the old text names no
            # consequence, renders no count, and is byte-static across firings once
            # the moving `(step N)` back-pointer is normalized); the fourth is an
            # invariance guard that must be green on BOTH trees, because the repair
            # is additive rendering and the notice must stay advisory rather than
            # become a suppressor.
            # `python_test_file_count` advances 826 -> 827 for the one new file.
            #
            # 10012 from PKG-19-ROUTE-B-AND-F58: +12 static ids against +12
            # collected node ids — nothing parametrized, so the two move together
            # and there is no gap to explain. TWO new files, which is why
            # `python_test_file_count` advances 827 -> 829 rather than by one:
            #   * `packages/core/tests/test_route_b_driven_composition.py` (+7,
            #     under `packages`) — constraint 4's route (B), driven
            #     composition. The real caller reaches the real emitter twice in
            #     ONE run for both remaining targets, with each fire reading the
            #     previous fire's own emitted events rather than a hand-built
            #     stand-in, and each target carries a MUTATION CONTROL that
            #     removes the repair and shows the same drive going byte-identical
            #     again. A renderer exercised in isolation would not have counted:
            #     that is `_plan_nudge`'s own 07h defect.
            #   * `tests/architecture/test_attestation_binding.py` (+5, under
            #     `tests`) — the Attestation-Binding Invariant (F58), enforced. A
            #     test may not carry a COPY of a named production sentence; it
            #     must derive it from the symbol that owns it, because a copy
            #     attests itself and keeps passing after the product is reworded.
            #     Keyed on DERIVABILITY, not on length: the census measured the
            #     longest legitimate fragment probe (44 chars) as LONGER than the
            #     shortest in-scope restatement (36), so no length cut separates
            #     the classes without gerrymandering.
            #
            # 10015 from PKG-19-F62-POPULATION: +3 static ids against +3 collected
            # node ids, nothing parametrized, so the two move together and there is
            # no gap to explain. NO new file — `python_test_file_count` stays 829,
            # because all three additions land in the EXISTING
            # `tests/architecture/test_attestation_binding.py`:
            #   * `test_derived_population_covers_the_historical_family`
            #   * `test_test_files_glob_is_complete`
            #   * `test_allowlist_entries_are_dated_and_explained`
            # They exist because that gate's POPULATION was a hand-maintained tuple
            # of five files out of 793 (F62). The gate was real — 07s proved it
            # refusing three of F58's four in-scope restatements against the tree's
            # actual pre-repair bytes — but the same routine over the other 788
            # found TEN live instances of exactly the defect F58 named. The ten are
            # bound to their production owners, F61's two as well, and the
            # population is now DERIVED by glob rather than enumerated by hand, per
            # the 2026-08-07s DERIVED-POPULATION RULE. The three new tests guard the
            # derivation itself: that it still covers the original five, that the
            # `test_*.py` glob is complete, and that any future allowlist entry is
            # dated and explained.
            #
            # 10025 from PKG-19-CLEAN-SLATE: +10 static ids against +10 collected
            # node ids, nothing parametrized, so the two move together. ONE new file,
            # `packages/core/tests/test_f59_generic_notice.py` (+7) and the two
            # `test_classifier.py` extensions (+2) plus the F47 exclusion predicate
            # (+1). `python_test_file_count` advances 829 -> 830 for the one new file.
            # 10029 from PKG-19-CLEAN-SLATE (amended): +4 more static ids (F59
            # grading-path 2, F63 drift 1, F63 import-boundary 1) against +4
            # collected, so the two move together again. No new file; the four
            # land in the existing `test_f59_generic_notice.py` (+2) and
            # `test_classifier.py` (+2).
            # CLEAN-SLATE residue discharge: one new real planning-gate test file
            # contributes two ids; the existing F51 file adds the host-HALT reach id.
            # F59 seam: one new production-seam regression test in existing F59 file
            # (`test_verify_web_app_notice_via_real_observation_execution_integration_seam`)
            # adds one static id against one collected id.
            # PKG-19-CERT-REQUAL5-DEEPSEEK adds one exact provider-binding harness
            # contract plus two product-side DeepSeek request-policy contracts, so
            # collected and mapping-static each advance by three.
            # PKG-19-SEED-COMMISSION adds the four focused seed-gate tests and one
            # matrix contract; one new file makes the file count advance once.
            # The shared-owner correction adds four more focused contracts: three
            # direct-run CLI requirements and one disjoint multi-suite allocation
            # proof. Its one new file advances the file count once.
            "python_test_file_count": 833,
            "python_static_test_id_count": 10046,
            "typescript_test_file_count": 239,
            "typescript_static_test_id_count": 1203,
        }
        assert mapping["identity"] == baseline["source_identity"]
        assert {key: mapping[key] for key in expected_counts} == expected_counts
        for prefix in ("python", "typescript"):
            files = mapping[f"{prefix}_test_files"]
            ids = mapping[f"{prefix}_static_test_ids"]
            assert len(files) == mapping[f"{prefix}_test_file_count"]
            assert len(ids) == mapping[f"{prefix}_static_test_id_count"]
            assert files == sorted(files)
            assert ids == sorted(ids)
            assert len(files) == len(set(files))
            assert all(isinstance(item, str) and item for item in files + ids)

    def test_mapping_static_count_drift_fails(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        drifted = copy.deepcopy(baseline)
        mapping = drifted["mapping_static"]
        mapping["python_static_test_id_count"] -= 1
        mapping["python_test_files"] = list(reversed(mapping["python_test_files"]))
        mapping["typescript_test_files"].append(mapping["typescript_test_files"][0])

        problems = _check_static(monkeypatch, drifted, _mapping_actual(baseline))

        assert_problem_contains(problems, "python_static_test_id_count", "count drift")
        assert_problem_contains(problems, "python_test_files", "sorted")
        assert_problem_contains(problems, "typescript_test_files", "unique")
        assert_problem_contains(problems, "typescript_test_files", "drift")

    def test_mapping_static_fixture_count_drift_fails(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        fixtures = baseline["mapping_static"]["fixtures"]
        assert len(fixtures) == 177
        assert fixtures == sorted(fixtures, key=_canonical_row)
        assert len({_canonical_row(row) for row in fixtures}) == len(fixtures)
        assert all(
            set(row) == {"path", "line", "fixture", "scope"} and isinstance(row["line"], int)
            for row in fixtures
        )
        drifted = copy.deepcopy(baseline)
        drifted["mapping_static"]["fixtures"] = fixtures[:-1]

        problems = _check_static(monkeypatch, drifted, _mapping_actual(baseline))

        assert_problem_contains(problems, "mapping_static.fixtures", "drift", "added")

    def test_mapping_static_skip_marker_count_drift_fails(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        markers = baseline["mapping_static"]["markers"]
        assert len(markers) == 86
        assert markers == sorted(markers, key=_canonical_row)
        assert len({_canonical_row(row) for row in markers}) == len(markers)
        assert all(set(row) == {"framework", "path", "line", "marker", "source"} for row in markers)
        drifted = copy.deepcopy(baseline)
        drifted["mapping_static"]["markers"] = markers[1:]

        problems = _check_static(monkeypatch, drifted, _mapping_actual(baseline))

        assert_problem_contains(problems, "mapping_static.markers", "drift", "added")

    def test_mapping_static_forbidden_xfail_marker_fails(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        actual = _mapping_actual(baseline)
        actual["markers"].append(
            {
                "framework": "pytest",
                "path": "tests/test_new.py",
                "line": 1,
                "marker": "pytest.mark.xfail",
                "source": "@pytest.mark.xfail",
            }
        )
        actual["markers"].sort(key=_canonical_row)

        problems = _check_static(monkeypatch, baseline, actual)

        assert_problem_contains(problems, "xfail", "forbidden")
        assert_problem_contains(problems, "mapping_static.markers", "drift")

    def test_mapping_static_forbidden_todo_marker_fails(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        actual = _mapping_actual(baseline)
        actual["markers"].append(
            {
                "framework": "vitest/playwright",
                "path": "frontend/src/new.test.ts",
                "line": 1,
                "marker": "test.todo(",
                "source": "test.todo('later')",
            }
        )
        actual["markers"].sort(key=_canonical_row)

        problems = _check_static(monkeypatch, baseline, actual)

        assert_problem_contains(problems, "todo", "forbidden")
        assert_problem_contains(problems, "mapping_static.markers", "drift")

    def test_mapping_static_forbidden_only_marker_fails(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        actual = _mapping_actual(baseline)
        actual["markers"].append(
            {
                "framework": "vitest/playwright",
                "path": "frontend/src/new.test.ts",
                "line": 1,
                "marker": "test.only(",
                "source": "test.only('focused', () => {})",
            }
        )
        actual["markers"].sort(key=_canonical_row)

        problems = _check_static(monkeypatch, baseline, actual)

        assert_problem_contains(problems, "only", "forbidden")
        assert_problem_contains(problems, "mapping_static.markers", "drift")


class TestFrontendCollection:
    def test_frontend_collection_counts(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        frontend = baseline["frontend_real_collection"]
        assert (
            (
                len(frontend["vitest_ids_list"]),
                len(frontend["vitest_files_list"]),
            )
            == (frontend["vitest_ids"], frontend["vitest_files"])
            == (1153, 176)
        )
        assert frontend["vitest_ids_list"] == sorted(frontend["vitest_ids_list"])
        assert frontend["vitest_files_list"] == sorted(frontend["vitest_files_list"])
        assert len(set(frontend["vitest_ids_list"])) == 1153
        assert len(set(frontend["vitest_files_list"])) == 176
        assert set(frontend["playwright_configs"]) == set(test_inventory.PLAYWRIGHT_CONFIGS)
        for config, authority in frontend["playwright_configs"].items():
            assert len(authority["ids"]) == authority["id_count"]
            assert len(authority["files"]) == authority["file_count"]
            assert authority["ids"] == sorted(set(authority["ids"]))
            assert authority["files"] == sorted(set(authority["files"]))
            assert all(item.startswith("frontend/") for item in authority["ids"])
            assert config in test_inventory.PLAYWRIGHT_TEST_DIRS

        calls: list[str] = []
        vitest_ids = ["frontend/src/unit.test.ts::unit"]
        vitest_files = ["frontend/src/unit.test.ts"]
        live_ids = ["frontend/e2e-live/live.spec.ts::live"]
        live_files = ["frontend/e2e-live/live.spec.ts"]

        def collect_vitest(root: Path):
            assert root == REPO_ROOT
            calls.append("vitest")
            return vitest_ids, vitest_files, ""

        def collect_playwright(root: Path, config: str):
            assert root == REPO_ROOT
            calls.append(config)
            if config in test_inventory.LIVE_CONFIGS:
                return live_ids, live_files, ""
            source = (
                f"frontend/{test_inventory.PLAYWRIGHT_TEST_DIRS[config]}/"
                f"{config.replace('/', '-')}.spec.ts"
            )
            return [f"{source}::{config}"], [source], ""

        monkeypatch.setattr(test_inventory, "_collect_vitest", collect_vitest)
        monkeypatch.setattr(test_inventory, "_collect_playwright", collect_playwright)
        actual_ids, actual_files, playwright, error = test_inventory._collect_frontend(REPO_ROOT)
        assert error == ""
        assert (actual_ids, actual_files) == (vitest_ids, vitest_files)
        assert calls == ["vitest", *test_inventory.PLAYWRIGHT_CONFIGS]
        assert len(calls) == len(set(calls)) == 7
        assert tuple(playwright) == test_inventory.PLAYWRIGHT_CONFIGS
        first, second = test_inventory.LIVE_CONFIGS
        assert (
            playwright[first]
            == playwright[second]
            == {
                "id_count": 1,
                "file_count": 1,
                "ids": live_ids,
                "files": live_files,
            }
        )

    def test_frontend_collection_drift_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        ids, files, error = test_inventory._collect_vitest(tmp_path)
        assert (ids, files) == ([], [])
        assert "missing" in error
        frontend = tmp_path / "frontend"
        write(frontend / "node_modules/.bin/vitest", "")
        write(frontend / "vite.config.ts", "export default {};\n")
        payload = [
            {"name": "alpha", "file": "src/a.test.ts"},
            {
                "name": "beta",
                "file": str(frontend / "src/b.test.ts"),
            },
        ]
        monkeypatch.setattr(
            test_inventory.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr=""
            ),
        )
        ids, files, error = test_inventory._collect_vitest(tmp_path)
        assert error == ""
        assert ids == [
            "frontend/src/a.test.ts::alpha",
            "frontend/src/b.test.ts::beta",
        ]
        assert files == [
            "frontend/src/a.test.ts",
            "frontend/src/b.test.ts",
        ]
        monkeypatch.setattr(
            test_inventory.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout='{"not": "a list"}', stderr=""
            ),
        )
        ids, files, error = test_inventory._collect_vitest(tmp_path)
        assert (ids, files) == ([], [])
        assert "parse failed" in error

        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        drifted = copy.deepcopy(baseline)
        drifted["frontend_real_collection"]["vitest_ids_list"] = drifted[
            "frontend_real_collection"
        ]["vitest_ids_list"][:-1]
        problems = _check_frontend(monkeypatch, drifted, _frontend_actual(baseline))
        assert_problem_contains(problems, "frontend.vitest_ids_list", "drift")

    def test_frontend_live_configs_must_select_same_ids(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        actual = _frontend_actual(baseline)
        playwright = actual[2]
        first, second = test_inventory.LIVE_CONFIGS
        assert playwright[first]["ids"] == playwright[second]["ids"]
        assert playwright[first]["files"] == playwright[second]["files"]
        assert (
            playwright[first]["id_count"],
            playwright[first]["file_count"],
        ) == (38, 36)
        playwright[second]["ids"][-1] = "frontend/e2e-live/replacement.spec.ts::replacement"
        playwright[second]["ids"].sort()

        problems = _check_frontend(monkeypatch, baseline, actual)

        assert_problem_contains(problems, "live configs", "identical", "38")
        assert_problem_contains(problems, f"frontend.{second}.ids", "drift")

    def test_frontend_config_identities_retained(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        frontend = baseline["frontend_real_collection"]
        assert set(frontend["config_identities"]) == set(test_inventory.PLAYWRIGHT_CONFIGS)
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in frontend["config_identities"].values()
        )
        assert frontend["collector_commands"] == (test_inventory._collector_commands())
        drifted = copy.deepcopy(baseline)
        drifted_frontend = drifted["frontend_real_collection"]
        drifted_frontend["config_identities"]["playwright.config.ts"] = "0" * 64
        drifted_frontend["collector_commands"]["vitest"].remove("--json")

        problems = _check_frontend(
            monkeypatch,
            drifted,
            _frontend_actual(baseline),
            frontend["config_identities"],
        )

        assert_problem_contains(problems, "collector command", "drift")
        assert_problem_contains(problems, "config identities", "drift")


class TestCollectedCounts:
    def test_collected_counts(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        collected = baseline["collected"]
        expected = {
            # 10340 from PKG-19-THRASH-REPAIR: +7 under `packages`, the regression
            # evidence for the two counted wave-1 FAILs. These land under `packages`
            # and not `harness` because both defects are product bytes: the serve
            # shape refusal (`packages/core/tests/test_serve_tool.py`, +1) and the
            # dictated-literal extractor (`test_dictated_content_literals.py`, +1
            # plus a 5-case parametrization). Both fail on the pre-repair bytes.
            #
            # 10376 from PKG-19-GROUNDED-FEEDBACK: +12 under `packages` — the 9
            # currency-predicate enforcement ids plus the serve-escalation file's
            # net +3 (six projection-rule assertions replacing three that pinned
            # the delegated fork). All product bytes, so all land here; this
            # boundary added no harness ids.
            #
            # 10364 from PKG-19-PILOT-REPAIR: +24 under `packages` — 10 for F47's
            # answered-question notice evidence and 14 for F49's observation-scoped
            # receipt evidence. Both are product bytes, so they land here; the
            # remaining 4 ids of that seal's 28 land under `harness` below, because
            # F47's invariant is enforced by the ORACLE, not by the product.
            # 10397 from PKG-19-CONSTRAINT4-DYNAMIC-REPAIR: +21 under `packages` —
            # 15 for the new `test_f51_constraint4_multifire.py` (three of which
            # are one parametrized case) and 6 extending
            # `test_f47_answered_question_notice.py` with the plan-tracking notice
            # class. The other 3 of this seal's 24 land under `harness` below.
            # 10415 from PKG-19-CONSTRAINT4-CONFIRMED-REPAIR: +18 under `packages`,
            # the whole of that seal's delta — one new file,
            # `test_f53_constraint4_confirmed_repeats.py`, and no harness id.
            # 10428 from PKG-19-CONSTRAINT4-REACH: +13 under `packages`, the whole
            # of that seal's delta — one new file,
            # `test_f55_f56_weakening_repeats_and_ordinals.py`, and no harness id.
            # 10432 from PKG-19-SEAL-AND-DRAW: +4 under `packages`, the whole of
            # this seal's delta — one new file,
            # `test_w39_shell_escalation.py`, and no harness id.
            # 10439 from PKG-19-ROUTE-B-AND-F58: +7 under `packages` — one new
            # file, `test_route_b_driven_composition.py`, and no harness id. This
            # seal's delta is NOT wholly here: its other 5 ids land under `tests`
            # below, because the F58 enforcement is an architecture fitness test
            # rather than product-adjacent unit coverage.
            # 10447 from PKG-19-CLEAN-SLATE: +8 under `packages` — seven F59 ids
            # (`test_f59_generic_notice.py`) plus the one F47 exclusion predicate.
            # 10449 from PKG-19-CLEAN-SLATE (amended): +2 more under `packages`
            # (F59 grading-path 2) — the F63 drift and import-boundary ids land
            # under `harness` below, but the packages count moves by +2 for the
            # two new F59 grading-path proofs.
            # CLEAN-SLATE residue discharge adds three product-path ids.
            # F59 seam: one new production-seam test in existing F59 file.
            # PKG-19-CERT-REQUAL5-DEEPSEEK adds two request-policy ids in the
            # existing thinking-budget test module.
            "packages": 10455,
            # 1288 from PKG-03-EDIT-EVIDENCE: +24 in the `harness` root, the
            # edit-evidence producer acceptance. The new ids land here and not
            # under `packages` because the producer is harness code; the
            # sanctioned lane that owns them is `make harness`
            # (`PYTHONPATH=. uv run pytest harness`), and
            # check_inventory_execution confirms all 12088 certified ids stay
            # reachable by a sanctioned command.
            #
            # 1312 from PKG-03-F20-CONTEXT-PRESSURE: +22 in the `harness` root,
            # the driver context-window pin. Same reasoning — the pin is harness
            # code, so `make harness` owns the new ids. Of the 22, the ones that
            # carry the weight are the fail-closed cases: an unhonoured pin and a
            # pin that changes mid-run must both RAISE, because the product
            # resolves an unknown model_override by silently falling back to the
            # default driver.
            # 1313 from PKG-03-F28-LEG-C-A4FLIP: +1 in the `harness` root,
            # `test_promotion.py::test_product_harness_is_required_by_default`. A4
            # step 4 flipped `require_product_harness` to True once the F28 set came
            # back 10/10 green at one sealed sha; every other test in that file passes
            # the flag EXPLICITLY, so without this one the default could silently
            # regress to False and the whole file would stay green.
            #
            # 1317 from PKG-19-PILOT-REPAIR: +4 in the `harness` root, all in
            # `test_thrash_shapes.py`. This is the first repair in the PKG-19 chain
            # to move `harness`, and deliberately: F47's general invariant —
            # answered-question repetition is culpable only after notice — is
            # enforced against the ORACLE. One of the four enumerates every
            # repetition shape and fails if any lacks a declared `disco.core`
            # identity owner, so a fifth shape cannot silently re-open the gap that
            # produced the 2026-08-06v pilot reds. The 06t seal's note that repairs
            # land under `packages` described that repair, not a rule.
            # 1320 from PKG-19-CONSTRAINT4-DYNAMIC-REPAIR: +3 in the `harness`
            # root, all in `test_thrash_shapes.py`, and for the same reason the
            # 06x seal moved it — F47's invariant is enforced against the ORACLE.
            # These three add the REACH duty the 06x enforcement could not carry:
            # that file's green was true and the guarantee was still false,
            # because `ThrashOracle` counts identical repeats over EVERY tool
            # while the notice was emitted only for `_W39_SHELL_TOOLS`. One pins
            # that premise, one ratchets the covered classes, one declares the
            # residue the oracle counts but no notice reaches.
            # 1322 from PKG-19-CLEAN-SLATE: see above — the two `test_classifier.py`
            # ids land here, because the classifier fixture is a harness oracle.
            # 1324 from PKG-19-CLEAN-SLATE (amended): +2 more in `harness` for
            # the F63 drift control and import-boundary tests.
            # 1325 from PKG-19-CERT-REQUAL5-DEEPSEEK: +1 exact provider-binding
            # contract in the existing capture-classifier test module.
            # 1330 from PKG-19-SEED-COMMISSION: +5 seed commissioning contracts.
            # 1335 after its shared-owner correction: +5 harness contracts,
            # including the F1 wrapper's explicit-seed requirement.
            "harness": 1335,
            # 1320 UNCHANGED at PKG-19-CONSTRAINT4-CONFIRMED-REPAIR: all 18 of that
            # seal's ids are product bytes under `packages`. The repair is at four
            # `disco.core.loop` emitting seams and the oracles were deliberately not
            # touched — `_thrash_recovery._validate_blocked_meta` reads named keys
            # rather than an exact key set, so the durable `blocked_guidance_fp`
            # added to the landing meta does not move its verdict and needs no
            # oracle change to stay true.
            # 1320 UNCHANGED at PKG-19-CONSTRAINT4-REACH for the same reason: all
            # 13 of that seal's ids are product bytes under `packages`. F55 is
            # repaired at one `disco.core.loop` emitting seam and F56 at two
            # rendering helpers; no oracle sees either, because both repairs change
            # only the TEXT a surface renders on its repeat path and every oracle
            # that could care keys on the durable `blocking` label, which did not
            # move.
            # 1320 UNCHANGED at PKG-19-SEAL-AND-DRAW for the same reason: all 4 of
            # this seal's ids are product bytes under `packages`. The repair is a
            # MESSAGE repair at two `disco.core.loop` rendering seams with the
            # shared vocabulary moved to one owner; no oracle sees it.
            # `max_identical_action_repeats` is still 2 and
            # `_BOOKKEEPING_STREAK_NUDGE_AT` is still 3 — the cap the notice now
            # names is the harness scenario's, and the notice deliberately quotes
            # no number so product prose cannot couple to a grading threshold.
            # 1320 UNCHANGED at PKG-19-ROUTE-B-AND-F58: that seal adds no harness
            # id either. Its product-side change is an EXTRACTION — two inline
            # agent-facing literals given names so tests can derive them — proven
            # byte-identical by control, so no oracle and no harness pin can see
            # it.
            # 1322 from PKG-19-CLEAN-SLATE: +2 in the `harness` root for the two
            # `test_classifier.py` extensions (`test_classifier_still_independent_of_producer`
            # and `test_planning_gate_refusal_fixture_is_derived_from_production`),
            # plus +8 in `packages` for the seven F59 ids and the F47 exclusion.
            # The mapping-static count moves by +10 for the same reasons; the
            # harness/packets split reflects the sanctioned lanes (`make harness`
            # vs `make unit`).
            "integrations": 9,
            # 391 from PKG-19-ROUTE-B-AND-F58: +5 under `tests` for
            # `tests/architecture/test_attestation_binding.py`, the F58
            # enforcement. It belongs under `tests` and not `packages` because it
            # is an architecture fitness gate over the repo's own test corpus,
            # which is what the `tests` root collects.
            # 394 from PKG-19-F62-POPULATION: +3 under `tests` for the three
            # derivation guards added to `test_attestation_binding.py` when that
            # gate's population stopped being a hand-maintained five-file tuple and
            # became a glob over the whole test tree (F62).
            # 394 UNCHANGED at PKG-19-CLEAN-SLATE: no `tests` ids added here; the
            # two `test_classifier.py` ids land under `harness` and the seven F59
            # ids plus the F47 extension land under `packages`.
            "tests": 394,
        }
        assert set(collected["roots"]) == set(test_inventory.PYTHON_ROOTS)
        assert collected["counts"] == expected
        assert collected["total"] == 12193 == sum(expected.values())
        for root in test_inventory.PYTHON_ROOTS:
            ids = collected["roots"][root]
            assert len(ids) == expected[root]
            assert ids == sorted(ids)
            assert len(ids) == len(set(ids))
            assert all(isinstance(node_id, str) and "::" in node_id for node_id in ids)

        def collect(_root: Path, root_dir: str, marker: str = ""):
            assert marker in ("", "not sandbox_integration")
            return copy.deepcopy(collected["roots"][root_dir]), ""

        monkeypatch.setattr(test_inventory, "_collect_pytest_ids", collect)
        problems: list[str] = []
        result = test_inventory._check_collected_ids(baseline, REPO_ROOT, problems)
        assert problems == []
        assert result == {"collected_total": 12193}

        drifted = copy.deepcopy(baseline)
        drifted["collected"]["roots"]["tests"] = list(
            reversed(drifted["collected"]["roots"]["tests"])
        )
        problems = []
        test_inventory._check_collected_ids(drifted, REPO_ROOT, problems)
        assert_problem_contains(problems, "collected.roots.tests", "sorted")
        assert_problem_contains(problems, "collected IDs in tests", "drift")


class TestBaselineValidation:
    def test_baseline_data_validates(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        result = _check_exact_live(monkeypatch, baseline, baseline)
        assert result == {
            "ok": True,
            "problems": [],
            "python_static_ids": 10046,
            "typescript_static_ids": 1203,
            "collected_total": 12193,
        }

        latest_identity = test_inventory.subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        if latest_identity == baseline["source_identity"]:
            # Baseline already at HEAD after a regeneration-before-commit
            # (dirty tree). Exercise the same advanced-transition invariant
            # using a resolvable alternate commit instead of skipping.
            latest_identity = test_inventory.subprocess.check_output(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD~1"],
                text=True,
            ).strip()
            assert latest_identity != baseline["source_identity"]
            assert test_inventory.commit_identity_resolves(REPO_ROOT, latest_identity)
        advanced = copy.deepcopy(baseline)
        advanced["source_identity"] = latest_identity
        advanced["mapping_static"]["identity"] = latest_identity
        represented_files = {
            path
            for transition in advanced["additive_transitions"]
            for path in transition["mapping_static_additions"]["python_test_files"]
        }
        later_file = next(
            path
            for path in advanced["mapping_static"]["python_test_files"]
            if path not in represented_files
        )
        # Must not collide with a package that already owns a transition —
        # hardcoding one silently became a duplicate as the baseline grew.
        claimed = {row["package"] for row in advanced["additive_transitions"]}
        later_package = next(
            name
            for name in ("PKG-11-RETRIEVAL", "PKG-11-SETTINGS", "PKG-12-FE-SHELL")
            if name not in claimed
        )
        advanced["additive_transitions"].append(
            {
                "package": later_package,
                "root": "multiple",
                "source_identity_before": baseline["source_identity"],
                "source_identity_after": latest_identity,
                "collected_roots": {},
                "mapping_static_additions": {
                    "python_test_files": [later_file],
                    "python_static_test_ids": [],
                    "typescript_test_files": [],
                    "typescript_static_test_ids": [],
                    "fixtures": [],
                },
            }
        )
        advanced["additive_transitions"].sort(key=_canonical_row)
        result = _check_exact_live(monkeypatch, advanced, baseline)
        assert result["ok"] is True, result["problems"]

        later_collected = (
            "tests/architecture/test_public_api.py::"
            "TestExtractInitSurface::test_later_package_addition"
        )
        later_static = "tests/architecture/test_public_api.py::test_later_package_addition"
        advanced["collected"]["roots"]["tests"].append(later_collected)
        advanced["collected"]["roots"]["tests"].sort()
        advanced["collected"]["counts"]["tests"] += 1
        advanced["collected"]["total"] += 1
        advanced["mapping_static"]["python_static_test_ids"].append(later_static)
        advanced["mapping_static"]["python_static_test_ids"].sort()
        advanced["mapping_static"]["python_static_test_id_count"] += 1
        later_transition = _transition_for(advanced, later_package)
        later_transition["root"] = "tests"
        later_transition["collected_roots"] = {
            "tests": {
                "before_count": baseline["collected"]["counts"]["tests"],
                "after_count": advanced["collected"]["counts"]["tests"],
                "added_ids": [later_collected],
            }
        }
        later_transition["mapping_static_additions"]["python_static_test_ids"] = [later_static]
        # Mutating a row in place changes its canonical key, so the list must be
        # re-sorted exactly as it was after the append above; the authority
        # requires canonical order and this fixture must satisfy it to exercise
        # the assertion below rather than the sort check.
        advanced["additive_transitions"].sort(key=_canonical_row)
        result = _check_exact_live(monkeypatch, advanced, advanced)
        assert result["ok"] is True, result["problems"]

        forged = copy.deepcopy(baseline)
        forged_identity = "f" * 40
        forged["source_identity"] = forged_identity
        forged["mapping_static"]["identity"] = forged_identity
        _transition_for(forged, "PKG-03-HARNESS-TRANSPORT")["source_identity_after"] = (
            forged_identity
        )

        mismatched_identity = copy.deepcopy(baseline)
        mismatched_identity["mapping_static"]["identity"] = (
            "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"
        )

        arbitrary = copy.deepcopy(baseline)
        arbitrary["additive_transitions"] = [{"anything": "goes"}]

        wrong_container = copy.deepcopy(baseline)
        wrong_container["additive_transitions"] = {"anything": "goes"}

        wrong_counts = copy.deepcopy(baseline)
        _transition_for(wrong_counts, "PKG-02-GATE")["collected_roots"]["tests"]["before_count"] = (
            24
        )

        forged_set = copy.deepcopy(baseline)
        forged_ids = _transition_for(forged_set, "PKG-02-GATE")["collected_roots"]["tests"][
            "added_ids"
        ]
        forged_ids[0] = "tests/forged.py::test_forged"
        forged_ids.sort()

        substituted_collected = copy.deepcopy(baseline)
        collected_additions = _transition_for(substituted_collected, "PKG-02-GATE")[
            "collected_roots"
        ]["tests"]["added_ids"]
        accepted_collected = next(
            node_id
            for node_id in baseline["collected"]["roots"]["tests"]
            if node_id not in set(collected_additions)
        )
        collected_additions[0] = accepted_collected
        collected_additions.sort()

        substituted_static = copy.deepcopy(baseline)
        static_additions = _transition_for(substituted_static, "PKG-02-GATE")[
            "mapping_static_additions"
        ]["python_static_test_ids"]
        accepted_static = next(
            node_id
            for node_id in baseline["mapping_static"]["python_static_test_ids"]
            if node_id not in set(static_additions)
        )
        static_additions[0] = accepted_static
        static_additions.sort()

        same_identity = copy.deepcopy(baseline)
        same_identity_transition = _transition_for(same_identity, "PKG-03-HARNESS-TRANSPORT")
        same_identity_transition["source_identity_before"] = same_identity_transition[
            "source_identity_after"
        ]

        unrepresented_identity = copy.deepcopy(baseline)
        unrepresented_identity["source_identity"] = latest_identity
        unrepresented_identity["mapping_static"]["identity"] = latest_identity

        duplicate = copy.deepcopy(baseline)
        duplicate["additive_transitions"].append(
            copy.deepcopy(_transition_for(duplicate, "PKG-02-GATE"))
        )

        overlap = copy.deepcopy(baseline)
        overlapping_row = copy.deepcopy(_transition_for(overlap, "PKG-02-GATE"))
        overlapping_row["package"] = "PKG-04-STORES"
        overlap["additive_transitions"].append(overlapping_row)

        cases = (
            (
                "nonexistent source identity",
                forged,
                ("source_identity", "does not resolve"),
            ),
            (
                "mapping/source mismatch",
                mismatched_identity,
                ("mapping_static.identity", "must equal"),
            ),
            (
                "arbitrary transition",
                arbitrary,
                ("additive_transitions[0]", "schema mismatch"),
            ),
            (
                "non-list transition authority",
                wrong_container,
                ("additive_transitions", "exact object list"),
            ),
            (
                "transition count mismatch",
                wrong_counts,
                ("count/addition mismatch",),
            ),
            (
                "transition addition outside live IDs",
                forged_set,
                ("absent from the current inventory", "test_forged"),
            ),
            (
                "old collected ID substituted as PKG-02 addition",
                substituted_collected,
                ("collected additions", "frozen live test IDs"),
            ),
            (
                "old static ID substituted as PKG-02 addition",
                substituted_static,
                ("static additions", "frozen live test IDs"),
            ),
            (
                "same before and after identity",
                same_identity,
                ("before/after identities must differ",),
            ),
            (
                "unrepresented current identity",
                unrepresented_identity,
                ("latest additive-transition identity",),
            ),
            (
                "duplicate transition",
                duplicate,
                ("duplicate additive-transition package",),
            ),
            (
                "overlapping package additions",
                overlap,
                ("additions overlap across packages",),
            ),
        )
        for label, stored, expected in cases:
            result = _check_exact_live(monkeypatch, stored, baseline)
            assert result["ok"] is False, label
            assert_problem_contains(result["problems"], *expected)


class TestSandboxDeselections:
    def test_five_sandbox_deselections_listed(self):
        expected = (
            "packages/tools/tests/test_sandbox_integration.py::"
            "test_real_hostconfig_pidmode_is_never_host",
            "packages/tools/tests/test_sandbox_integration.py::"
            "test_pid_and_net_namespace_inodes_differ_from_host",
            "packages/tools/tests/test_sandbox_integration.py::"
            "test_sealed_box_cannot_reach_host_loopback",
            "packages/tools/tests/test_sandbox_integration.py::"
            "test_host_process_survives_every_signal_bypass_form",
            "packages/tools/tests/test_sandbox_integration.py::"
            "test_guest_symlink_escape_refused_live",
        )
        assert test_inventory.SANDBOX_INTEGRATION_DESELECTED_IDS == expected
        assert len(set(expected)) == 5

    def test_sandbox_deselections_are_specific_files(self, monkeypatch: pytest.MonkeyPatch):
        expected = sorted(test_inventory.SANDBOX_INTEGRATION_DESELECTED_IDS)
        assert all(
            node_id.startswith("packages/tools/tests/test_sandbox_integration.py::test_")
            and "*" not in node_id
            for node_id in expected
        )
        monkeypatch.setattr(
            test_inventory,
            "_collect_pytest_ids",
            lambda _root, root_dir, marker="": (
                (
                    expected[:-1],
                    "",
                )
                if (root_dir, marker) == ("packages", "sandbox_integration")
                else ([], "unexpected collector invocation")
            ),
        )
        problems: list[str] = []
        test_inventory._check_sandbox_deselections(REPO_ROOT, problems)
        assert_problem_contains(problems, "sandbox_integration", "mismatch", expected[-1])


class TestInventoryRules:
    def test_inventory_rules_present(self):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        assert baseline["inventory_rules"] == {
            "any_unexplained_deletion_fails": True,
            "deselection_fails": True,
            "skip_xfail_todo_only_growth_fails": True,
            "command_drift_fails": True,
            "additions_allowed_only_when_baseline_updated_in_owning_package": True,
            "real_collection_distinct_from_mapping_static": True,
            "module_splits_require_an_exact_transition_record": True,
        }

    def test_additive_transitions_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        transitions = [
            transition
            for transition in baseline["additive_transitions"]
            if transition["package"] == "PKG-02-GATE"
        ]
        assert len(transitions) == 1
        transition = transitions[0]
        assert transition["root"] == "tests"
        assert transition["source_identity_before"] == ("1cf00dbe194a2a276ea1fd17ab74589355f2e0dc")
        retained_after = transition["source_identity_after"]
        assert re.fullmatch(r"[0-9a-f]{40}", retained_after)
        assert test_inventory.commit_identity_resolves(REPO_ROOT, retained_after)
        roots = transition["collected_roots"]
        assert set(roots) == {"tests"}
        tests = roots["tests"]
        assert (tests["before_count"], tests["after_count"]) == (25, 327)
        assert len(tests["added_ids"]) == 302
        assert tests["added_ids"] == sorted(set(tests["added_ids"]))
        assert tests["after_count"] - tests["before_count"] == len(tests["added_ids"])
        static = transition["mapping_static_additions"]
        assert set(static) == {
            "python_test_files",
            "python_static_test_ids",
            "typescript_test_files",
            "typescript_static_test_ids",
            "fixtures",
        }
        assert len(static["python_test_files"]) == 14
        assert len(static["python_static_test_ids"]) == 302
        assert static["python_test_files"] == sorted(set(static["python_test_files"]))
        assert static["python_static_test_ids"] == sorted(static["python_static_test_ids"])
        assert all(
            static[key] == []
            for key in (
                "typescript_test_files",
                "typescript_static_test_ids",
                "fixtures",
            )
        )

        assert inventory_static._DERIVED_PATHS == {
            "architecture/public-api.json",
            "architecture/test-inventory.json",
            "docs/governance/CAMPAIGN-STATUS.md",
            "docs/governance/PROTECTED.sha256",
        }
        _git(tmp_path, "init", "-q")
        write(tmp_path / "README.md", "lineage fixture\n")
        base = _checkpoint(tmp_path, "base")
        accepted = {
            "schema": "disclaude-architecture-test-inventory-v1",
            "source_identity": "accepted",
        }
        accepted_text = json.dumps(accepted, indent=2) + "\n"
        authority = tmp_path / "architecture/test-inventory.json"
        write(authority, accepted_text)
        source = _checkpoint(tmp_path, "source")
        monkeypatch.setattr(inventory_static, "_PKG02_BEFORE", base)
        monkeypatch.setattr(inventory_static, "_ACCEPTED_INVENTORY_COMMIT", source)
        monkeypatch.setattr(
            inventory_static,
            "_ACCEPTED_INVENTORY_SHA256",
            hashlib.sha256(accepted_text.encode()).hexdigest(),
        )

        assert inventory_static.candidate_prior(tmp_path, source) == accepted
        assert inventory_static.regeneration_prior(tmp_path, source) == (
            source,
            accepted,
        )
        write(authority, json.dumps({**accepted, "forged": True}) + "\n")
        with pytest.raises(RuntimeError, match="working prewrite inventory"):
            inventory_static.regeneration_prior(tmp_path, source)

        candidate = {**accepted, "source_identity": source}
        write(authority, json.dumps(candidate, indent=2) + "\n")
        _git(tmp_path, "add", ".")
        candidate_tree = _git(tmp_path, "write-tree")
        descendant = _tree_commit(
            tmp_path,
            candidate_tree,
            source,
            "forged descendant",
        )
        unchanged_descendant = _tree_commit(
            tmp_path,
            _git(tmp_path, "rev-parse", f"{source}^{{tree}}"),
            source,
            "unchanged descendant",
        )
        final = _tree_commit(tmp_path, candidate_tree, base, "final sibling")

        _git(tmp_path, "update-ref", "HEAD", descendant)
        with pytest.raises(RuntimeError, match="changed its parent"):
            inventory_static.candidate_prior(tmp_path, descendant)
        with pytest.raises(RuntimeError, match="not the source sibling"):
            inventory_static.candidate_prior(tmp_path, source)
        _git(tmp_path, "update-ref", "HEAD", source)
        with pytest.raises(RuntimeError, match="not the source sibling"):
            inventory_static.candidate_prior(tmp_path, unchanged_descendant)
        _git(tmp_path, "update-ref", "HEAD", final)
        assert _git(tmp_path, "diff", "--name-only", source, final) == (
            "architecture/test-inventory.json"
        )
        assert inventory_static.candidate_prior(tmp_path, source) == accepted
        with pytest.raises(RuntimeError, match="must equal HEAD"):
            inventory_static.regeneration_prior(tmp_path, source)

        write(tmp_path / "unrelated.py", "VALUE = 1\n")
        _git(tmp_path, "add", ".")
        unrelated = _tree_commit(
            tmp_path,
            _git(tmp_path, "write-tree"),
            base,
            "unrelated sibling",
        )
        _git(tmp_path, "update-ref", "HEAD", unrelated)
        with pytest.raises(RuntimeError, match="non-derived drift"):
            inventory_static.candidate_prior(tmp_path, source)


class TestCollectorFailure:
    def test_collection_env_sets_pythonpath(self, tmp_path: Path) -> None:
        env = test_inventory._collection_env(tmp_path)
        assert env["PYTHONPATH"] == str(tmp_path)
        assert env["PYTEST_ADDOPTS"] == ""

    def test_collection_roots_are_four(self):
        assert test_inventory.PYTHON_ROOTS == (
            "packages",
            "harness",
            "integrations",
            "tests",
        )
        commands = test_inventory._collector_commands()
        assert commands["pytest"] == [
            "python",
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-m",
            "<marker>",
            "--collect-only",
            "-q",
            "<root>",
        ]
        assert set(commands["playwright"]) == set(test_inventory.PLAYWRIGHT_CONFIGS)

    def test_collector_failure_detected_in_temp_repo(self, tmp_path: Path) -> None:
        write(tmp_path / "tests/test_broken.py", "def test_broken(:\n    pass\n")
        ids, error = test_inventory._collect_pytest_ids(tmp_path, "tests")
        assert ids == []
        assert "collection failed for tests" in error
        assert "exit" in error

    def test_valid_test_file_collects_ids(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        write(tmp_path / "tests/test_ok.py", "def test_pass():\n    assert 2 + 2 == 4\n")
        ids, error = test_inventory._collect_pytest_ids(tmp_path, "tests")
        assert error == ""
        assert ids == ["tests/test_ok.py::test_pass"]

        monkeypatch.setattr(
            test_inventory.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=("tests/test_ok.py::test_pass\ntests/test_ok.py::test_pass\n"),
                stderr="",
            ),
        )
        ids, error = test_inventory._collect_pytest_ids(tmp_path, "tests")
        assert ids == []
        assert "duplicate node IDs" in error


class TestDeselectionDetection:
    def test_deselection_rule_present(self, monkeypatch: pytest.MonkeyPatch):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        assert baseline["inventory_rules"]["deselection_fails"] is True
        expected = sorted(test_inventory.SANDBOX_INTEGRATION_DESELECTED_IDS)
        monkeypatch.setattr(
            test_inventory,
            "_collect_pytest_ids",
            lambda _root, root_dir, marker="": (
                (
                    copy.deepcopy(expected),
                    "",
                )
                if (root_dir, marker) == ("packages", "sandbox_integration")
                else ([], "unexpected collector invocation")
            ),
        )
        problems: list[str] = []
        test_inventory._check_sandbox_deselections(REPO_ROOT, problems)
        assert problems == []

    def test_command_drift_rule_present(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        assert baseline["inventory_rules"]["command_drift_fails"] is True
        ids, files, error = test_inventory._collect_playwright(tmp_path, "unknown.config.ts")
        assert (ids, files) == ([], [])
        assert "unknown playwright config" in error
        ids, files, error = test_inventory._collect_playwright(tmp_path, "playwright.config.ts")
        assert (ids, files) == ([], [])
        assert "executable or config missing" in error

        frontend = tmp_path / "frontend"
        write(frontend / "node_modules/.bin/playwright", "")
        write(frontend / "playwright.config.ts", "export default {};\n")
        monkeypatch.setattr(
            test_inventory.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=("  [chromium] › sample.spec.ts:3:4 › works\nTotal: 1 test in 1 file\n"),
                stderr="",
            ),
        )
        ids, files, error = test_inventory._collect_playwright(tmp_path, "playwright.config.ts")
        assert error == ""
        assert ids == ["frontend/e2e/sample.spec.ts::works"]
        assert files == ["frontend/e2e/sample.spec.ts"]

        monkeypatch.setattr(
            test_inventory.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=("  [chromium] › sample.spec.ts:3:4 › works\nTotal: 2 tests in 1 file\n"),
                stderr="",
            ),
        )
        ids, files, error = test_inventory._collect_playwright(tmp_path, "playwright.config.ts")
        assert (ids, files) == ([], [])
        assert "parse mismatch" in error

    def test_additions_require_baseline_update(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        baseline = test_inventory.load_test_inventory(REPO_ROOT)
        assert (
            baseline["inventory_rules"][
                "additions_allowed_only_when_baseline_updated_in_owning_package"
            ]
            is True
        )
        assert test_inventory._assert_no_deletions(
            "tests",
            ["tests/test_a.py::test_a"],
            [
                "tests/test_a.py::test_a",
                "tests/test_b.py::test_b",
            ],
        ) == ["tests/test_b.py::test_b"]
        with pytest.raises(RuntimeError, match="unexplained deletion"):
            test_inventory._assert_no_deletions(
                "tests",
                ["tests/test_a.py::test_a"],
                ["tests/test_b.py::test_b"],
            )
        accepted_mapping = dict(baseline["mapping_static"])
        accepted_mapping["identity"] = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"
        _, _, _, accepted_identity = test_inventory._accepted_authorities(
            REPO_ROOT,
            baseline["collected"]["roots"],
            accepted_mapping,
        )
        assert accepted_identity == accepted_mapping["identity"]
        accepted_mapping["identity"] = ""
        with pytest.raises(
            RuntimeError,
            match="accepted mapping-static source identity",
        ):
            test_inventory._accepted_authorities(
                REPO_ROOT,
                baseline["collected"]["roots"],
                accepted_mapping,
            )
        accepted_mapping["identity"] = "f" * 40
        with pytest.raises(
            RuntimeError,
            match="must resolve to a Git commit",
        ):
            test_inventory._accepted_authorities(
                REPO_ROOT,
                baseline["collected"]["roots"],
                accepted_mapping,
            )
        authority = REPO_ROOT / "architecture/test-inventory.json"
        before = authority.read_bytes()
        with pytest.raises(RuntimeError, match="source identity must resolve"):
            test_inventory.regenerate_inventory(
                REPO_ROOT,
                source_identity="f" * 40,
            )
        assert authority.read_bytes() == before

        temp_authority = tmp_path / "architecture/test-inventory.json"
        regeneration_baseline = copy.deepcopy(baseline)
        regeneration_baseline["additive_transitions"] = []
        # tmp_path is not a Git repo, so no accepting commit resolves
        # there; this test exercises the PKG-02 relation, not splits.
        regeneration_baseline["module_split_transitions"] = []
        write(temp_authority, json.dumps(regeneration_baseline, indent=2) + "\n")
        next_identity = _git(REPO_ROOT, "rev-parse", "HEAD")
        monkeypatch.setattr(
            inventory_static,
            "regeneration_prior",
            lambda _root, identity: (identity, baseline),
        )
        monkeypatch.setattr(
            test_inventory,
            "accepted_mapping_identity",
            lambda _previous, _root, mapping, *, explicit: mapping["identity"],
        )
        monkeypatch.setattr(
            test_inventory,
            "scan_mapping_static",
            lambda _root: _mapping_actual(baseline),
        )
        monkeypatch.setattr(
            test_inventory,
            "_collect_frontend",
            lambda _root: _frontend_actual(baseline),
        )
        monkeypatch.setattr(
            test_inventory,
            "_config_identities",
            lambda _root: copy.deepcopy(baseline["frontend_real_collection"]["config_identities"]),
        )

        def collect(_root: Path, root_dir: str, marker: str = ""):
            if (root_dir, marker) == ("packages", "sandbox_integration"):
                return sorted(test_inventory.SANDBOX_INTEGRATION_DESELECTED_IDS), ""
            return copy.deepcopy(baseline["collected"]["roots"][root_dir]), ""

        monkeypatch.setattr(test_inventory, "_collect_pytest_ids", collect)
        before = temp_authority.read_bytes()
        with pytest.raises(RuntimeError, match="exact additive transition"):
            test_inventory.regenerate_inventory(tmp_path, next_identity)
        assert temp_authority.read_bytes() == before

        pkg02_live = _without_later_transition_additions(baseline)
        monkeypatch.setattr(
            test_inventory,
            "scan_mapping_static",
            lambda _root: _mapping_actual(pkg02_live),
        )

        def collect_pkg02(_root: Path, root_dir: str, marker: str = ""):
            if (root_dir, marker) == ("packages", "sandbox_integration"):
                return sorted(test_inventory.SANDBOX_INTEGRATION_DESELECTED_IDS), ""
            return copy.deepcopy(pkg02_live["collected"]["roots"][root_dir]), ""

        monkeypatch.setattr(test_inventory, "_collect_pytest_ids", collect_pkg02)

        transition = _transition_for(pkg02_live, "PKG-02-GATE")
        accepted_roots = copy.deepcopy(pkg02_live["collected"]["roots"])
        added_ids = set(transition["collected_roots"]["tests"]["added_ids"])
        accepted_roots["tests"] = [
            node_id for node_id in accepted_roots["tests"] if node_id not in added_ids
        ]
        accepted_mapping = copy.deepcopy(pkg02_live["mapping_static"])
        additions = transition["mapping_static_additions"]
        for key in ("python_test_files", "python_static_test_ids"):
            added = set(additions[key])
            accepted_mapping[key] = [item for item in accepted_mapping[key] if item not in added]
        accepted_mapping["identity"] = inventory_static._PKG02_BEFORE
        monkeypatch.setattr(
            inventory_static,
            "commit_identity_resolves",
            lambda _root, _identity: True,
        )

        substituted_roots = copy.deepcopy(accepted_roots)
        substituted_roots["tests"][0] = transition["collected_roots"]["tests"]["added_ids"][0]
        substituted_roots["tests"].sort()
        substituted_mapping = copy.deepcopy(accepted_mapping)
        substituted_mapping["python_static_test_ids"][0] = additions["python_static_test_ids"][0]
        substituted_mapping["python_static_test_ids"].sort()
        before = temp_authority.read_bytes()
        with pytest.raises(RuntimeError, match="frozen live test IDs") as error:
            test_inventory.regenerate_inventory(
                tmp_path,
                next_identity,
                accepted_collected_roots=substituted_roots,
                accepted_mapping_static=substituted_mapping,
            )
        assert "static additions" in str(error.value)
        assert temp_authority.read_bytes() == before

        result = test_inventory.regenerate_inventory(
            tmp_path,
            next_identity,
            accepted_collected_roots=accepted_roots,
            accepted_mapping_static=accepted_mapping,
        )
        updated = test_inventory.load_test_inventory(tmp_path)
        rebuilt = copy.deepcopy(transition)
        rebuilt["source_identity_after"] = next_identity
        assert result["transition_added_ids"] == 302
        assert updated["additive_transitions"] == [rebuilt]
        assert updated["source_identity"] == next_identity
        assert updated["mapping_static"]["identity"] == next_identity
        assert inventory_static.check_inventory_metadata(updated, REPO_ROOT) == []

    def test_collection_uses_not_sandbox_integration_marker(self, tmp_path: Path) -> None:
        write(
            tmp_path / "pytest.ini",
            "[pytest]\nmarkers = sandbox_integration: live sandbox test\n",
        )
        write(
            tmp_path / "tests/test_sandbox.py",
            "import pytest\n"
            "@pytest.mark.sandbox_integration\n"
            "def test_sandbox():\n"
            "    assert False\n",
        )
        write(
            tmp_path / "tests/test_normal.py",
            "def test_normal():\n    assert True\n",
        )
        ids, error = test_inventory._collect_pytest_ids(tmp_path, "tests")
        assert error == ""
        assert ids == ["tests/test_normal.py::test_normal"]


class TestNullAdvance:
    """A row owning no addition must PIN every root, never assert nothing."""

    def _counts(self) -> dict[str, int]:
        return dict.fromkeys(sorted(_transitions.PYTHON_ROOTS), 7)

    def _problems(self, row: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        _transitions.check_null_advance(row, problems)
        return problems

    def test_pinned_null_row_is_accepted(self):
        assert self._problems(null_advance_row(self._counts())) == []

    def test_empty_collected_roots_is_refused(self):
        row = null_advance_row(self._counts())
        row["collected_roots"] = {}
        assert_problem_contains(self._problems(row), "pin every collected root")

    def test_missing_root_is_refused(self):
        counts = self._counts()
        del counts["harness"]
        row = null_advance_row(counts)
        assert_problem_contains(self._problems(row), "pin every collected root")

    def test_count_change_is_refused(self):
        row = null_advance_row(self._counts())
        row["collected_roots"]["tests"]["after_count"] = 8
        assert_problem_contains(
            self._problems(row), "collected_roots.tests", "must not change its collected count"
        )

    def test_relocation_is_refused(self):
        row = null_advance_row(self._counts())
        row["collected_roots"]["packages"]["relocated_count"] = 1
        assert_problem_contains(
            self._problems(row), "collected_roots.packages", "must not relocate a test"
        )
