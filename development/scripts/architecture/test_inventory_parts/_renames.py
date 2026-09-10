"""Renamed test identities: the fourth phenomenon behind the word "deletion".

:mod:`._splits` records that measurement found "three distinct phenomena
hiding behind the single word 'deletion'" — split relocation, partial
extraction and line drift.  A *rename* is a fourth, and that module rules it
out in as many words (obligation 5: "A rename is not a relocation"), because a
relocation is defined by the identity surviving unchanged at a new path.

A rename is the mirror image: the **path** survives unchanged and the **name**
moves.  The inventory keys a test on ``path::name``, so renaming a test reads
as deleting one test and inventing another — and the two are genuinely
indistinguishable to a byte comparison.  The cheapest real example: appending
a ticket id to a test title.  Nothing about the test changed, and the gate
correctly refused to regenerate.

Before this module the only way to sanction that was a finite, single-use
retirement authority (:mod:`._retirements`, pinned to one package at one
sealed baseline).  Spending a retirement on a rename would write a false
justification — "this test was retired" — into a sealed authority, and would
be spent again by the next landing that renames anything.  So renames are
modelled here as what they are, with a record type that can be used more than
once and still cannot launder a deletion.

Fail-closed obligations, every one required, no wildcards:

1. exact field set; every scalar non-empty; ``owner_package`` well-formed;
2. ``accepting_commit`` is a full SHA that resolves in this repository, and
   ``accepting_receipt`` is named;
3. **same file**: every ``old_id`` and ``new_id`` is ``"<path>::<name>"`` for
   this record's own ``path``, and the two names differ.  A rename that also
   moves the test is a relocation and belongs to :mod:`._splits`;
4. **one-to-one, in both directions**: ``old_id`` values are unique, ``new_id``
   values are unique, and the two sets are disjoint.  Many-to-one (two tests
   collapsing into one) is therefore rejected — that is lost coverage wearing
   a rename's clothes — and so is a rename onto a name that already existed;
5. **the new identity must exist**: at regeneration the ``new_id`` must be
   present in the freshly collected inventory *and* absent from the accepted
   one, so it is a real addition and not a pre-existing test being borrowed to
   excuse a deletion.  An ``old_id`` no record maps is still an unexplained
   deletion, exactly as before;
6. ``renamed_id_count`` pins the number of pairs exactly, and the observed
   renames per label must equal that pin — the same ratchet ``_splits`` uses,
   with the same absorbed-record exemption at zero;
7. every record's ``path`` must still be a test file in the inventory.  A
   later split that moves one of these files will have to say so; that is the
   intended cost of a record that keeps proving something after it lands.

A renamed test is deliberately **not** parametrized-aware.  A parametrized
test collects as ``path::name[case]``, which no ``old_id`` matches, so such a
rename fails closed here rather than being half-authorized.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ._rows import canonical_row

RENAME_FIELDS = {
    "path",
    "renames",
    "renamed_id_count",
    "owner_package",
    "accepting_commit",
    "accepting_receipt",
}
RENAME_PAIR_FIELDS = {"old_id", "new_id", "reason"}
_PACKAGE = re.compile(r"PKG-\d{2}-[A-Z0-9-]+")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")


def _commit_resolves(root: Path, value: str) -> bool:
    """Resolve through ``inventory_static`` so there is one commit seam.

    Imported at call time and read off the module object: the adversarial
    suite stubs ``inventory_static.commit_identity_resolves`` when it
    regenerates into a temporary root, and a patch only takes effect on the
    module whose globals the reader consults.
    """
    from .. import inventory_static

    return inventory_static.commit_identity_resolves(root, value)


def _valid_scalars(row: dict[str, Any], root: Path) -> bool:
    """Obligations 1 and 2: the record names its own authority."""
    scalars = ("path", "owner_package", "accepting_commit", "accepting_receipt")
    if not all(isinstance(row.get(key), str) and row[key] for key in scalars):
        return False
    return (
        bool(_PACKAGE.fullmatch(row["owner_package"]))
        and bool(_GIT_COMMIT.fullmatch(row["accepting_commit"]))
        and _commit_resolves(root, row["accepting_commit"])
    )


def _valid_pair(pair: Any, path: str) -> bool:
    """Obligation 3: one pair, both halves in this record's own file."""
    if not isinstance(pair, dict) or set(pair) != RENAME_PAIR_FIELDS:
        return False
    if not all(isinstance(pair[key], str) and pair[key] for key in sorted(RENAME_PAIR_FIELDS)):
        return False
    prefix = f"{path}::"
    if not (pair["old_id"].startswith(prefix) and pair["new_id"].startswith(prefix)):
        return False
    old_name = pair["old_id"][len(prefix) :]
    new_name = pair["new_id"][len(prefix) :]
    return bool(old_name) and bool(new_name) and old_name != new_name


def _valid_pairs(row: dict[str, Any]) -> bool:
    """Obligations 3 and 4: a sorted, one-to-one, non-empty rename list."""
    pairs = row.get("renames")
    if not isinstance(pairs, list) or not pairs:
        return False
    if not all(_valid_pair(pair, row["path"]) for pair in pairs):
        return False
    if pairs != sorted(pairs, key=lambda pair: pair["old_id"]):
        return False
    old_ids = [pair["old_id"] for pair in pairs]
    new_ids = [pair["new_id"] for pair in pairs]
    if len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids):
        return False
    return not set(old_ids) & set(new_ids)


def _valid_count(row: dict[str, Any]) -> bool:
    """Obligation 6: the pair count is pinned, not merely described."""
    count = row.get("renamed_id_count")
    if not isinstance(count, int) or isinstance(count, bool):
        return False
    return count == len(row["renames"])


def _valid_rename_row(row: Any, root: Path) -> bool:
    """Obligations 1-4 and 6 — decomposed so each stays readable."""
    if not isinstance(row, dict) or set(row) != RENAME_FIELDS:
        return False
    if not _valid_scalars(row, root):
        return False
    return _valid_pairs(row) and _valid_count(row)


def rename_authority(
    baseline: dict[str, Any],
    root: Path,
    problems: list[str],
) -> dict[str, dict[str, Any]]:
    """Return the validated ``renamed_test_transitions`` map by path."""
    rows = baseline.get("renamed_test_transitions", [])
    if not isinstance(rows, list) or not all(_valid_rename_row(row, root) for row in rows):
        problems.append("renamed_test_transitions has invalid explicit metadata")
        return {}
    if rows != sorted(rows, key=lambda item: item["path"]):
        problems.append("renamed_test_transitions must be sorted by path")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["path"] in result:
            problems.append(f"duplicate rename record for {row['path']}")
            continue
        result[row["path"]] = row
    return result


def pairs_by_old_id(record: dict[str, Any]) -> dict[str, str]:
    return {pair["old_id"]: pair["new_id"] for pair in record["renames"]}


def path_presence_problems(
    records: dict[str, dict[str, Any]],
    test_files: list[str],
) -> list[str]:
    """Obligation 7 against the live file inventory."""
    present = set(test_files)
    return [
        f"renamed test file {path}: the path is not a test file in the inventory"
        for path in sorted(records)
        if path not in present
    ]


def identity_problems(
    records: dict[str, dict[str, Any]],
    static_ids: list[str],
) -> list[str]:
    """Re-prove obligation 5 against a stored inventory, not just at write time.

    The record keeps meaning something after it lands: the name it retired must
    still be gone and the name it introduced must still be there.
    """
    live = set(static_ids)
    problems: list[str] = []
    for path in sorted(records):
        for pair in records[path]["renames"]:
            if pair["old_id"] in live:
                problems.append(f"renamed test {pair['old_id']}: the old identity still exists")
            if pair["new_id"] not in live:
                problems.append(f"renamed test {pair['old_id']}: {pair['new_id']} does not exist")
    return problems


def current_identity_map(baseline: dict[str, Any]) -> dict[str, str]:
    """Map every renamed identity to the name it is known by NOW.

    A past package's addition receipt names the identity it actually added.
    Renaming that test later must not falsify the receipt, and rewriting the
    receipt to name the new test would be the falsification. So the receipt
    stays as written and is resolved through this map when the gate asks
    whether the identity it claims still exists.

    The map is one hop deep on purpose. Obligation 4 keeps a row's old and new
    identities disjoint, and a rename never leaves its file, so no chain can be
    recorded today; a landing that needs one will fail closed on that
    obligation, which is where the design decision belongs.

    Deliberately tolerant of a malformed record: :func:`rename_authority` is
    the validator and reports those separately, and a resolution that silently
    does nothing leaves the original strict presence check in force.
    """
    rows = baseline.get("renamed_test_transitions")
    resolved: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        pairs = row.get("renames") if isinstance(row, dict) else None
        for pair in pairs if isinstance(pairs, list) else []:
            old_id = pair.get("old_id") if isinstance(pair, dict) else None
            new_id = pair.get("new_id") if isinstance(pair, dict) else None
            if isinstance(old_id, str) and isinstance(new_id, str):
                resolved[old_id] = new_id
    return resolved


class RenameLedger:
    """Accumulates observed renames so pinned counts can be verified."""

    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self._records = records
        self._observed: dict[tuple[str, str], int] = {}

    def count(self, path: str, label: str, amount: int = 1) -> None:
        key = (path, label)
        self._observed[key] = self._observed.get(key, 0) + amount

    def authorized_ids(
        self,
        label: str,
        previous: list[str],
        current: list[str],
    ) -> tuple[set[str], list[str]]:
        """Return the renamed old IDs and the deletions no record explains."""
        live = set(current)
        prior = set(previous)
        authorized: set[str] = set()
        unexplained: list[str] = []
        for node_id in previous:
            if node_id in live:
                continue
            record = self._records.get(node_id.partition("::")[0])
            new_id = pairs_by_old_id(record).get(node_id) if record else None
            # Obligation 5: the new identity must be a real addition.
            if new_id is None or new_id not in live or new_id in prior:
                unexplained.append(node_id)
                continue
            authorized.add(node_id)
            self.count(str(record["path"]), label)
        return authorized, unexplained

    def count_problems(self) -> list[str]:
        """Obligation 6: observed renames must equal the pinned count.

        A record is *absorbed* once its transition has been baselined: the
        deletions it explained are no longer visible, so it observes zero on
        every later regeneration. Zero is therefore permitted, and any other
        value must equal the pin exactly.
        """
        problems: list[str] = []
        for (path, label), observed in sorted(self._observed.items()):
            pinned = self._records[path]["renamed_id_count"]
            if observed != pinned:
                problems.append(
                    f"renamed test file {path}: {label} pinned {pinned}, observed {observed}"
                )
        return problems


_STATIC_LABELS = ("python_static_test_ids", "typescript_static_test_ids")


def build_authorizations(
    records: dict[str, dict[str, Any]],
    previous_mapping: dict[str, Any],
    current_mapping: dict[str, Any],
    previous_roots: dict[str, list[str]],
    current_roots: dict[str, list[str]],
) -> tuple[dict[str, set[str]], RenameLedger]:
    """Return per-label authorized deletions and the ledger that pins them.

    Nothing is authorized by default: a label absent from the result keeps
    ``_assert_no_deletions``'s absolute refusal.
    """
    ledger = RenameLedger(records)
    authorized: dict[str, set[str]] = {}
    for name, previous in previous_roots.items():
        label = f"collected.{name}"
        renamed, _ = ledger.authorized_ids(label, previous, current_roots[name])
        authorized[label] = {canonical_row(item) for item in renamed}
    for key in _STATIC_LABELS:
        label = f"mapping_static.{key}"
        renamed, _ = ledger.authorized_ids(label, previous_mapping[key], current_mapping[key])
        authorized[label] = {canonical_row(item) for item in renamed}
    return authorized, ledger
