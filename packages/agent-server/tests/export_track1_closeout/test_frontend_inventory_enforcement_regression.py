"""Mutation-regression proof that the closeout verifier's FRONTEND per-file title
enforcement WORKS — that filename-set parity alone can no longer let a load-bearing
vitest node be dropped, renamed, skipped, or swapped inside a frozen file.

Background (the hole this closes): the acceptance manifest's
``frontend_closeout_inventory`` freezes, per vitest FILE, the set of ``it(...)`` titles;
but the verifier's frontend lane historically compared only the discovered test-FILE set
(``frozen_inventory.keys()`` vs the basenames vitest reported) and never the per-file
titles. So renaming/dropping/skipping a title inside a frozen file was invisible — the
G08 bound nodes could vanish from the run yet the lane stay as green as ever.

The verifier now extracts, per file, the set of EXECUTED (passed|failed) leaf titles
(``_parse_vitest``) and diffs them against the frozen ``tests`` inventory via a pure,
importable helper (``_diff_frontend_titles``); ``_run_frontend_lane`` uses that helper as
its single source of truth. This file is the committed proof that the helper (and the
extractor feeding it) actually BITE.

Like the sibling ``test_evidence_hygiene_regression.py`` / ``test_g13_verifier_truthfulness.py``,
it IMPORTS the real verifier module and calls its real functions on real synthetic data
/ a real temporary filesystem — that is the CODE UNDER TEST, not a mock, and is allowed
by the anti-bypass contract (no ``unittest.mock`` / ``MagicMock`` / ``patch`` /
``monkeypatch``). These are GREEN regression tests: each asserts the enforcement behaves
correctly, so they PASS. They are deterministic and OFFLINE — no vitest is ever run;
synthetic Vitest-JSON-shaped payloads exercise the extractor directly.

Six properties are pinned:

1. An EXACT title-set match is ACCEPTED (ok True).
2. DROPPING a G08 bound title -> REJECTED (a dropped node cannot pass).
3. RENAMING a G08 bound title -> REJECTED.
4. A FILENAME-ONLY match (every file present, identical per-file COUNT, one title
   differs) -> REJECTED. The key property: filename-set equality is insufficient.
5. An EXTRA/unexpected observed title -> REJECTED.
6. A skipped/pending/todo leaf is UNREPORTED — its title is excluded by the extractor
   (proven by feeding a synthetic vitest JSON through ``_parse_vitest``), so it reads as
   a missing title and is REJECTED; passed AND failed leaves ARE reported (so the real
   G08 bound RED nodes still satisfy the title set).

Plus a single-source-of-truth guard: ``_run_frontend_lane`` genuinely delegates to
``_diff_frontend_titles`` (asserted by source inspection, so a future inline re-diff that
diverges is caught).
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

# ``development/scripts/`` is not an installed package. Put it on ``sys.path`` exactly as the verifier
# resolves its sibling manifest module and as the sibling closeout regressions do.
# ``importlib.import_module`` (a call, not a top-level import of code-under-test) keeps
# this lint-clean without a suppression directive.
_REPO_ROOT = Path(__file__).resolve().parents[5]
_SCRIPTS_DIR = _REPO_ROOT / "development" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

verify = importlib.import_module("verify_export_track1_closeout")

pytestmark = pytest.mark.export_track1_closeout

# ---- synthetic frozen inventory (mirrors frontend_closeout_inventory shape) --------
# Basenames + their frozen ``it(...)`` titles. G08 is the load-bearing file: two BOUND
# cases (materially different fixtures) + one UNBOUND case. c3/c6 stand in for the
# single-test files so the fixture is a faithful multi-file inventory.
_G08 = "g08-download-url-binding.test.tsx"
_C3 = "c3-candidate-copy.test.tsx"
_C6 = "c6-collision-blockers.test.tsx"

_BOUND_A = "carries the SUPPLIED binding [v7 / g08a-0007] on the download URL"
_BOUND_B = "carries the SUPPLIED binding [v42 / g08b-0042] on the download URL"
_UNBOUND = "G11: an unbound release (binding=null) must not fabricate a binding on the URL"
_C3_TITLE = "renders 'Bundle available' + 'Not runtime-verified' and no case-insensitive 'ready'"
_C6_TITLE = "renders every collision blocker (code + exact path) and only the plain download"


def _frozen_inventory() -> dict[str, object]:
    """A faithful frozen inventory: three files, G08 carrying all three of its titles."""
    return {
        _C3: {"describe": "WO-A (C3)", "tests": [_C3_TITLE]},
        _C6: {"describe": "WO-A (C6)", "tests": [_C6_TITLE]},
        _G08: {
            "describe": "WO-A (G08) — the download URL binds to the release's ...",
            "tests": [_BOUND_A, _BOUND_B, _UNBOUND],
        },
    }


def _observed_from_frozen(frozen: dict[str, object]) -> dict[str, set[str]]:
    """The per-file EXECUTED-title map that an untampered run would report: exactly the
    frozen ``tests`` of every file, as a set."""
    observed: dict[str, set[str]] = {}
    for name, meta in frozen.items():
        assert isinstance(meta, dict)
        tests = meta["tests"]
        assert isinstance(tests, list)
        observed[name] = {str(t) for t in tests}
    return observed


def test_exact_title_match_is_accepted() -> None:
    """(1) When every file reports EXACTLY its frozen titles, the helper accepts."""
    frozen = _frozen_inventory()
    ok, detail = verify._diff_frontend_titles(frozen, _observed_from_frozen(frozen))
    assert ok is True, f"an exact per-file title match must be accepted; detail={detail!r}"
    assert detail["title_mismatches"] == {}, (
        f"a clean match must record no per-file mismatch; got {detail['title_mismatches']!r}"
    )


def test_dropping_a_bound_title_is_rejected() -> None:
    """(2) A dropped G08 bound node (title absent from the observed set) must be rejected —
    a load-bearing node cannot silently vanish from the run."""
    frozen = _frozen_inventory()
    observed = _observed_from_frozen(frozen)
    observed[_G08].discard(_BOUND_A)  # the run no longer reports the v7 bound node

    ok, detail = verify._diff_frontend_titles(frozen, observed)

    assert ok is False, "dropping a frozen G08 bound title must be rejected"
    mismatches = detail["title_mismatches"]
    assert isinstance(mismatches, dict) and _G08 in mismatches, (
        f"the rejection must name the G08 file; got {mismatches!r}"
    )
    assert _BOUND_A in mismatches[_G08]["missing"], (
        f"the dropped title must be reported missing; got {mismatches[_G08]!r}"
    )


def test_renaming_a_bound_title_is_rejected() -> None:
    """(3) Renaming a G08 bound title (frozen title missing, a different one present) must
    be rejected — the frozen node is gone even though the file still runs three tests."""
    frozen = _frozen_inventory()
    observed = _observed_from_frozen(frozen)
    renamed = _BOUND_B + " (renamed)"
    observed[_G08].discard(_BOUND_B)
    observed[_G08].add(renamed)

    ok, detail = verify._diff_frontend_titles(frozen, observed)

    assert ok is False, "renaming a frozen G08 bound title must be rejected"
    entry = detail["title_mismatches"][_G08]
    assert _BOUND_B in entry["missing"] and renamed in entry["extra"], (
        f"a rename must show as the old title missing + the new title extra; got {entry!r}"
    )


def test_filename_set_equality_is_insufficient() -> None:
    """(4) THE KEY PROPERTY. Every file is present on both sides AND every file reports the
    SAME NUMBER of titles it froze, but one G08 title is swapped for a look-alike. A
    filename-set comparison (frozen basenames vs observed basenames) is satisfied, yet the
    title diff must REJECT — filename parity alone cannot pass."""
    frozen = _frozen_inventory()
    observed = _observed_from_frozen(frozen)
    lookalike = _BOUND_B.replace("g08b-0042", "g08b-9999")  # same count, one title differs
    observed[_G08].discard(_BOUND_B)
    observed[_G08].add(lookalike)

    # Pre-condition: the two checks the OLD lane relied on both pass here.
    assert set(frozen) == set(observed), "the filename sets are deliberately equal"
    assert len(observed[_G08]) == len(_frozen_inventory()[_G08]["tests"]), (
        "per-file title COUNT is deliberately unchanged"
    )

    ok, detail = verify._diff_frontend_titles(frozen, observed)

    assert ok is False, (
        "filename-set + count parity must NOT satisfy the check when a title differs; "
        "the per-file title diff has to bite"
    )
    entry = detail["title_mismatches"][_G08]
    assert _BOUND_B in entry["missing"] and lookalike in entry["extra"], (
        f"the swapped title must show missing old + extra look-alike; got {entry!r}"
    )


def test_extra_observed_title_is_rejected() -> None:
    """(5) An extra/unexpected observed title in a frozen file must be rejected — a file
    may not grow an un-frozen node."""
    frozen = _frozen_inventory()
    observed = _observed_from_frozen(frozen)
    surprise = "an unexpected extra node the manifest never froze"
    observed[_G08].add(surprise)

    ok, detail = verify._diff_frontend_titles(frozen, observed)

    assert ok is False, "an extra observed title must be rejected"
    assert surprise in detail["title_mismatches"][_G08]["extra"], (
        f"the surprise title must be reported extra; got {detail['title_mismatches'][_G08]!r}"
    )


def _write_vitest_json(path: Path, g08_statuses: dict[str, str]) -> None:
    """Write a synthetic Vitest ``--reporter=json`` payload for the single G08 file, each
    of its three leaf titles carrying the supplied status. Faithful to the Jest-shaped
    schema ``_parse_vitest`` reads (numTotalTests/... + testResults[].assertionResults[])."""
    assertions = [{"title": title, "status": status} for title, status in g08_statuses.items()]
    payload = {
        "numTotalTests": len(g08_statuses),
        "numPassedTests": sum(1 for s in g08_statuses.values() if s == "passed"),
        "numFailedTests": sum(1 for s in g08_statuses.values() if s == "failed"),
        "numPendingTests": sum(1 for s in g08_statuses.values() if s in {"pending", "skipped"}),
        "numTodoTests": sum(1 for s in g08_statuses.values() if s == "todo"),
        "testResults": [
            {
                "name": f"/abs/frontend/src/test/export-track1-closeout/{_G08}",
                "assertionResults": assertions,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("skip_status", ["skipped", "pending", "todo"])
def test_skipped_leaf_is_unreported_and_rejected(skip_status: str, tmp_path: Path) -> None:
    """(6) A non-executed leaf (skipped/pending/todo) is EXCLUDED by the extractor, so its
    title reads as UNREPORTED (missing) and the diff rejects. Proven end-to-end by feeding
    a synthetic vitest JSON through the REAL ``_parse_vitest`` (no vitest run), then the
    REAL ``_diff_frontend_titles``. The other two G08 leaves stay executed (passed/failed),
    so the rejection is specifically the skipped node, not a collapsed file."""
    vitest_json = tmp_path / "frontend-vitest.json"
    _write_vitest_json(
        vitest_json,
        {_BOUND_A: skip_status, _BOUND_B: "failed", _UNBOUND: "passed"},
    )

    counts, discovered, observed_titles, parsed_ok = verify._parse_vitest(vitest_json)

    assert parsed_ok is True, "the synthetic vitest JSON must parse"
    assert _G08 in discovered, "the G08 file must be discovered"
    assert counts["total"] == 3, f"three leaves were emitted; got counts={counts!r}"
    # The skipped/pending/todo leaf is excluded; the executed (failed+passed) ones remain.
    assert _BOUND_A not in observed_titles[_G08], (
        f"a {skip_status!r} leaf must be excluded from the executed-title set; "
        f"got {sorted(observed_titles[_G08])!r}"
    )
    assert {_BOUND_B, _UNBOUND} <= observed_titles[_G08], (
        "executed (failed AND passed) leaves must be reported so real RED nodes still count; "
        f"got {sorted(observed_titles[_G08])!r}"
    )

    ok, detail = verify._diff_frontend_titles(_frozen_inventory(), observed_titles)

    assert ok is False, f"an unreported ({skip_status!r}) frozen title must be rejected"
    assert _BOUND_A in detail["title_mismatches"][_G08]["missing"], (
        f"the {skip_status!r} node's title must read as missing; "
        f"got {detail['title_mismatches'][_G08]!r}"
    )


def test_run_frontend_lane_delegates_to_the_pure_helper() -> None:
    """Single-source-of-truth guard: ``_run_frontend_lane`` genuinely calls
    ``_diff_frontend_titles`` (not an inline re-diff that could drift from the helper the
    regression exercises). Source inspection keeps this offline + deterministic."""
    src = inspect.getsource(verify._run_frontend_lane)
    assert "_diff_frontend_titles(" in src, (
        "the frontend lane must delegate title enforcement to _diff_frontend_titles so the "
        "committed regression and the live lane share one implementation"
    )
