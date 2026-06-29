"""Targeted-edit oracles (P8D).

Zero-opinion checks that a targeted edit stayed targeted: it touched only the files it
declared, and a SMALL edit did not rewrite the world. Pure functions over the
``product_evidence`` dossier (the live producer is the P1B-LIVE / soak runner); each reads
its own slice and SKIPs (never silent-passes) when the slice is absent. A present-but-
malformed slice fails closed as EDIT_ORACLE_EVIDENCE_MALFORMED (→ INVALID_RUN), never a pass.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ._edit_evidence import ABSENT, MALFORMED, is_int, is_num, is_str_list, malformed, slice_of
from .schema import OracleResult, failing, passing, skipping


class TargetedEditOracle:
    """The edited files must be a SUBSET of the expected files — a targeted edit never
    silently touches a file it didn't declare."""

    _NAME = "TargetedEditOracle"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        ev = slice_of(product_evidence, "targeted_edit")
        if ev is ABSENT:
            return [skipping(self._NAME, reason="no targeted_edit evidence")]
        if ev is MALFORMED:
            return [malformed(self._NAME, "targeted_edit must be a dict")]
        edited, expected = ev.get("edited_files"), ev.get("expected_files")
        if not is_str_list(edited) or not is_str_list(expected):
            return [malformed(self._NAME, "edited_files/expected_files must be lists of strings")]
        unexpected = sorted(set(edited) - set(expected))
        facts = {"edited_files": sorted(edited), "expected_files": sorted(expected), "unexpected": unexpected}
        if unexpected:
            return [failing(self._NAME, fc.TARGETED_EDIT_TOUCHED_UNEXPECTED_FILES,
                            first_broken_link="edit -> files", facts=facts)]
        return [passing(self._NAME, facts=facts)]


class RewriteAvoidanceOracle:
    """A SMALL-scoped edit must not churn past the scenario's max_churn_ratio (changed lines
    / total lines). Only adjudicated when edit_scope == 'small'; the bound is evidence-
    supplied (not a hidden oracle opinion)."""

    _NAME = "RewriteAvoidanceOracle"

    def check(self, *, product_evidence: dict[str, Any] | None = None) -> list[OracleResult]:
        ev = slice_of(product_evidence, "rewrite_avoidance")
        if ev is ABSENT:
            return [skipping(self._NAME, reason="no rewrite_avoidance evidence")]
        if ev is MALFORMED:
            return [malformed(self._NAME, "rewrite_avoidance must be a dict")]
        scope = ev.get("edit_scope")
        if not isinstance(scope, str):
            return [malformed(self._NAME, "edit_scope must be a string")]
        if scope != "small":
            return [skipping(self._NAME, reason=f"edit_scope={scope!r} not adjudicated (small-only)")]
        changed, total, bound = ev.get("changed_lines"), ev.get("total_lines"), ev.get("max_churn_ratio")
        if not is_int(changed) or not is_int(total) or not is_num(bound):
            return [malformed(self._NAME, "changed_lines/total_lines int (not bool) + max_churn_ratio number required")]
        if total <= 0:
            return [malformed(self._NAME, "total_lines must be > 0", {"total_lines": total})]
        ratio = changed / total
        facts = {"edit_scope": scope, "changed_lines": changed, "total_lines": total,
                 "churn_ratio": ratio, "max_churn_ratio": float(bound)}
        if ratio > bound:
            return [failing(self._NAME, fc.SMALL_EDIT_FULL_REWRITE,
                            first_broken_link="small_edit -> churn", facts=facts)]
        return [passing(self._NAME, facts=facts)]
