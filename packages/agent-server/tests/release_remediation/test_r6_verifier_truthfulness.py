"""R6 (GAPs G13, G18, G19) — mutation/regression tests for the closeout verifier's
truthfulness gates (plan §9, strict acceptance criteria 1-6).

These are the committed COUNTERPART to the two frozen G13 reds
(``test_g13_verifier_truthfulness.py``, which land the reds; R6 flips them green through
production alone). They live OUTSIDE the frozen dirs and run in the normal unit suite,
EXCEPT the tests that certify the real closeout checkout state (the campaign-diff scan
over the real repo history, the committed frozen-manifest binding, and the real npm
toolchain positive) — those are ``integration``-marked because they require a repository
state/toolchain the fast required lane does not provide. They import the REAL verifier
module exactly as the frozen test does — a call to ``importlib.import_module``, no
``unittest.mock`` — driving its real functions on a real temporary filesystem. Nothing
here edits a frozen file.

Coverage against plan §9 strict acceptance criteria:

* Criterion 1 — one failing static gate (a new campaign-diff suppression), one failing
  Firefox result (unexpected>0 / skipped>0), and one non-green lane each drive the real
  functions to non-green and fold the final verdict to false.
* Criterion 2 — a deleted / hash-changed (placeholder-replaced) frozen artifact makes the
  frozen-manifest check fail, folding the verdict false; a dirty/altered checkout can never
  pass.
* Criterion 3 — an observed command inventory that is not EXACTLY the frozen required
  inventory (a missing OR an extra/substitute command) is rejected.
* Criterion 4 — a new ``# noqa`` / type-ignore / ``@ts-ignore`` / skip / xfail /
  ``continue-on-error`` in a diff hunk fails the anti-bypass scan, UNLESS its
  (file, line) pair is in the owner-approved recorded baseline (both directions proven).
* Criterion 5 — the browser gate: ``_parse_playwright_e2e`` is green ONLY for an executed,
  all-passed, full-spec-set report; absent / failed / skipped / partial-spec-set are all
  non-green (the ``integration``-marked positive at the bottom proves ``_run_frontend_lane``
  green flips TRUE on an all-passed report — the positive counterpart to frozen G13(b)).
* Criterion 6 — a planted registered credential sentinel in the NEW ``frontend-e2e.json``
  evidence artifact forces the hygiene scan closed, folding the verdict false.
* G13(a) positive — the live-production Docker status is stamped ONLY when the engine is
  present AND the live lifecycle succeeded.
"""

from __future__ import annotations

import importlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS_DIR = _REPO_ROOT / "development" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

verify = importlib.import_module("verify_export_track1_closeout")
manifest_mod = importlib.import_module("gen_closeout_acceptance_manifest")

_LIVE_DOCKER_CLAIM = "produced_by_live_lane_on_docker_host"
_MARKER = next(iter(verify._EVIDENCE_HYGIENE_SENTINELS.values()))

# The four frozen e2e spec basenames the browser gate demands.
_FROZEN_E2E_SPECS = {
    "selfhost-candidate.spec.ts",
    "selfhost-download-binding.spec.ts",
    "selfhost-needs-review.spec.ts",
    "selfhost-not-web.spec.ts",
}


def _pw_report(
    spec_files: set[str], *, expected: int, unexpected: int = 0, flaky: int = 0, skipped: int = 0
) -> dict[str, object]:
    """A minimal Playwright ``--reporter=json`` shape: a suite per spec file plus a stats
    block. Enough for ``_parse_playwright_e2e`` to read the executed spec set + counts."""
    return {
        "suites": [
            {"file": f"e2e/export-track1-closeout/{name}", "specs": [], "suites": []}
            for name in sorted(spec_files)
        ],
        "stats": {
            "expected": expected,
            "unexpected": unexpected,
            "flaky": flaky,
            "skipped": skipped,
        },
    }


def _diff(path: str, added_line: str) -> str:
    """A minimal single-hunk unified diff that ADDS ``added_line`` to ``path``."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,1 +1,2 @@\n"
        f" import os\n"
        f"+{added_line}\n"
    )


def _noprefix_diff(path: str, added_line: str) -> str:
    """A single-hunk diff as emitted by a git configured with ``diff.noprefix=true``: the
    ``diff --git a/… b/…`` header still carries a/ b/, but the file markers are
    ``--- x`` / ``+++ x`` with NO ``b/`` prefix. A parser that anchored solely on
    ``+++ b/`` would leave ``cur_file`` None and silently skip every added line (F4)."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- {path}\n"
        f"+++ {path}\n"
        f"@@ -1,1 +1,2 @@\n"
        f" import os\n"
        f"+{added_line}\n"
    )


# ---- G13(a): truthful Docker-host status (positive + negatives) ----------------


def test_docker_live_claim_only_when_available_and_lifecycle_ok(tmp_path: Path) -> None:
    """G13(a) positive: the live-production status is stamped ONLY for
    (docker_available=True, live_lifecycle_ok=True); an engine present but a failed
    lifecycle, or no engine, must NOT carry the live claim."""
    live_dir = tmp_path / "live"
    failed_dir = tmp_path / "failed"
    absent_dir = tmp_path / "absent"
    for d in (live_dir, failed_dir, absent_dir):
        d.mkdir()

    live = verify._write_docker_host_artifacts(live_dir, True, live_lifecycle_ok=True)
    failed = verify._write_docker_host_artifacts(failed_dir, True, live_lifecycle_ok=False)
    absent = verify._write_docker_host_artifacts(absent_dir, False)

    assert set(live.values()) == {_LIVE_DOCKER_CLAIM}
    assert _LIVE_DOCKER_CLAIM not in set(failed.values())
    assert _LIVE_DOCKER_CLAIM not in set(absent.values())
    # On-disk truth matches the returned map.
    on_disk = json.loads((failed_dir / "bundle-digests.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "live_lane_failed"
    on_disk_absent = json.loads((absent_dir / "bundle-digests.json").read_text(encoding="utf-8"))
    assert on_disk_absent["status"] == "absent_no_docker_engine"


# ---- Criterion 5: the browser e2e gate (pure) ---------------------------------


def test_browser_gate_green_only_on_executed_all_passed_full_spec_set(tmp_path: Path) -> None:
    path = tmp_path / "frontend-e2e.json"
    path.write_text(json.dumps(_pw_report(_FROZEN_E2E_SPECS, expected=4)), encoding="utf-8")
    ok, detail = verify._parse_playwright_e2e(path, set(_FROZEN_E2E_SPECS))
    assert ok is True, detail
    assert detail["status"] == "executed"
    assert detail["spec_set_ok"] is True


def test_browser_gate_absent_report_is_not_green(tmp_path: Path) -> None:
    ok, detail = verify._parse_playwright_e2e(
        tmp_path / "frontend-e2e.json", set(_FROZEN_E2E_SPECS)
    )
    assert ok is False
    assert detail["status"] == "not_executed_offline"


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"expected": 4, "unexpected": 1}, "a failing spec (unexpected>0)"),
        ({"expected": 3, "skipped": 1}, "a skipped spec"),
        ({"expected": 4, "flaky": 1}, "a flaky spec"),
        ({"expected": 0}, "nothing executed"),
    ],
)
def test_browser_gate_any_failure_or_skip_is_not_green(
    tmp_path: Path, kwargs: dict[str, int], why: str
) -> None:
    path = tmp_path / "frontend-e2e.json"
    path.write_text(json.dumps(_pw_report(_FROZEN_E2E_SPECS, **kwargs)), encoding="utf-8")
    ok, _ = verify._parse_playwright_e2e(path, set(_FROZEN_E2E_SPECS))
    assert ok is False, f"browser gate must be non-green for {why}"


def test_browser_gate_partial_or_extra_spec_set_is_not_green(tmp_path: Path) -> None:
    path = tmp_path / "frontend-e2e.json"
    # A single spec executed (dropped 3) — all "passed", but the spec SET is wrong.
    path.write_text(
        json.dumps(_pw_report({"selfhost-candidate.spec.ts"}, expected=1)), encoding="utf-8"
    )
    ok, detail = verify._parse_playwright_e2e(path, set(_FROZEN_E2E_SPECS))
    assert ok is False and detail["spec_set_ok"] is False


def test_browser_gate_failure_record_is_not_green(tmp_path: Path) -> None:
    path = tmp_path / "frontend-e2e.json"
    path.write_text(json.dumps({"status": "browser_run_failed", "exit_code": 1}), encoding="utf-8")
    ok, detail = verify._parse_playwright_e2e(path, set(_FROZEN_E2E_SPECS))
    assert ok is False and detail["status"] == "browser_run_failed"


# ---- Criterion 3: the frozen command inventory ---------------------------------


def test_command_inventory_exact_match_accepted() -> None:
    ok, detail = verify._check_command_inventory(
        set(manifest_mod.REQUIRED_COMMAND_IDS), manifest_mod.REQUIRED_COMMAND_IDS
    )
    assert ok is True and not detail["missing"] and not detail["extra"]


def test_command_inventory_omitted_command_rejected() -> None:
    observed = set(manifest_mod.REQUIRED_COMMAND_IDS) - {"browser_e2e"}
    ok, detail = verify._check_command_inventory(observed, manifest_mod.REQUIRED_COMMAND_IDS)
    assert ok is False and "browser_e2e" in detail["missing"]


def test_command_inventory_extra_substitute_command_rejected() -> None:
    observed = set(manifest_mod.REQUIRED_COMMAND_IDS) | {"browser_e2e_single_spec_file"}
    ok, detail = verify._check_command_inventory(observed, manifest_mod.REQUIRED_COMMAND_IDS)
    assert ok is False and "browser_e2e_single_spec_file" in detail["extra"]


def test_command_inventory_names_the_e2e_directory_never_a_single_spec_file() -> None:
    """G19: the frozen browser command names the e2e DIRECTORY, never a nonexistent single
    ``e2e/export-track1-closeout.spec.ts`` file."""
    browser_cmd = manifest_mod.COMMAND_INVENTORY["browser_e2e"]
    assert "e2e/export-track1-closeout" in browser_cmd
    assert "export-track1-closeout.spec.ts" not in browser_cmd


# ---- Criterion 4: the G18 no-new-suppression campaign-diff scan -----------------


@pytest.mark.parametrize(
    ("path", "added"),
    [
        ("packages/core/src/disco/core/x.py", "value = compute()  # noqa: E501"),
        ("packages/core/src/disco/core/x.py", "value = compute()  # type: ignore[arg-type]"),
        ("packages/core/src/disco/core/x.py", "value = compute()  # pyright: ignore"),
        ("packages/agent-server/tests/export_track1_closeout/t.py", "@pytest.mark.skip"),
        ("packages/agent-server/tests/export_track1_closeout/t.py", "@pytest.mark.xfail"),
        ("frontend/src/api/x.ts", "const y = z; // @ts-ignore"),
        ("frontend/src/test/export-track1-closeout/x.test.tsx", "it.skip('nope', () => {})"),
        (".github/workflows/export-track1-closeout.yml", "    continue-on-error: true"),
    ],
)
def test_new_suppression_in_diff_is_rejected(path: str, added: str) -> None:
    violations = verify._scan_campaign_diff_suppressions(_diff(path, added), set())
    assert violations, f"a new suppression {added!r} in {path} must be flagged"
    assert violations[0]["file"] == path


def test_new_suppression_accepted_when_in_owner_approved_baseline() -> None:
    path = "packages/core/src/disco/core/x.py"
    added = "value = compute()  # noqa: E501"
    baseline = {(path, added.strip())}
    assert verify._scan_campaign_diff_suppressions(_diff(path, added), baseline) == []
    # ... but the SAME hit without the baseline entry is rejected.
    assert verify._scan_campaign_diff_suppressions(_diff(path, added), set())


def test_diff_scan_exempts_gate_scripts_and_meta_tests() -> None:
    """The two gate-definition scripts (they DEFINE the patterns) and the
    release_remediation meta-test dir (it embeds tokens as fixtures) are exempt, so the
    scan never flags its own definition."""
    for exempt in (
        "development/scripts/verify_export_track1_closeout.py",
        "development/scripts/gen_closeout_acceptance_manifest.py",
        "packages/agent-server/tests/release_remediation/test_r6_verifier_truthfulness.py",
    ):
        assert verify._scan_campaign_diff_suppressions(_diff(exempt, "x = 1  # noqa"), set()) == []


def test_prose_comment_mentioning_suppression_words_is_not_flagged() -> None:
    """A comment EXPLAINING there is no continue-on-error / skip path is not a suppression;
    only the real directive syntax (a YAML key, a decorator, a comment directive) counts."""
    diff = _diff(
        ".github/workflows/x.yml", "# There is NO advisory / continue-on-error / skip path here"
    )
    assert verify._scan_campaign_diff_suppressions(diff, set()) == []


# ---- Criterion 4 hardening (R6.1): file-level TS + module-level pytestmark + F4 ------


@pytest.mark.parametrize(
    ("path", "added"),
    [
        # F1: `@ts-nocheck` disables type-checking for the WHOLE file (unlike the
        # deliberately-excluded `@ts-expect-error`), so it must be flagged.
        ("frontend/src/api/x.ts", "// @ts-nocheck"),
        ("frontend/src/api/x.ts", "//@ts-nocheck this whole file is unchecked"),
        # F2: a module-level `pytestmark` skip/skipif/xfail — the per-test decorator
        # patterns miss this whole-module skip.
        (
            "packages/agent-server/tests/export_track1_closeout/t.py",
            "pytestmark = pytest.mark.skip(reason='disable the whole module')",
        ),
        (
            "packages/agent-server/tests/export_track1_closeout/t.py",
            "pytestmark = pytest.mark.skipif(True, reason='env')",
        ),
        (
            "packages/agent-server/tests/export_track1_closeout/t.py",
            "pytestmark = pytest.mark.xfail(reason='known')",
        ),
        (
            "packages/agent-server/tests/export_track1_closeout/t.py",
            "pytestmark = [pytest.mark.integration, pytest.mark.skip]",
        ),
    ],
)
def test_file_level_and_module_level_suppression_in_diff_is_rejected(path: str, added: str) -> None:
    """F1/F2: a newly-added `@ts-nocheck` or module-level `pytestmark` skip/skipif/xfail
    (including the list form) is flagged — and, being un-baselined, rejected."""
    violations = verify._scan_campaign_diff_suppressions(_diff(path, added), set())
    assert violations, f"a new suppression {added!r} in {path} must be flagged"
    assert violations[0]["file"] == path
    # ... but recording the exact (file, stripped-line) pair in the baseline clears it.
    baseline = {(path, added.strip())}
    assert verify._scan_campaign_diff_suppressions(_diff(path, added), baseline) == []


@pytest.mark.parametrize(
    "added",
    [
        "pytestmark = pytest.mark.export_track1_closeout",
        "pytestmark = pytest.mark.integration",
        "pytestmark = [pytest.mark.export_track1_closeout, pytest.mark.integration]",
    ],
)
def test_legitimate_pytestmark_marker_is_not_flagged(added: str) -> None:
    """F2 must not over-match: a `pytestmark` that assigns a real MARKER
    (`export_track1_closeout` / `integration`) is not a suppression and is not flagged."""
    path = "packages/agent-server/tests/export_track1_closeout/t.py"
    assert verify._scan_campaign_diff_suppressions(_diff(path, added), set()) == []


def test_ts_expect_error_is_not_flagged_but_ts_nocheck_is() -> None:
    """The frozen g11 guard contract relies on `@ts-expect-error` being EXCLUDED (it is a
    negative assertion, never a suppression); only `@ts-nocheck` (whole-file disable) is
    flagged."""
    path = "frontend/src/api/x.ts"
    expect_error = verify._scan_campaign_diff_suppressions(
        _diff(path, "// @ts-expect-error next"), set()
    )
    assert expect_error == []
    assert verify._scan_campaign_diff_suppressions(_diff(path, "// @ts-nocheck"), set())


def test_noprefix_diff_still_flags_a_new_suppression() -> None:
    """F4: a diff from a git with `diff.noprefix=true` emits `+++ x` (no `b/`). The scanner
    must still anchor the file via the `diff --git a/x b/x` header, so a newly-added
    `# noqa` is flagged rather than silently skipped (the whole gate passing vacuously)."""
    path = "packages/core/src/disco/core/x.py"
    diff = _noprefix_diff(path, "value = compute()  # noqa: E501")
    # Sanity: the diff genuinely lacks a `+++ b/` marker (so the pre-F4 parser would blank).
    assert "+++ b/" not in diff and "+++ " + path in diff
    violations = verify._scan_campaign_diff_suppressions(diff, set())
    assert violations, "a no-prefix diff must not silently no-op the suppression scan"
    assert violations[0]["file"] == path
    assert violations[0]["rule"] == "new_suppression_noqa"


def test_scanner_lane_git_diff_invocation_forces_ab_prefixes() -> None:
    """F4 (invocation side): the campaign `git diff` is called with explicit
    `--src-prefix=a/ --dst-prefix=b/` so prefixes are emitted regardless of the host's
    `diff.noprefix` config. Guards against a future edit dropping the flags."""
    src = _SCRIPTS_DIR.joinpath("verify_export_track1_closeout.py").read_text(encoding="utf-8")
    assert "--src-prefix=a/" in src and "--dst-prefix=b/" in src


# ---- The ratified acceptance anchor (F6-a) ---------------------------------------
#
# The two certification-only tests below were authored against ``..HEAD`` and the live
# working tree. That made each one true only for as long as the checkout still *was*
# the closeout campaign's accepted tree. Once a later campaign committed on top, both
# began reporting drift that is perfectly real and says nothing whatever about Export
# Track 1: the suppression window grew to span the whole of a different campaign, and
# the frozen manifest was being compared against files that campaign had edited.
#
# So each is pinned to the commit whose evidence it was written about. That restores an
# immutable historical fact in place of a moving one, and it is the reason the pin is a
# strengthening rather than a relaxation.
#
# ``manifest_mod.ACCEPTANCE_TAG`` names v6 and the repository carries no v6 tag, so the
# anchor is recorded by owner-delegate decision of 2026-08-05 (ESCALATION-2026-08-05.md
# option 2, and DECISIONS-LOG "F6-a DECIDED"): ``18ffa403`` is the commit that re-froze
# the manifest for v6 — its own message says so — and the last commit to change any
# frozen file hash.
#
# Neither assertion's predicate is altered: still zero un-baselined suppressions, still
# a manifest that verifies clean. Only the window/tree each is evaluated over is pinned.
# The manifest and the drift detector itself are untouched.
_ACCEPTANCE_ANCHOR = "18ffa4032dc58f2d76db9f372c9d4bebcf818504"


def _require_anchor_commit() -> None:
    """Prove the ratified anchor is present before anything is evaluated against it.

    ``verify._run`` is deliberately ``check=False`` and never raises, so on a checkout
    without this commit ``git diff`` would emit *nothing* and the suppression scan would
    pass over an empty diff — a vacuous green that reads exactly like a real one. This
    guard is what stops the pin from being weaker than the ``..HEAD`` form it replaces.
    """
    probe = verify._run(
        ["git", "rev-parse", "--verify", f"{_ACCEPTANCE_ANCHOR}^{{commit}}"], cwd=_REPO_ROOT
    )
    assert probe.returncode == 0, (
        f"the ratified acceptance anchor {_ACCEPTANCE_ANCHOR[:8]} is absent from this "
        f"checkout, so nothing can be evaluated against it: {probe.stderr.strip()}"
    )


def _materialize_anchor_tree(dest: Path) -> Path:
    """Extract the ratified tree at ``_ACCEPTANCE_ANCHOR`` into ``dest``.

    A throwaway extraction rather than ``git worktree add``: this repository already
    carries hundreds of registered worktrees, and a test must not add one.
    """
    _require_anchor_commit()
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", _ACCEPTANCE_ANCHOR], cwd=_REPO_ROOT, capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return dest


# Certification-only: this scans the REAL git history window
# BASELINE_SHA.._ACCEPTANCE_ANCHOR, so it needs a checkout that carries the closeout
# campaign's history (the C9 certification lane). ``integration``-marked per the repo
# convention: excluded from the default `-m "not integration"` unit selection, runnable
# explicitly / in the advisory lane. The assertion itself is unchanged.
@pytest.mark.integration
def test_real_campaign_diff_has_no_unbaselined_suppression() -> None:
    """End-to-end over the REAL campaign diff (BASELINE_SHA.._ACCEPTANCE_ANCHOR) with the
    REAL committed baseline: zero un-approved new suppressions. Proves the baseline is
    complete and the scanner lane was green on the campaign as it was accepted."""
    baseline, note = verify._load_suppression_baseline(_REPO_ROOT)
    assert note == "loaded" and baseline, "the owner-approved suppression baseline must load"
    _require_anchor_commit()
    completed = verify._run(
        ["git", "diff", f"{manifest_mod.BASELINE_SHA}..{_ACCEPTANCE_ANCHOR}"], cwd=_REPO_ROOT
    )
    # Anti-vacuity: an empty diff would scan clean and prove nothing at all.
    assert completed.returncode == 0, f"the campaign diff could not be taken: {completed.stderr}"
    diff_text = completed.stdout
    assert diff_text.strip(), "the campaign diff is empty — the window scanned nothing"
    violations = verify._scan_campaign_diff_suppressions(diff_text, baseline)
    assert violations == [], (
        f"unexpected un-baselined suppressions in the campaign diff: {violations}"
    )


def test_baseline_records_the_ratified_c2_noqa() -> None:
    baseline, _ = verify._load_suppression_baseline(_REPO_ROOT)
    files = {f for f, _line in baseline}
    assert any("test_c2_bound_download.py" in f for f in files)


# ---- Criterion 6: evidence hygiene extends to frontend-e2e.json -----------------


def test_planted_sentinel_in_frontend_e2e_json_forces_passed_false(tmp_path: Path) -> None:
    (tmp_path / "frontend-e2e.json").write_text(
        json.dumps({"stats": {"expected": 4}, "leak": f"...{_MARKER}..."}), encoding="utf-8"
    )
    ok, violations = verify._scan_evidence_hygiene(tmp_path)
    assert ok is False
    assert any(v["file"] == "frontend-e2e.json" for v in violations)
    # The hygiene failure folds the final verdict false (all else green).
    assert (
        verify._final_verdict(
            frozen_ok=True,
            all_lanes_green=True,
            clean=True,
            author=False,
            hygiene_ok=ok,
            command_inventory_ok=True,
        )
        is False
    )


def test_frontend_e2e_json_and_g11_txt_are_in_the_hygiene_scan_set() -> None:
    assert "frontend-e2e.json" in verify._HYGIENE_SCAN_FILES
    assert "g11-typecheck.txt" in verify._HYGIENE_SCAN_FILES


# ---- Criterion 1 & 2: the verdict folds every gate --------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"all_lanes_green": False},  # crit 1: any lane (static / Firefox / live) non-green
        {"hygiene_ok": False},  # crit 6
        {"command_inventory_ok": False},  # crit 3
        {"frozen_ok": False},  # crit 2: deleted/replaced frozen artifact
        {"clean": False},  # crit 2: dirty/altered checkout
        {"author": True},  # author runs can never pass
    ],
)
def test_final_verdict_is_false_when_any_gate_fails(override: dict[str, object]) -> None:
    base = {
        "frozen_ok": True,
        "all_lanes_green": True,
        "clean": True,
        "author": False,
        "hygiene_ok": True,
        "command_inventory_ok": True,
    }
    assert verify._final_verdict(**base) is True  # all green ⇒ passes
    base.update(override)
    assert verify._final_verdict(**base) is False


# Certification-only: the positive precondition requires the COMMITTED closeout manifest
# to verify clean against the tree it ratified (``_ACCEPTANCE_ANCHOR``, materialized
# fresh) — a binding that only the C9 certification lane regenerates.
# ``integration``-marked per the repo convention: excluded from the default
# `-m "not integration"` unit selection, runnable explicitly / in the advisory lane. The
# assertion itself is unchanged.
@pytest.mark.integration
def test_frozen_manifest_check_fails_on_deleted_or_replaced_frozen_file(tmp_path: Path) -> None:
    """Criterion 2: a frozen file missing from reality, or a frozen file whose hash was
    changed (e.g. replaced with placeholder text), makes ``_verify_frozen_manifest`` fail —
    folding the verdict false."""
    stored, note = verify._load_manifest(_REPO_ROOT)
    assert stored is not None and note == "loaded"
    # Anti-vacuity: an empty frozen set would "verify clean" against anything.
    stored_files = stored.get("files")
    assert isinstance(stored_files, dict) and stored_files, (
        "the committed manifest must carry frozen entries, or nothing below is a real check"
    )

    # The COMMITTED manifest, against the tree it was ratified over. Comparing it to the
    # working tree instead would report a later campaign's edits as closeout drift.
    ratified = _materialize_anchor_tree(tmp_path / "ratified")
    ok_now, now_note = verify._verify_frozen_manifest(ratified, stored, note)
    assert ok_now is True, f"the committed manifest must verify clean on the ratified tree: {now_note}"

    # Mutate a single stored hash → drift (this is what replacing a frozen artifact with
    # placeholder text looks like to the byte-hash check).
    tampered = json.loads(json.dumps(stored))  # deep copy
    some_file = next(iter(tampered["files"]))
    tampered["files"][some_file] = "0" * 64
    ok_drift, drift_note = verify._verify_frozen_manifest(ratified, tampered, "loaded")
    assert ok_drift is False and "drift" in drift_note.lower()


# ---- Criterion 5 positive counterpart (needs the real npm toolchain) ------------


def _mirror_frontend(
    real_frontend: Path, dst_frontend: Path, vitest_rel_parts: tuple[str, ...], keep: set[str]
) -> None:
    """A symlink mirror of the frontend whose closeout vitest dir is shadowed to ``keep``
    (copied from the frozen G13(b) helper). ``node_modules`` is symlinked so ``npx``
    resolves offline; ``index.html`` is copied; ``dist`` is skipped."""
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
    for child in cur_real.iterdir():
        if child.name in keep:
            (cur_dst / child.name).symlink_to(child)


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


@pytest.mark.integration
def test_run_frontend_lane_green_true_on_all_passed_browser_report(tmp_path: Path) -> None:
    """Criterion 5 positive counterpart to frozen G13(b): with vitest+typecheck+build green
    (curated mirror) AND an all-passed, full-spec-set ``frontend-e2e.json`` present in the
    evidence dir, ``_run_frontend_lane`` returns green True — proving the browser gate folds
    into green in the TRUE direction (the frozen G13(b) proves the FALSE/absent direction).
    ``integration``-marked: it runs the real npm toolchain and is excluded from the fast
    required lane."""
    if shutil.which("npx") is None or shutil.which("npm") is None:
        pytest.skip("needs the node/npm/npx toolchain")

    real_vitest_dir = _REPO_ROOT / str(manifest_mod.FRONTEND_VITEST_DIR)
    green_present = sorted(
        p.name for p in real_vitest_dir.iterdir() if p.name in _GREEN_VITEST_FILES
    )
    keep = set(green_present)
    frontend_inventory = {name: {} for name in green_present}
    vitest_rel = Path(str(manifest_mod.FRONTEND_VITEST_DIR).removeprefix("current/")).relative_to("frontend").parts

    mirror_repo = tmp_path / "repo"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _mirror_frontend(_REPO_ROOT / "frontend", mirror_repo / "frontend", vitest_rel, keep)

    # An all-passed, full-spec-set browser report present in the evidence dir.
    e2e_dir = mirror_repo / "frontend" / manifest_mod.FRONTEND_E2E_DIR.removeprefix("current/").split("/", 1)[1]
    specs = {p.name for p in e2e_dir.glob("*.spec.ts")}
    assert specs, "the mirror must expose the frozen e2e spec set"
    (evidence_dir / "frontend-e2e.json").write_text(
        json.dumps(_pw_report(specs, expected=len(specs))), encoding="utf-8"
    )

    lane = verify._run_frontend_lane(mirror_repo, evidence_dir, frontend_inventory)
    assert lane.detail["playwright_e2e"]["browser_ok"] is True, lane.detail["playwright_e2e"]
    assert lane.green is True, lane.detail
