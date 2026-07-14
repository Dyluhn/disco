"""P8D tests: the targeted-edit + manual-edit-preservation oracles — pass, fail, absent→SKIP,
present-but-malformed→FAIL(EDIT_ORACLE_EVIDENCE_MALFORMED), and the SKIP-safe classify() wiring."""

from __future__ import annotations

from _eventlog import clean_smoke_log

from harness.build_soak import failure_codes as fc
from harness.build_soak.classify import classify
from harness.build_soak.oracles import (
    TARGETED_EDIT_ORACLES,
    CommentAnchorOracle,
    ManualEditPreservationOracle,
    RewriteAvoidanceOracle,
    ScreenLabelOracle,
    TargetedEditOracle,
)


def _scn():
    return {"id": "s", "assertions": {"event_chain": {"require_plan_before_execution": True}}}


def _one(oracle, ev):
    return oracle().check(product_evidence=ev)[0]


# --- TargetedEditOracle -------------------------------------------------------
def test_targeted_edit_subset_passes() -> None:
    r = _one(
        TargetedEditOracle,
        {
            "targeted_edit": {
                "edited_files": ["index.html"],
                "expected_files": ["index.html", "style.css"],
            }
        },
    )
    assert r.passed


def test_targeted_edit_unexpected_file_fails() -> None:
    r = _one(
        TargetedEditOracle,
        {
            "targeted_edit": {
                "edited_files": ["index.html", "secrets.env"],
                "expected_files": ["index.html"],
            }
        },
    )
    assert r.failed and r.code == fc.TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES
    assert r.facts["unexpected"] == ["secrets.env"]


def test_targeted_edit_absent_skips() -> None:
    assert _one(TargetedEditOracle, None).skipped


def test_targeted_edit_malformed_fails_closed() -> None:
    r = _one(
        TargetedEditOracle, {"targeted_edit": {"edited_files": "index.html", "expected_files": []}}
    )
    assert r.failed and r.code == fc.EDIT_ORACLE_EVIDENCE_MALFORMED


# --- RewriteAvoidanceOracle ---------------------------------------------------
def test_small_edit_within_bound_passes() -> None:
    r = _one(
        RewriteAvoidanceOracle,
        {
            "rewrite_avoidance": {
                "edit_scope": "small",
                "changed_lines": 3,
                "total_lines": 100,
                "max_churn_ratio": 0.20,
            }
        },
    )
    assert r.passed and r.facts["churn_ratio"] == 0.03


def test_small_cta_edit_full_rewrite_fails() -> None:
    r = _one(
        RewriteAvoidanceOracle,
        {
            "rewrite_avoidance": {
                "edit_scope": "small",
                "changed_lines": 90,
                "total_lines": 100,
                "max_churn_ratio": 0.20,
            }
        },
    )
    assert r.failed and r.code == fc.SMALL_EDIT_FULL_REWRITE


def test_non_small_scope_skips() -> None:
    assert _one(
        RewriteAvoidanceOracle,
        {
            "rewrite_avoidance": {
                "edit_scope": "rebuild",
                "changed_lines": 90,
                "total_lines": 100,
                "max_churn_ratio": 0.2,
            }
        },
    ).skipped


def test_rewrite_absent_skips() -> None:
    assert _one(RewriteAvoidanceOracle, None).skipped


def test_rewrite_zero_total_and_missing_bound_fail_closed() -> None:
    z = _one(
        RewriteAvoidanceOracle,
        {
            "rewrite_avoidance": {
                "edit_scope": "small",
                "changed_lines": 0,
                "total_lines": 0,
                "max_churn_ratio": 0.2,
            }
        },
    )
    assert z.failed and z.code == fc.EDIT_ORACLE_EVIDENCE_MALFORMED
    m = _one(
        RewriteAvoidanceOracle,
        {"rewrite_avoidance": {"edit_scope": "small", "changed_lines": 1, "total_lines": 10}},
    )
    assert m.failed and m.code == fc.EDIT_ORACLE_EVIDENCE_MALFORMED


# --- ManualEditPreservationOracle ---------------------------------------------
def test_manual_override_preserved_passes() -> None:
    r = _one(
        ManualEditPreservationOracle,
        {
            "manual_edit": {
                "overrides": {"index.html": "MY HERO"},
                "final_files": {"index.html": "<h1>MY HERO</h1>"},
            }
        },
    )
    assert r.passed


def test_manual_override_clobbered_fails() -> None:
    r = _one(
        ManualEditPreservationOracle,
        {
            "manual_edit": {
                "overrides": {"index.html": "MY HERO"},
                "final_files": {"index.html": "<h1>generic</h1>"},
            }
        },
    )
    assert (
        r.failed and r.code == fc.MANUAL_EDIT_CLOBBERED and r.facts["clobbered"] == ["index.html"]
    )


def test_manual_absent_skips_and_malformed_fails() -> None:
    assert _one(ManualEditPreservationOracle, None).skipped
    r = _one(ManualEditPreservationOracle, {"manual_edit": {"overrides": ["x"], "final_files": {}}})
    assert r.failed and r.code == fc.EDIT_ORACLE_EVIDENCE_MALFORMED


# --- CommentAnchorOracle ------------------------------------------------------
def test_anchor_preserved_through_text_edit_passes() -> None:
    assert _one(
        CommentAnchorOracle,
        {"comment_anchors": {"before": ["hero", "lead"], "after": ["hero", "lead"]}},
    ).passed


def test_anchor_survives_section_reorder_passes() -> None:
    # reorder changes order, not membership → still PASS (set, not position)
    assert _one(
        CommentAnchorOracle,
        {"comment_anchors": {"before": ["hero", "lead"], "after": ["lead", "hero"]}},
    ).passed


def test_anchor_lost_fails() -> None:
    r = _one(
        CommentAnchorOracle, {"comment_anchors": {"before": ["hero", "lead"], "after": ["hero"]}}
    )
    assert r.failed and r.code == fc.COMMENT_ANCHOR_LOST and r.facts["lost"] == ["lead"]


def test_anchor_absent_skips_and_malformed_fails() -> None:
    assert _one(CommentAnchorOracle, None).skipped
    assert (
        _one(CommentAnchorOracle, {"comment_anchors": {"before": "hero", "after": []}}).code
        == fc.EDIT_ORACLE_EVIDENCE_MALFORMED
    )


# --- ScreenLabelOracle --------------------------------------------------------
def test_unedited_section_label_stable_passes() -> None:
    r = _one(
        ScreenLabelOracle,
        {
            "screen_labels": {
                "edited_sections": ["hero"],
                "before": {"hero": "hero", "about": "about"},
                "after": {"hero": "hero-new", "about": "about"},
            }
        },
    )
    assert r.passed  # 'about' (unedited) stable; 'hero' (edited) may change


def test_unedited_section_label_changed_fails() -> None:
    r = _one(
        ScreenLabelOracle,
        {
            "screen_labels": {
                "edited_sections": ["hero"],
                "before": {"about": "about"},
                "after": {"about": "about-2"},
            }
        },
    )
    assert r.failed and r.code == fc.SCREEN_LABEL_UNSTABLE and r.facts["unstable"] == ["about"]


def test_screen_label_absent_skips() -> None:
    assert _one(ScreenLabelOracle, None).skipped


# --- classify() SKIP-safe wiring (P8D oracles do not regress a clean run) ------
def test_classify_unaffected_without_edit_evidence() -> None:
    # no edit slices → every P8D oracle SKIPs → still PASS (no false FAIL)
    assert classify(clean_smoke_log(), scenario=_scn())["status"] == "PASS"


def test_classify_fails_on_unexpected_edit_file() -> None:
    ev = {
        "targeted_edit": {
            "edited_files": ["index.html", "id_rsa"],
            "expected_files": ["index.html"],
        }
    }
    c = classify(clean_smoke_log(), scenario=_scn(), product_evidence=ev)
    assert c["status"] == "FAIL" and c["code"] == fc.TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES


def test_classify_malformed_edit_evidence_is_invalid_run() -> None:
    ev = {"targeted_edit": {"edited_files": "nope", "expected_files": []}}
    c = classify(clean_smoke_log(), scenario=_scn(), product_evidence=ev)
    assert c["status"] == "INVALID_RUN"  # harness-validity class, never a silent pass


def test_all_five_oracles_are_wired() -> None:
    assert len(TARGETED_EDIT_ORACLES) == 5


# --- P8D code-review fixes: present-non-dict + strict types (no silent pass) ---
_SLICE_KEY = {
    TargetedEditOracle: "targeted_edit",
    RewriteAvoidanceOracle: "rewrite_avoidance",
    ManualEditPreservationOracle: "manual_edit",
    CommentAnchorOracle: "comment_anchors",
    ScreenLabelOracle: "screen_labels",
}


def test_present_non_dict_slice_is_malformed_for_every_oracle() -> None:
    # a present-but-not-a-dict slice (e.g. []/None/str) must FAIL malformed, NOT look absent
    for oracle, key in _SLICE_KEY.items():
        for bad in ([], None, "x", 3):
            r = _one(oracle, {key: bad})
            assert r.failed and r.code == fc.EDIT_ORACLE_EVIDENCE_MALFORMED, (oracle.__name__, bad)


def test_bool_is_not_accepted_as_int_for_churn() -> None:
    r = _one(
        RewriteAvoidanceOracle,
        {
            "rewrite_avoidance": {
                "edit_scope": "small",
                "changed_lines": True,
                "total_lines": 100,
                "max_churn_ratio": 0.2,
            }
        },
    )
    assert r.failed and r.code == fc.EDIT_ORACLE_EVIDENCE_MALFORMED


def test_non_string_list_and_dict_payloads_are_malformed() -> None:
    assert (
        _one(
            TargetedEditOracle,
            {"targeted_edit": {"edited_files": [123], "expected_files": ["123"]}},
        ).code
        == fc.EDIT_ORACLE_EVIDENCE_MALFORMED
    )
    assert (
        _one(CommentAnchorOracle, {"comment_anchors": {"before": [1, 2], "after": []}}).code
        == fc.EDIT_ORACLE_EVIDENCE_MALFORMED
    )
    assert (
        _one(
            ManualEditPreservationOracle,
            {"manual_edit": {"overrides": {"a": 1}, "final_files": {}}},
        ).code
        == fc.EDIT_ORACLE_EVIDENCE_MALFORMED
    )


def test_screen_label_malformed_fails_closed() -> None:
    r = _one(
        ScreenLabelOracle, {"screen_labels": {"edited_sections": "hero", "before": {}, "after": {}}}
    )
    assert r.failed and r.code == fc.EDIT_ORACLE_EVIDENCE_MALFORMED


def test_classify_malformed_is_invalid_run_for_each_slice() -> None:
    # malformed evidence in ANY P8D slice → INVALID_RUN (never a silent PASS)
    for key in _SLICE_KEY.values():
        c = classify(clean_smoke_log(), scenario=_scn(), product_evidence={key: []})
        assert c["status"] == "INVALID_RUN", key
