"""G13 [critical governance] red — the closeout verifier must not emit a green/passing
result on the strength of evidence it never actually produced.

REMEDIATION gap G13 (Export Track-1 Closeout). Two truthfulness holes in
``scripts/verify_export_track1_closeout.py`` on candidate 581d1fbe let the verifier
report success for work that never ran:

  (a) ``_write_docker_host_artifacts`` writes the Docker-host evidence files with
      ``status = "produced_by_live_lane_on_docker_host"`` REGARDLESS of whether a Docker
      engine ran, so an absent-Docker placeholder is labelled as live-produced (only the
      separate ``available`` flag betrays it — the ``status`` string itself lies).
  (b) ``_run_frontend_lane`` records the Playwright browser e2e as
      ``status = "not_executed_offline"`` and then computes ``lane.green`` from
      vitest + typecheck + build ONLY — the browser lane never gates. So the frontend
      lane is reported green while its browser proof never executed, and ``main()`` folds
      that green into ``all_lanes_green`` on the path to ``passed: true``.

This test IMPORTS the real verifier script module and calls its real functions on a real
temporary filesystem, then asserts the CORRECT, truthful behavior. Importing the verifier
script and calling its functions is NOT a mock — it is the code under test — and is
allowed by the anti-bypass contract; no ``unittest.mock`` / ``MagicMock`` / ``patch`` /
``monkeypatch`` is used, and the filesystem work is real (``tmp_path``, symlinks, copy).

The G13(b) assertion drives the REAL ``_run_frontend_lane`` against a symlink MIRROR of
the repo's frontend whose closeout vitest directory is shadowed to contain a CURATED,
always-green subset of the closeout vitest files (the pre-existing c3/c6 tests), and the
lane is driven with an inventory equal to exactly that subset. That isolation is
deliberate on two axes: the live frontend closeout directory is mutated by other
concurrent remediation reds (e.g. a sibling ``g11-*.test.tsx``), AND freezing R0
regenerates the manifest so ``frontend_closeout_inventory`` GAINS that red test — either
would make the real lane non-green for a reason UNRELATED to G13 and mask this gap.
Curating the mirror off the always-green subset (never the full inventory) makes
vitest + typecheck + build pass deterministically in every manifest state, so the ONLY
reason the lane's ``green`` could differ from ``False`` is the un-gated browser lane —
exactly the defect under test. Explicit preconditions assert those three sub-lanes are
green, so the test can never silently pass on a broken toolchain.

Both assertions are RED on 581d1fbe. The verifier LOGIC fix is R6 — this file only LANDS
the red; it changes no production, verifier, manifest, or work-order file.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

# scripts/ is not an installed package. Put it on sys.path exactly as the verifier itself
# resolves its sibling ``gen_closeout_acceptance_manifest`` module (a bare import that
# relies on scripts/ being importable). ``importlib.import_module`` — a call, not a
# top-level ``import`` after code — keeps this lint-clean without a suppression directive.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

verify = importlib.import_module("verify_export_track1_closeout")
manifest_mod = importlib.import_module("gen_closeout_acceptance_manifest")

pytestmark = pytest.mark.export_track1_closeout

# The exact status string the verifier stamps onto live-produced Docker-host evidence.
_LIVE_DOCKER_CLAIM = "produced_by_live_lane_on_docker_host"
# The exact status the frontend lane records for the browser e2e it never ran offline.
_BROWSER_NOT_RUN = "not_executed_offline"

# A curated, pre-existing ALWAYS-GREEN subset of the closeout vitest files. The G13(b)
# mirror is shadowed to exactly these so vitest is deterministically green REGARDLESS of
# manifest state. This is deliberate: freezing R0 regenerates the acceptance manifest,
# which ADDS the new RED ``g11-*.test.tsx`` to ``frontend_closeout_inventory`` — sourcing
# the mirror from the full inventory would then pull a red vitest into the mirror and make
# this test fail at its vitest precondition instead of its teeth. These c3/c6 files predate
# the remediation and pass on their own, so the mirror stays green in every manifest state.
_GREEN_VITEST_FILES = frozenset(
    {
        "c3-candidate-copy.test.tsx",
        "c3-mode-independent.test.tsx",
        "c3-needs-review-blockers.test.tsx",
        "c3-projects-badge-honest.test.tsx",
        "c3-verified-wording.test.tsx",
        "c6-collision-blockers.test.tsx",
    }
)


def test_docker_evidence_absent_engine_must_not_claim_live_production(tmp_path: Path) -> None:
    """G13(a): with NO Docker engine present (``docker_available=False``) the Docker-host
    evidence writer must NOT stamp its artifacts ``produced_by_live_lane_on_docker_host``
    — that is a live-production claim for a lane that never ran. Absent-Docker evidence
    must record a not-run / absent / failed status instead.

    RED on 581d1fbe: ``_write_docker_host_artifacts`` hard-codes
    ``status = "produced_by_live_lane_on_docker_host"`` regardless of ``docker_available``.
    """
    returned = verify._write_docker_host_artifacts(tmp_path, docker_available=False)

    # The real function returns {artifact_name: status} AND writes the files to disk.
    returned_map = dict(returned)
    assert returned_map, "writer produced no Docker-host artifacts to inspect"

    false_live_claims = {
        name: status for name, status in returned_map.items() if status == _LIVE_DOCKER_CLAIM
    }
    assert not false_live_claims, (
        "G13(a): the Docker engine is ABSENT (docker_available=False) yet these evidence "
        f"artifacts are stamped with the live-production status {_LIVE_DOCKER_CLAIM!r}: "
        f"{sorted(false_live_claims)}. Absent-Docker evidence must record a "
        "not-run / absent / failed status, never a live-produced claim."
    )

    # The same claim is persisted to disk — read one JSON artifact back and assert its
    # on-disk status is likewise not a live-production claim.
    on_disk = json.loads((tmp_path / "bundle-digests.json").read_text(encoding="utf-8"))
    assert on_disk.get("status") != _LIVE_DOCKER_CLAIM, (
        "G13(a): on-disk Docker-host artifact bundle-digests.json records "
        f"status={on_disk.get('status')!r} while available={on_disk.get('available')!r} — "
        "an absent Docker engine must not be labelled live-produced."
    )


def _mirror_frontend(
    real_frontend: Path, dst_frontend: Path, vitest_rel_parts: tuple[str, ...], keep: set[str]
) -> None:
    """Build a symlink MIRROR of ``real_frontend`` at ``dst_frontend`` that is byte-for-byte
    the real frontend EXCEPT the closeout vitest directory (``vitest_rel_parts``), which is
    shadowed to hold ONLY the ``keep`` files.

    The mirror lets the real ``_run_frontend_lane`` run vitest/typecheck/build against the
    genuine frontend while making vitest discover exactly the ``keep`` closeout test set —
    immune to sibling reds dropped into the live directory. ``node_modules`` is symlinked
    so ``npx`` resolves offline; ``index.html`` is COPIED (a symlinked entry html makes
    vite emit an out-of-root asset path and fail); ``dist`` is skipped so the build writes
    into the temp tree and never touches the real repo's build output.
    """
    copy_files = {"index.html"}
    skip_names = {"dist"}
    dst_frontend.mkdir(parents=True, exist_ok=True)
    cur_real = real_frontend
    cur_dst = dst_frontend
    for depth, segment in enumerate(vitest_rel_parts):
        for child in cur_real.iterdir():
            if child.name == segment:
                continue
            if depth == 0 and child.name in skip_names:
                continue
            if depth == 0 and child.name in copy_files and child.is_file():
                shutil.copy2(child, cur_dst / child.name)
            else:
                (cur_dst / child.name).symlink_to(child)
        next_dst = cur_dst / segment
        next_dst.mkdir()
        cur_real = cur_real / segment
        cur_dst = next_dst
    # ``cur_dst`` is now the shadowed closeout vitest dir — symlink only the kept files.
    for child in cur_real.iterdir():
        if child.name in keep:
            (cur_dst / child.name).symlink_to(child)


def test_frontend_lane_unexecuted_browser_must_block_green(tmp_path: Path) -> None:
    """G13(b): the frontend lane records its Playwright browser e2e as un-executed
    (``not_executed_offline``) but computes ``green`` from vitest + typecheck + build
    ONLY. A browser lane that never executed must NOT be a green / gating input to
    ``passed: true``, so an un-run browser lane must block the frontend lane's green.

    Drives the REAL ``_run_frontend_lane`` against a symlink mirror of the frontend whose
    closeout vitest dir holds a curated always-green subset (see ``_mirror_frontend`` and
    ``_GREEN_VITEST_FILES``), so vitest + typecheck + build all pass deterministically in
    every manifest state and the browser gate is the only variable. RED on 581d1fbe: the
    lane returns ``green is True`` while recording the browser proof as un-run.
    """
    if shutil.which("npx") is None or shutil.which("npm") is None:
        pytest.fail("G13(b) requires the node/npm/npx toolchain to exercise the frontend lane")

    # Build the mirror's kept set from the curated always-green c3/c6 files that ACTUALLY
    # exist on disk — never the full frozen inventory (which, post-regen, carries red
    # tests). The lane is then driven with an inventory equal to exactly that shadowed set,
    # so its own ``file_set_ok`` is green off the curated subset. green thus hinges only on
    # the un-gated browser lane, independent of manifest state.
    real_vitest_dir = _REPO_ROOT / str(manifest_mod.FRONTEND_VITEST_DIR)
    green_present = sorted(
        p.name for p in real_vitest_dir.iterdir() if p.name in _GREEN_VITEST_FILES
    )
    assert len(green_present) >= 2, (
        "expected the pre-existing always-green closeout vitest files (c3/c6) on disk to "
        f"build a deterministically-green mirror; found {green_present}"
    )
    keep = set(green_present)
    frontend_inventory = {name: {} for name in green_present}

    vitest_rel = Path(str(manifest_mod.FRONTEND_VITEST_DIR)).relative_to("frontend").parts
    mirror_repo = tmp_path / "repo"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _mirror_frontend(_REPO_ROOT / "frontend", mirror_repo / "frontend", vitest_rel, keep)

    lane = verify._run_frontend_lane(mirror_repo, evidence_dir, frontend_inventory)
    detail = lane.detail

    # Preconditions: the three non-browser sub-lanes are green in the isolated mirror, so
    # a non-green lane here can ONLY be the un-gated browser lane. If the toolchain is
    # broken these fail loudly — the test never silently passes.
    assert detail.get("npx_available") is True, f"precondition: npx unavailable; detail={detail!r}"
    vitest_counts = detail["vitest"]["counts"]
    assert vitest_counts["failed"] == 0 and vitest_counts["total"] > 0, (
        f"precondition: the curated always-green vitest subset must pass; counts={vitest_counts!r}"
    )
    # file_set_ok is asserted against the CURATED green subset we drove the lane with — not
    # the full frozen inventory — so it stays deterministically green across manifest regens.
    assert detail["vitest"]["file_set_ok"] is True, (
        f"precondition: isolated vitest file-set must equal the curated green subset; "
        f"extra={detail['vitest'].get('extra_files')!r} missing={detail['vitest'].get('missing_files')!r}"
    )
    assert detail["typecheck"]["exit_code"] == 0, "precondition: typecheck:build must pass"
    assert detail["build"]["exit_code"] == 0, "precondition: vite build must pass"

    browser_status = detail["playwright_e2e"]["status"]
    # Current recorded reality on 581d1fbe: the browser e2e proof did not execute.
    assert browser_status == _BROWSER_NOT_RUN, (
        f"precondition: expected the browser e2e recorded un-executed ({_BROWSER_NOT_RUN!r}); "
        f"got {browser_status!r}"
    )

    # CORRECT behavior: a browser lane that never executed blocks the frontend lane green.
    assert lane.green is not True, (
        "G13(b): the frontend lane is GREEN while its Playwright browser proof was never "
        f"executed (playwright_e2e.status={browser_status!r}). green is computed from "
        "vitest+typecheck+build only, so an un-run browser lane cannot be a green/gating "
        "input to passed:true."
    )
