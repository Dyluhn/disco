"""A moved frontend public target is a move, not a deletion — when it really is.

A public target's identity is ``(surface, path, public_name)``, and a
TypeScript re-export cannot keep a name alive at its old path the way a Python
``__init__.py`` can. ``_relocations`` is the authority that tells a genuine move
from a deletion. These are its adversarial cases: each rejection below is a way
to retire a public capability while claiming it merely moved.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "development" / "scripts"))

from architecture import public_api  # noqa: E402
from architecture.public_api_parts import _relocations  # noqa: E402

# Advanced by the package that lands a relocation; 0 until one does.
# PKG-38-UI-FIXES-V51 is the first: the three ActivityFeed download components
# and the image-gen setup note, all four moved without losing a capability.
SHIPPED_RELOCATIONS = 4
OLD_PATH = "frontend/src/components/build/ActivityFeed.tsx"
NEW_PATH = "frontend/src/components/build/activityFeedParts/FileDownload.tsx"
NAME = "FileDownload"


def accepting_commit() -> str:
    """A real commit, so the resolvability obligation is exercised, not stubbed."""
    return (
        subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]).decode().strip()
    )


def record(
    *,
    old_path: str = OLD_PATH,
    old_name: str = NAME,
    new_path: str = NEW_PATH,
    new_name: str = NAME,
    kind: str = "function",
    surface: str = "frontend",
) -> dict[str, Any]:
    return {
        "surface": surface,
        "old_path": old_path,
        "old_public_name": old_name,
        "new_path": new_path,
        "new_public_name": new_name,
        "declaration_kind": kind,
        "reason": "the module went over budget and the component moved out",
        "owner_package": "PKG-38-UI-FIXES-V51",
        "accepting_commit": accepting_commit(),
        "accepting_receipt": "PKG-38-UI-FIXES-V51: exact frontend public target relocation",
    }


def targets(*identities: tuple[str, str, str]) -> dict[tuple[str, str, str, str], Any]:
    """Build a live-surface map: one target per identity, all functions."""
    return {
        (*identity, f"{index:064d}"): {"declaration": {"kind": "function", "name": identity[2]}}
        for index, identity in enumerate(identities)
    }


LIVE = targets(("frontend", NEW_PATH, NAME))


def authority(rows: list[dict[str, Any]], problems: list[str]):
    return _relocations.relocation_authority(
        {"frontend_target_relocations": sorted(rows, key=_relocations.canonical)},
        REPO_ROOT,
        problems,
    )


def test_a_valid_relocation_sanctions_exactly_its_own_deletion():
    problems: list[str] = []
    records = authority([record()], problems)
    assert problems == []
    assert _relocations.sanctioned_deletions(records, LIVE, problems) == {
        ("frontend", OLD_PATH, NAME)
    }
    assert problems == []


def test_a_relocation_whose_destination_does_not_exist_is_rejected():
    """Without this, "relocated" is a synonym for "deleted"."""
    problems: list[str] = []
    records = authority([record()], problems)
    assert _relocations.sanctioned_deletions(records, targets(), problems) == set()
    assert any("does not name one live public target" in problem for problem in problems)


def test_a_relocation_whose_origin_is_still_public_is_rejected():
    """A record may not sanction a deletion that did not happen."""
    problems: list[str] = []
    records = authority([record()], problems)
    live = targets(("frontend", NEW_PATH, NAME), ("frontend", OLD_PATH, NAME))
    assert _relocations.sanctioned_deletions(records, live, problems) == set()
    assert any("origin is still public" in problem for problem in problems)


def test_a_destination_of_a_different_declaration_kind_is_rejected():
    """A component that reappears as a type alias is a different change."""
    problems: list[str] = []
    records = authority([record(kind="type_alias")], problems)
    assert _relocations.sanctioned_deletions(records, LIVE, problems) == set()
    assert any("declaration_kind does not match" in problem for problem in problems)


def test_two_targets_relocating_onto_one_destination_are_rejected():
    """Two public names collapsing into one is lost surface, not a move."""
    problems: list[str] = []
    authority([record(), record(old_name="SheetDownload")], problems)
    assert any("duplicate frontend target relocation destination" in p for p in problems)


def test_a_relocation_chain_is_rejected():
    """A chain would let one record be re-aimed at a different destination."""
    middle = "frontend/src/components/build/activityFeedParts/Interim.tsx"
    problems: list[str] = []
    authority(
        [record(new_path=middle), record(old_path=middle, new_path=NEW_PATH)],
        problems,
    )
    assert any("chains onto itself" in problem for problem in problems)


def test_a_record_that_moves_nothing_is_rejected():
    problems: list[str] = []
    authority([record(new_path=OLD_PATH)], problems)
    assert problems == ["frontend_target_relocations has invalid explicit metadata"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"surface": "python"},
        {"owner_package": "not-a-package"},
        {"accepting_commit": "f" * 40},
        {"reason": ""},
    ],
)
def test_a_malformed_record_is_rejected(mutation: dict[str, Any]):
    """Obligations 1 and 2: no wildcards, no defaults, no partial records."""
    problems: list[str] = []
    assert authority([{**record(), **mutation}], problems) == {}
    assert problems == ["frontend_target_relocations has invalid explicit metadata"]


def test_a_missing_or_extra_field_is_rejected():
    problems: list[str] = []
    short = record()
    del short["reason"]
    assert authority([short], problems) == {}
    assert authority([{**record(), "extra": "x"}], problems) == {}
    assert problems == ["frontend_target_relocations has invalid explicit metadata"] * 2


def test_a_record_with_no_relocation_this_run_is_extra_and_an_accepted_one_is_frozen():
    """Stale and extra records fail; an accepted record may not be edited."""
    row = record()
    previous = {"frontend_target_relocations": []}
    inventory = {"frontend_target_relocations": [row]}
    problems: list[str] = []
    _relocations.check_relocation_delta(previous, inventory, REPO_ROOT, set(), problems)
    assert any("do not exactly authorize regeneration" in problem for problem in problems)

    problems = []
    _relocations.check_relocation_delta(
        previous, inventory, REPO_ROOT, {("frontend", OLD_PATH, NAME)}, problems
    )
    assert problems == []

    edited = {**row, "reason": "rewritten after the fact"}
    problems = []
    _relocations.check_relocation_delta(
        inventory, {"frontend_target_relocations": [edited]}, REPO_ROOT, set(), problems
    )
    assert any("accepted frontend target relocation changed" in problem for problem in problems)


def test_the_shipped_records_are_valid_and_fully_reasoned():
    """The V51 relocations are a live authority, re-proved against the surface."""
    baseline = public_api.load_public_api(REPO_ROOT)
    rows = baseline.get("frontend_target_relocations", [])
    problems: list[str] = []
    records = _relocations.relocation_authority(baseline, REPO_ROOT, problems)
    assert problems == []
    assert len(records) == len(rows) == SHIPPED_RELOCATIONS
    assert all(row["reason"].strip() for row in rows)
