"""Promotion evidence is selected by identity, never by recency.

The previous rule took the newest `batch-summary.json` under the output root by
mtime. Diagnostic and promotion campaigns write the SAME filename, so a root
containing both resolved to whichever happened to run last — intent inferred from
a clock.

Observed 2026-07-27: 26 promotion-named summaries sit under the Epic-4 diagnostic
tree (correctly preserved — this campaign does not delete evidence to tidy an
audit), and a reader rooted at their shared ancestor selected one of those. The
counted lanes would have displaced them later purely by ordering.

Both directions are pinned here: a newer diagnostic cannot displace an older
valid promotion summary, and an older diagnostic cannot be resurrected either.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from harness.reliability.run import (
    INFRA,
    INVALID,
    PASS,
    _build_soak_result,
    _select_build_soak_summary,
)


def _summary(path: Path, *, batch_id: str, passes: int, age_s: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": batch_id,
                "runs": [{"status": "PASS"} for _ in range(passes)],
            }
        ),
        encoding="utf-8",
    )
    if age_s:
        old = time.time() - age_s
        os.utime(path, (old, old))
    return path


def test_a_single_summary_is_selected(tmp_path):
    _summary(tmp_path / "promotion" / "batch-summary.json", batch_id="b1", passes=3)
    chosen, reason = _select_build_soak_summary(tmp_path)
    assert chosen is not None and reason == ""
    status, units, _ = _build_soak_result(tmp_path, exit_code=0, units=3)
    assert (status, units) == (PASS, 3)


def test_two_summaries_are_REFUSED_rather_than_guessed(tmp_path):
    # The exact footgun: a diagnostic tree and a promotion tree under one root.
    _summary(tmp_path / "promotion" / "batch-summary.json", batch_id="promo", passes=3, age_s=600)
    _summary(tmp_path / "diagnostic" / "batch-summary.json", batch_id="diag", passes=1)
    chosen, reason = _select_build_soak_summary(tmp_path)
    assert chosen is None
    assert "ambiguous" in reason and "recency is not intent" in reason
    status, units, _ = _build_soak_result(tmp_path, exit_code=0, units=3)
    assert (status, units) == (INFRA, 0)


def test_a_newer_diagnostic_cannot_displace_an_older_valid_promotion(tmp_path):
    # The promotion summary is OLDER and has enough passes; the diagnostic is
    # newer and would fail the unit bar. Under mtime selection the diagnostic won.
    _summary(tmp_path / "promotion" / "batch-summary.json", batch_id="promo", passes=5, age_s=3600)
    _summary(tmp_path / "diagnostic" / "batch-summary.json", batch_id="diag", passes=1)
    status, units, _ = _build_soak_result(tmp_path, exit_code=0, units=5, expected_batch_id="promo")
    assert (status, units) == (PASS, 5)


def test_an_older_diagnostic_cannot_be_resurrected_either(tmp_path):
    _summary(tmp_path / "diagnostic" / "batch-summary.json", batch_id="diag", passes=9, age_s=7200)
    _summary(tmp_path / "promotion" / "batch-summary.json", batch_id="promo", passes=2)
    status, units, reason = _build_soak_result(
        tmp_path, exit_code=0, units=5, expected_batch_id="promo"
    )
    # Bound to the promotion batch, its 2 passes are honestly short of 5 — the
    # diagnostic's 9 must not be borrowed to cover the gap.
    assert status == INVALID
    assert "2/5" in reason


def test_an_unknown_batch_id_is_refused_not_defaulted(tmp_path):
    _summary(tmp_path / "promotion" / "batch-summary.json", batch_id="promo", passes=3)
    chosen, reason = _select_build_soak_summary(tmp_path, expected_batch_id="absent")
    assert chosen is None
    assert "not provably this invocation" in reason


def test_a_summary_without_identity_is_refused(tmp_path):
    path = tmp_path / "promotion" / "batch-summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runs": [{"status": "PASS"}]}), encoding="utf-8")
    status, _, reason = _build_soak_result(tmp_path, exit_code=0, units=1)
    assert status == INVALID
    assert "declares no batch_id" in reason


def test_no_summary_at_all_is_infra(tmp_path):
    status, units, reason = _build_soak_result(tmp_path, exit_code=0, units=1)
    assert (status, units) == (INFRA, 0)
    assert "no batch-summary.json" in reason


def test_a_symlink_escaping_the_bound_root_is_ignored(tmp_path):
    outside = tmp_path / "outside"
    _summary(outside / "batch-summary.json", batch_id="foreign", passes=99)
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        return
    chosen, reason = _select_build_soak_summary(root)
    assert chosen is None
    assert "no batch-summary.json" in reason


def test_selection_is_deterministic_and_never_consults_mtime(tmp_path):
    # Same tree, opposite mtime orderings -> same refusal, no flip-flop.
    _summary(tmp_path / "a" / "batch-summary.json", batch_id="a", passes=1, age_s=10_000)
    _summary(tmp_path / "b" / "batch-summary.json", batch_id="b", passes=1)
    first = _select_build_soak_summary(tmp_path)
    _summary(tmp_path / "a" / "batch-summary.json", batch_id="a", passes=1)
    _summary(tmp_path / "b" / "batch-summary.json", batch_id="b", passes=1, age_s=10_000)
    second = _select_build_soak_summary(tmp_path)
    assert first[0] is None and second[0] is None
    assert first[1] == second[1]
