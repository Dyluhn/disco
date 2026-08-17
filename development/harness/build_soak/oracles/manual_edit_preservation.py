"""Manual-edit-preservation oracles (P8D).

Zero-opinion checks that an edit preserved work it must not destroy: a user's direct/manual
override survives a later agent edit, comment anchors survive text edits and section
reorders (set membership, not position), and an UNEDITED section's screen label stays
stable. Pure functions over the ``product_evidence`` dossier; SKIP on an absent slice,
fail closed (EDIT_ORACLE_EVIDENCE_MALFORMED → INVALID_RUN) on a malformed one.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ._edit_evidence import ABSENT, MALFORMED, is_str_dict, is_str_list, malformed, slice_of
from .schema import OracleResult, failing, passing, skipping


class ManualEditPreservationOracle:
    """Every manual/direct override snippet must still be present (verbatim) in the post-edit
    file — an agent edit must not clobber the user's own change. (The producer supplies the
    exact verbatim snippet that must survive.)"""

    _NAME = "ManualEditPreservationOracle"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        ev = slice_of(product_evidence, "manual_edit")
        if ev is ABSENT:
            return [skipping(self._NAME, reason="no manual_edit evidence")]
        if ev is MALFORMED:
            return [malformed(self._NAME, "manual_edit must be a dict")]
        overrides, final = ev.get("overrides"), ev.get("final_files")
        if not is_str_dict(overrides) or not is_str_dict(final):
            return [malformed(self._NAME, "overrides/final_files must be dicts of str->str")]
        clobbered = sorted(p for p, snippet in overrides.items() if snippet not in final.get(p, ""))
        facts = {"override_paths": sorted(overrides), "clobbered": clobbered}
        if clobbered:
            return [
                failing(
                    self._NAME,
                    fc.MANUAL_EDIT_CLOBBERED,
                    first_broken_link="agent_edit -> manual_override",
                    facts=facts,
                )
            ]
        return [passing(self._NAME, facts=facts)]


class CommentAnchorOracle:
    """Comment anchors present before the edit must all still be present after it — they
    survive a text edit and MOVE with a section reorder (set membership, not position)."""

    _NAME = "CommentAnchorOracle"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        ev = slice_of(product_evidence, "comment_anchors")
        if ev is ABSENT:
            return [skipping(self._NAME, reason="no comment_anchors evidence")]
        if ev is MALFORMED:
            return [malformed(self._NAME, "comment_anchors must be a dict")]
        before, after = ev.get("before"), ev.get("after")
        if not is_str_list(before) or not is_str_list(after):
            return [malformed(self._NAME, "before/after must be lists of strings")]
        lost = sorted(set(before) - set(after))
        facts = {"before": sorted(before), "after": sorted(after), "lost": lost}
        if lost:
            return [
                failing(
                    self._NAME,
                    fc.COMMENT_ANCHOR_LOST,
                    first_broken_link="edit -> comment_anchor",
                    facts=facts,
                )
            ]
        return [passing(self._NAME, facts=facts)]


class ScreenLabelOracle:
    """A section NOT in the edit's targets must keep the same screen label — an edit to one
    section must not perturb another section's stable identity."""

    _NAME = "ScreenLabelOracle"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        ev = slice_of(product_evidence, "screen_labels")
        if ev is ABSENT:
            return [skipping(self._NAME, reason="no screen_labels evidence")]
        if ev is MALFORMED:
            return [malformed(self._NAME, "screen_labels must be a dict")]
        edited, before, after = ev.get("edited_sections"), ev.get("before"), ev.get("after")
        if not is_str_list(edited) or not is_str_dict(before) or not is_str_dict(after):
            return [
                malformed(
                    self._NAME, "edited_sections list[str] + before/after dict[str,str] required"
                )
            ]
        edited_set = set(edited)
        unstable = sorted(
            sec
            for sec, label in before.items()
            if sec not in edited_set and after.get(sec) != label  # unedited section's label changed
        )
        facts = {"edited_sections": sorted(edited_set), "unstable": unstable}
        if unstable:
            return [
                failing(
                    self._NAME,
                    fc.SCREEN_LABEL_UNSTABLE,
                    first_broken_link="edit -> unedited_section_label",
                    facts=facts,
                )
            ]
        return [passing(self._NAME, facts=facts)]
