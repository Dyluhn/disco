"""CLI argument parsing + the final release-verdict conjunction — extracted from
:mod:`verify_export_track1_closeout`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod

from ._reports import LaneResult


def _build_arg_parser(description: str | None) -> argparse.ArgumentParser:
    """``description`` is the CALLER's ``__doc__`` (the parent entry-point module's),
    passed in explicitly rather than read via a bare ``__doc__`` here — this function
    now lives in a different module than ``main``, and a bare ``__doc__`` would
    resolve to THIS module's docstring, silently changing the ``--help`` text."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--all", action="store_true", help="run every wired lane")
    parser.add_argument(
        "--author",
        action="store_true",
        help="acceptance-authoring mode: allow a dirty tree; NEVER emits passed:true",
    )
    parser.add_argument(
        "--evidence-dir",
        required=True,
        type=Path,
        help="evidence output directory (MUST be OUTSIDE the checkout)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=manifest_mod.repo_root(),
        help="the checkout root (defaults to this script's repository)",
    )
    return parser


def _final_verdict(
    *,
    frozen_ok: bool,
    all_lanes_green: bool,
    clean: bool,
    author: bool,
    hygiene_ok: bool,
    command_inventory_ok: bool = True,
    live_evidence_ok: bool = True,
) -> bool:
    """The single release-verdict conjunction: ``passed`` is true iff the frozen manifest
    verified, EVERY lane is green, the checkout is clean, this is NOT an ``--author`` run,
    the evidence-hygiene scan found no planted credential, the observed command inventory
    exactly equals the frozen required inventory (G19 / plan §9 criterion 3), AND the
    live lane's genuine per-fixture evidence validated (C9-02). Any one false forces
    ``passed`` false; ``--author`` can never pass."""
    return bool(
        frozen_ok
        and all_lanes_green
        and clean
        and not author
        and hygiene_ok
        and command_inventory_ok
        and live_evidence_ok
    )


def _not_passed_reasons(
    *,
    author: bool,
    clean: bool,
    frozen_ok: bool,
    lanes: list[LaneResult],
    hygiene_ok: bool,
    command_inventory_ok: bool = True,
    command_inventory_detail: dict[str, object] | None = None,
    live_evidence_reasons: list[str] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if author:
        reasons.append("author_mode forces passed:false")
    if not clean:
        reasons.append("checkout is dirty")
    if not frozen_ok:
        reasons.append("frozen manifest not verified")
    for lane in lanes:
        if lane.green is True:
            continue
        # A governed pytest lane (nonlive / closeout / live) records SPECIFIC per-test
        # rejection reasons (a named failure, skip, xfail, xpass, error, collection-level
        # skip/error, or a within-selection disappearance); surface each verbatim. Lanes
        # without granular reasons (frontend / g11 / scanner) get the generic message.
        if lane.rejection_reasons:
            reasons.extend(lane.rejection_reasons)
        else:
            reasons.append(f"{lane.name} lane not green")
    if not hygiene_ok:
        reasons.append(
            "evidence hygiene: a registered planted-credential sentinel appears in a "
            "written evidence artifact (see evidence_hygiene.violations)"
        )
    if not command_inventory_ok:
        detail = command_inventory_detail or {}
        reasons.append(
            "command inventory does not equal the frozen required inventory "
            f"(missing={detail.get('missing')} extra={detail.get('extra')})"
        )
    if live_evidence_reasons:
        reasons.extend(live_evidence_reasons)
    return reasons
