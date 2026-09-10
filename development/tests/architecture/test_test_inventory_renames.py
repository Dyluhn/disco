"""A renamed test is a rename, not a deletion — and only when it really is one.

The inventory keys a test on ``path::name``, so a rename is byte-identical to
deleting one test and inventing another. ``_renames`` is the authority that
tells the two apart. These are its adversarial cases: the shapes that must be
accepted, and the ones that must not be, because each of them is a way to
launder lost coverage through a record that says "rename".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "development" / "scripts"))

from architecture import test_inventory  # noqa: E402
from architecture.test_inventory_parts import _renames, _transitions  # noqa: E402

PATH = "packages/tools/tests/test_audio_overview_truncation.py"
OTHER = "packages/tools/tests/test_audio_overview_integration.py"
# Advanced by the package that lands a rename; 0 until one does.
SHIPPED_RENAME_FILES = 0
SHIPPED_RENAMES = 0
OLD = f"{PATH}::test_short_batch_is_still_rejected"
NEW = f"{PATH}::test_short_batch_is_asked_for_again_not_rejected"


def accepting_commit() -> str:
    """A real commit, so obligation 2 is exercised rather than stubbed."""
    return (
        subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]).decode().strip()
    )


def record(pairs: list[tuple[str, str]], *, path: str = PATH, count: int | None = None) -> Any:
    return {
        "path": path,
        "renames": sorted(
            (
                {"old_id": old, "new_id": new, "reason": "behaviour restated under a true name"}
                for old, new in pairs
            ),
            key=lambda pair: pair["old_id"],
        ),
        "renamed_id_count": len(pairs) if count is None else count,
        "owner_package": "PKG-38-UI-FIXES-V51",
        "accepting_commit": accepting_commit(),
        "accepting_receipt": "PKG-38-UI-FIXES-V51: exact one-to-one test rename",
    }


def mapping(python_ids: list[str], files: list[str] | None = None) -> dict[str, Any]:
    return {
        "python_test_files": [PATH, OTHER] if files is None else files,
        "typescript_test_files": [],
        "python_static_test_ids": python_ids,
        "typescript_static_test_ids": [],
    }


def authorizations(
    records: list[Any],
    previous_ids: list[str],
    current_ids: list[str],
    *,
    files: list[str] | None = None,
) -> tuple[dict[str, set[str]], Any]:
    return test_inventory._rename_authorizations(
        REPO_ROOT,
        records,
        {"packages": previous_ids},
        {"packages": current_ids},
        mapping(previous_ids),
        mapping(current_ids, files),
    )


def assert_no_deletions(authorized: dict[str, set[str]], previous: list[str], current: list[str]):
    return test_inventory._assert_no_deletions(
        "collected.packages",
        previous,
        current,
        authorized=authorized.get("collected.packages"),
    )


def test_a_valid_rename_is_accepted_and_reads_as_one_addition():
    """The whole point: same file, one-to-one, the new name really is there."""
    authorized, ledger = authorizations([record([(OLD, NEW)])], [OLD], [NEW])

    assert authorized["collected.packages"] == {test_inventory._canonical_row(OLD)}
    assert authorized["mapping_static.python_static_test_ids"] == {
        test_inventory._canonical_row(OLD)
    }
    assert ledger.count_problems() == []
    # The deletion is explained, and the new identity is an ordinary addition.
    assert assert_no_deletions(authorized, [OLD], [NEW]) == [NEW]


def test_a_rename_whose_target_does_not_exist_is_rejected():
    """Obligation 5. Without this, "renamed" is a synonym for "deleted"."""
    authorized, _ = authorizations([record([(OLD, NEW)])], [OLD], [])

    assert authorized["collected.packages"] == set()
    with pytest.raises(RuntimeError, match="unexplained deletion"):
        assert_no_deletions(authorized, [OLD], [])


def test_a_many_to_one_rename_is_rejected():
    """Two tests collapsing into one is lost coverage, not a rename."""
    second = f"{PATH}::test_duplicate_index_uses_one_normal_malformed_retry"
    with pytest.raises(RuntimeError, match="renamed test transitions are invalid"):
        authorizations([record([(OLD, NEW), (second, NEW)])], [OLD, second], [NEW])


def test_a_rename_onto_a_name_that_already_existed_is_rejected():
    """The target must be a real addition, not a test borrowed to excuse a loss."""
    authorized, _ = authorizations([record([(OLD, NEW)])], [OLD, NEW], [NEW])

    assert authorized["collected.packages"] == set()
    with pytest.raises(RuntimeError, match="unexplained deletion"):
        assert_no_deletions(authorized, [OLD, NEW], [NEW])


def test_an_unmapped_deletion_is_still_an_unexplained_deletion():
    """A record for one rename does not excuse the deletion beside it."""
    gone = f"{PATH}::test_repeated_length_stop_fails_actionably_and_bounded"
    authorized, ledger = authorizations([record([(OLD, NEW)])], [OLD, gone], [NEW])

    assert ledger.count_problems() == []
    with pytest.raises(RuntimeError, match="unexplained deletion") as error:
        assert_no_deletions(authorized, [OLD, gone], [NEW])
    assert gone in str(error.value) and OLD not in str(error.value)


def test_a_deletion_with_no_record_at_all_still_fails():
    """The original refusal is unchanged where no authority speaks."""
    authorized, _ = authorizations([], [OLD], [NEW])

    assert authorized["collected.packages"] == set()
    with pytest.raises(RuntimeError, match="unexplained deletion"):
        assert_no_deletions(authorized, [OLD], [NEW])


def test_a_rename_that_moves_the_test_to_another_file_is_rejected():
    """That is a relocation, and module_split_transitions owns it."""
    moved = f"{OTHER}::test_short_batch_is_asked_for_again_not_rejected"
    with pytest.raises(RuntimeError, match="renamed test transitions are invalid"):
        authorizations([record([(OLD, moved)])], [OLD], [moved])


def test_the_pinned_count_must_equal_the_observed_renames():
    """Obligation 6: the record is a ratchet, not a standing permission."""
    inflated = record([(OLD, NEW)], count=2)
    with pytest.raises(RuntimeError, match="renamed test transitions are invalid"):
        authorizations([inflated], [OLD], [NEW])

    second = f"{PATH}::test_duplicate_index_uses_one_normal_malformed_retry"
    target = f"{PATH}::test_duplicate_index_costs_no_round_trip"
    both = record([(OLD, NEW), (second, target)])
    # Only one of the two pinned renames actually happened.
    _, ledger = authorizations([both], [OLD, second], [NEW, second])
    assert ledger.count_problems() == [
        f"renamed test file {PATH}: collected.packages pinned 2, observed 1",
        f"renamed test file {PATH}: mapping_static.python_static_test_ids pinned 2, observed 1",
    ]


def test_an_absorbed_record_observes_nothing_and_stays_valid():
    """Once the rename is baselined the deletion is gone; zero is not a failure."""
    _, ledger = authorizations([record([(OLD, NEW)])], [NEW], [NEW])
    assert ledger.count_problems() == []


def test_the_record_path_must_still_be_a_test_file():
    """Obligation 7: a record over a file that no longer exists proves nothing."""
    with pytest.raises(RuntimeError, match="not a test file in the inventory"):
        authorizations([record([(OLD, NEW)])], [OLD], [NEW], files=[OTHER])


def test_a_stored_record_is_re_proved_against_the_inventory_it_ships_in():
    """The gate re-checks the landed record, not only the regeneration."""
    records = _renames.rename_authority(
        {"renamed_test_transitions": [record([(OLD, NEW)])]}, REPO_ROOT, []
    )
    assert _renames.identity_problems(records, [NEW]) == []
    assert _renames.identity_problems(records, [OLD, NEW]) == [
        f"renamed test {OLD}: the old identity still exists"
    ]
    assert _renames.identity_problems(records, []) == [f"renamed test {OLD}: {NEW} does not exist"]


def test_the_shipped_records_are_valid_and_fully_reasoned():
    """The records in the authority are live, not a one-off script.

    ``SHIPPED_*`` advance in the package that lands a rename, exactly like the
    collected-id counts in ``test_test_inventory.py``: the totals are pinned so
    a rename cannot appear in the sealed authority without a package owning it.
    """
    baseline = test_inventory.load_test_inventory(REPO_ROOT)
    rows = baseline.get("renamed_test_transitions", [])
    assert test_inventory._rename_record_problems(baseline, REPO_ROOT) == []
    pairs = [pair for row in rows for pair in row["renames"]]
    assert sum(row["renamed_id_count"] for row in rows) == len(pairs)
    assert all(pair["reason"].strip() for pair in pairs)
    assert (len(rows), len(pairs)) == (SHIPPED_RENAME_FILES, SHIPPED_RENAMES)


def test_an_earlier_packages_addition_receipt_survives_the_rename():
    """PKG-37 added a test; PKG-38 renamed it. The receipt must stay as written.

    Rewriting the earlier package's ``added_ids`` to name the new test would be
    the falsification — that package never added a test by that name. The claim
    is resolved through the rename map instead, so the identity is still proved
    to exist under the name it now has.
    """
    resolved = _renames.current_identity_map({"renamed_test_transitions": [record([(OLD, NEW)])]})
    assert resolved == {OLD: NEW}

    problems: list[str] = []
    _transitions.check_subset("PKG-37.added_ids", [OLD], [NEW], problems, resolved)
    assert problems == []
    _transitions.check_subset("PKG-37.added_ids", [OLD], [], problems, resolved)
    assert problems == [
        f"PKG-37.added_ids additions are absent from the current inventory: ['{NEW}']"
    ]
    # With no map the original exact-presence check is unchanged.
    strict: list[str] = []
    _transitions.check_subset("PKG-37.added_ids", [OLD], [NEW], strict)
    assert strict == [
        f"PKG-37.added_ids additions are absent from the current inventory: ['{OLD}']"
    ]
