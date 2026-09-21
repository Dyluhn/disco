"""PKG-03-EDIT-EVIDENCE — the five edit-evidence producers, and the proof each is essential.

Two things are established here, and the second is the one that matters.

**Conformance.** The producer's output satisfies the dossier writer's schema and drives all
five P8D oracles from SKIP to an adjudication. Before this package those five had never
adjudicated in either campaign.

**Whether a producer matters.** A producer that emits a well-formed slice regardless of what the
product did would turn every one of those adjudications into a rubber stamp. So each
producer gets a NEGATIVE CONTROL: take the same real capture, nullify exactly the one
behaviour that producer observes, and assert the oracle's verdict CHANGES. A control that
compares two empty outputs proves nothing, so every control starts from the passing capture
and perturbs one field of it.

The fail-closed path (present-but-malformed → EDIT_ORACLE_EVIDENCE_MALFORMED → INVALID_RUN)
is asserted to still exist, because the value of these oracles is that they refuse to
mis-adjudicate, not that they are easy to satisfy.
"""

from __future__ import annotations

import pytest
from harness.build_soak import failure_codes as fc
from harness.build_soak.oracles import TARGETED_EDIT_ORACLES
from harness.build_soak.product_evidence import validate_product_evidence
from harness.product_build.edit_evidence import (
    EditCapture,
    EditObservationError,
    build_edit_slices,
    observe_churn,
    observe_comment_anchors,
    observe_edited_files,
    observe_override_line,
    observe_screen_labels,
)
from harness.product_build.edit_evidence_run import (
    MANUAL_OVERRIDE_SNIPPET,
    require_sections,
)

# A page shaped like the one the governed build actually produces (verified against the live
# run's own built.html): four id-carrying sections, each with an <h2> and a comment anchor.
BUILT_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Corner Bean</title></head>
<body>
  <header><h1>Corner Bean</h1></header>
  <!-- anchor: about -->
  <section id="about">
    <h2>Our Story</h2>
    <p>We have roasted on this corner since 1998.</p>
  </section>
  <!-- anchor: hours -->
  <section id="hours">
    <h2>Opening Hours</h2>
    <p>Weekdays 7am to 4pm.</p>
  </section>
  <!-- anchor: menu -->
  <section id="menu">
    <h2>The Menu</h2>
    <p>Espresso, filter, and a rotating single origin.</p>
  </section>
  <!-- anchor: contact -->
  <section id="contact">
    <h2>Find Us</h2>
    <p>12 Anywhere Street.</p>
  </section>
</body>
</html>
"""


def _seeded() -> tuple[str, str]:
    """The state the LATER edit is handed: the built page plus the user's own verbatim line,
    added through the product's follow-up surface in turn 2."""
    marker = "    <p>We have roasted on this corner since 1998.</p>"
    page = BUILT_PAGE.replace(marker, f"{marker}\n    {MANUAL_OVERRIDE_SNIPPET}")
    return page, "about"


def _edited(before: str) -> str:
    """What a well-behaved TARGETED edit to the `hours` section leaves behind."""
    return before.replace("Weekdays 7am to 4pm.", "Weekdays 7am to 6pm, weekends 8am to 5pm.")


def _capture(before: str, after: str, **overrides: object) -> EditCapture:
    fields: dict[str, object] = {
        "path": "index.html",
        "before": before,
        "after": after,
        "final_files": {"index.html": after},
        "overrides": {"index.html": MANUAL_OVERRIDE_SNIPPET},
        "edited_files": ("index.html",),
        "expected_files": ("index.html",),
        "edited_sections": ("hours",),
        "edit_scope": "small",
        "max_churn_ratio": 0.25,
    }
    fields.update(overrides)
    return EditCapture(**fields)  # type: ignore[arg-type]


def _verdicts(product_evidence: dict[str, object] | None) -> dict[str, str]:
    """Every P8D oracle's status, keyed by oracle name."""
    out: dict[str, str] = {}
    for oracle_cls in TARGETED_EDIT_ORACLES:
        for result in oracle_cls().check(product_evidence=product_evidence):
            out[result.oracle] = "FAIL" if result.failed else ("SKIP" if result.skipped else "PASS")
    return out


def _codes(product_evidence: dict[str, object]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for oracle_cls in TARGETED_EDIT_ORACLES:
        for result in oracle_cls().check(product_evidence=product_evidence):
            out[result.oracle] = result.code
    return out


# --------------------------------------------------------------------------------------
# Observation functions
# --------------------------------------------------------------------------------------


def test_the_pre_edit_state_carries_the_anchors_and_exactly_one_override() -> None:
    seeded, override_section = _seeded()
    assert observe_comment_anchors(seeded) == ["about", "contact", "hours", "menu"]
    assert seeded.count(MANUAL_OVERRIDE_SNIPPET) == 1
    # the user's line never lands in the section the later edit targets
    assert override_section == "about"


def test_the_line_the_user_asks_for_is_the_line_the_producer_checks_for() -> None:
    """If the override prompt and MANUAL_OVERRIDE_SNIPPET ever drift apart, the runner would
    ask the product for one line and adjudicate the survival of another — the pre-edit guard
    would then fail every honest run, or worse, pass on text nobody requested."""
    from harness.product_build import STATIC_SITE_EDIT_GOVERNED

    assert MANUAL_OVERRIDE_SNIPPET in STATIC_SITE_EDIT_GOVERNED.override_prompt


def test_the_adjudicated_override_is_the_line_the_PRODUCT_wrote() -> None:
    """Measured live in run r5: asked for `<p class="owner-note">Hand-written…</p>`, the
    product wrote that paragraph with an extra `<span>` inside it. The user's content was
    plainly there and only the requested BYTES differed, so the producer adjudicates the line
    it OBSERVES. Demanding the requested bytes would red an honest run over a formatting
    choice the product is entitled to make."""
    styled = (
        '  <p class="owner-note"><span class="owner-note-tag">owner-note</span>'
        "Hand-written by the owner — MANUAL-OVERRIDE-7Q4X — please keep this line.</p>  "
    )
    page = f"<html><body>\n{styled}\n</body></html>"
    observed = observe_override_line(page, "MANUAL-OVERRIDE-7Q4X")
    assert observed == styled.strip()
    assert "owner-note-tag" in observed  # the product's own formatting is carried, not erased


def test_override_line_fails_closed_when_absent_or_ambiguous() -> None:
    with pytest.raises(EditObservationError):
        observe_override_line("<html><body>nothing here</body></html>", "MARK-1")
    with pytest.raises(EditObservationError):
        observe_override_line("<p>MARK-1 one</p>\n<p>MARK-1 two</p>", "MARK-1")


def test_a_page_with_too_few_sections_is_refused() -> None:
    from harness.product_build.edit_evidence_run import LiveRunError

    with pytest.raises(LiveRunError, match="id-carrying"):
        require_sections("<html><body><section id='only'><h2>One</h2></section></body></html>")


def test_anchors_are_deduplicated_and_ignore_other_comments() -> None:
    html = "<!-- anchor: hero --><!-- not an anchor --><!--anchor:hero--><!-- anchor: foot -->"
    assert observe_comment_anchors(html) == ["foot", "hero"]


def test_screen_labels_read_the_heading_not_the_id() -> None:
    labels = observe_screen_labels(BUILT_PAGE)
    assert labels == {
        "about": "Our Story",
        "hours": "Opening Hours",
        "menu": "The Menu",
        "contact": "Find Us",
    }


def test_screen_labels_collapse_whitespace_so_a_reflow_is_not_a_change() -> None:
    one_line = '<section id="a"><h2>Opening Hours</h2></section>'
    wrapped = '<section id="a"><h2>Opening\n   Hours</h2></section>'
    assert observe_screen_labels(one_line) == observe_screen_labels(wrapped)


def test_screen_labels_fail_closed_on_an_unusable_page() -> None:
    with pytest.raises(EditObservationError):
        observe_screen_labels("   ")


def test_edited_files_counts_only_successful_calls_after_the_build() -> None:
    events = [
        # a build-phase write, BEFORE the edit boundary — not part of this edit
        {"seq": 5, "tool_call": {"tool_name": "safe_write_file", "call_id": "c0",
                                 "arguments": {"path": "index.html"}}},
        {"seq": 6, "tool_result": {"tool_name": "safe_write_file", "call_id": "c0",
                                   "success": True}},
        # the edit itself, on the workspace-prefixed form of the same path
        {"seq": 20, "tool_call": {"tool_name": "exact_replace", "call_id": "c1",
                                  "arguments": {"path": "workspace/index.html"}}},
        {"seq": 21, "tool_result": {"tool_name": "exact_replace", "call_id": "c1",
                                    "success": True}},
        # a REJECTED edit touched nothing and must not be counted
        {"seq": 22, "tool_call": {"tool_name": "exact_replace", "call_id": "c2",
                                  "arguments": {"path": "styles.css"}}},
        {"seq": 23, "tool_result": {"tool_name": "exact_replace", "call_id": "c2",
                                    "success": False}},
    ]
    assert observe_edited_files(events, after_seq=10) == ["index.html"]


def test_edited_files_reads_multi_operation_scripts() -> None:
    events = [
        {"seq": 2, "tool_call": {"tool_name": "run_project_script", "call_id": "c1",
                                 "arguments": {"operations": [{"path": "./index.html"},
                                                              {"path": "/workspace/app.js"}]}}},
        {"seq": 3, "tool_result": {"tool_name": "run_project_script", "call_id": "c1",
                                   "success": True}},
    ]
    assert observe_edited_files(events, after_seq=0) == ["app.js", "index.html"]


def test_churn_separates_a_targeted_edit_from_a_rewrite() -> None:
    before, _ = _seeded()
    small_changed, total = observe_churn(before, _edited(before))
    rewrite_changed, _ = observe_churn(before, "<html><body><p>gone</p></body></html>")
    assert small_changed / total < 0.10
    assert rewrite_changed / total > 0.80


def test_churn_fails_closed_on_an_empty_pre_edit_file() -> None:
    with pytest.raises(EditObservationError):
        observe_churn("", "anything")


def test_a_failed_seed_is_caught_before_it_becomes_evidence() -> None:
    """The override missing from the PRE-edit file means the manual edit never happened; a
    later PASS would be adjudicating the survival of something that never existed."""
    with pytest.raises(EditObservationError, match="seeding failed"):
        build_edit_slices(_capture(BUILT_PAGE, BUILT_PAGE))


# --------------------------------------------------------------------------------------
# Conformance: the producer's slices satisfy the writer and drive all five to adjudicate
# --------------------------------------------------------------------------------------


def test_producer_output_satisfies_the_writer_schema() -> None:
    before, _ = _seeded()
    assert validate_product_evidence(build_edit_slices(_capture(before, _edited(before)))) == []


def test_all_five_adjudicate_and_pass_on_a_well_behaved_edit() -> None:
    before, _ = _seeded()
    verdicts = _verdicts(build_edit_slices(_capture(before, _edited(before))))
    assert verdicts == {
        "TargetedEditOracle": "PASS",
        "RewriteAvoidanceOracle": "PASS",
        "ManualEditPreservationOracle": "PASS",
        "CommentAnchorOracle": "PASS",
        "ScreenLabelOracle": "PASS",
    }


def test_without_the_producer_all_five_skip() -> None:
    """The absence control: what these oracles did in all 2082 frozen classifications before
    this package existed. It is the producer's PRESENCE that converts SKIP to adjudication."""
    assert set(_verdicts(None).values()) == {"SKIP"}


# --------------------------------------------------------------------------------------
# Negative controls — one per producer, each nullifying exactly one observed behaviour
# --------------------------------------------------------------------------------------


def test_control_targeted_edit_an_undeclared_file_was_touched() -> None:
    before, _ = _seeded()
    slices = build_edit_slices(
        _capture(before, _edited(before), edited_files=("index.html", "styles.css"))
    )
    assert _verdicts(slices)["TargetedEditOracle"] == "FAIL"
    assert _codes(slices)["TargetedEditOracle"] == fc.TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES


def test_control_rewrite_avoidance_a_small_edit_rewrote_the_file() -> None:
    before, _ = _seeded()
    body = "\n".join(f"<p>line {i}</p>" for i in range(40))
    rewritten = f"<html><body>\n{body}\n</body></html>"
    slices = build_edit_slices(_capture(before, rewritten, overrides={}))
    assert _verdicts(slices)["RewriteAvoidanceOracle"] == "FAIL"
    assert _codes(slices)["RewriteAvoidanceOracle"] == fc.SMALL_EDIT_FULL_REWRITE


def test_control_manual_edit_the_hand_written_line_was_clobbered() -> None:
    before, _ = _seeded()
    clobbered = _edited(before).replace(MANUAL_OVERRIDE_SNIPPET, "<p>regenerated copy</p>")
    slices = build_edit_slices(_capture(before, clobbered))
    assert _verdicts(slices)["ManualEditPreservationOracle"] == "FAIL"
    assert _codes(slices)["ManualEditPreservationOracle"] == fc.MANUAL_EDIT_CLOBBERED


def test_control_comment_anchors_an_anchor_was_lost() -> None:
    before, _ = _seeded()
    lost = _edited(before).replace("<!-- anchor: menu -->\n", "")
    slices = build_edit_slices(_capture(before, lost))
    assert _verdicts(slices)["CommentAnchorOracle"] == "FAIL"
    assert _codes(slices)["CommentAnchorOracle"] == fc.COMMENT_ANCHOR_LOST


def test_control_screen_labels_an_unedited_section_was_relabelled() -> None:
    before, _ = _seeded()
    perturbed = _edited(before).replace("<h2>The Menu</h2>", "<h2>Drinks List</h2>")
    slices = build_edit_slices(_capture(before, perturbed))
    assert _verdicts(slices)["ScreenLabelOracle"] == "FAIL"
    assert _codes(slices)["ScreenLabelOracle"] == fc.SCREEN_LABEL_UNSTABLE


def test_control_editing_the_declared_section_does_NOT_trip_the_label_oracle() -> None:
    """The complement of the control above: the oracle must not fire on the section the edit
    was scoped to, or it would red every honest run and the PASS above would be luck."""
    before, _ = _seeded()
    retitled_target = _edited(before).replace("<h2>Opening Hours</h2>", "<h2>When We Are Open</h2>")
    assert _verdicts(build_edit_slices(_capture(before, retitled_target)))["ScreenLabelOracle"] == (
        "PASS"
    )


def test_malformed_evidence_still_fails_closed() -> None:
    """Preserved, not weakened: a present-but-malformed slice is INVALID_RUN, never a pass."""
    codes = _codes({"targeted_edit": {"edited_files": "index.html", "expected_files": []}})
    assert codes["TargetedEditOracle"] == fc.EDIT_ORACLE_EVIDENCE_MALFORMED
