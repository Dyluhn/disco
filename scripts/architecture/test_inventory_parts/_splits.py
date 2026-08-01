"""Module-split transition authority for the test inventory (Epic 10-D).

``regenerate_inventory`` refuses any deletion outright ("unexplained deletion
in collected.packages").  That is correct for a vanished test and wrong for a
*relocated* one, so the baseline could not be regenerated after Epics 8-10
split four oversized test modules — and the gate stayed red for three epics.

Measurement found three distinct phenomena hiding behind the single word
"deletion", and this module separates them:

1. **Split relocation.** A test module was replaced by, or extracted into,
   new modules.  Authorized only by an explicit record here.
2. **Partial extraction.** Some node IDs left a module that still exists
   (``test_browser_daemon.py``).  Same record type, ``disposition:
   "extracted"``.
3. **Line drift.** A fixture or marker moved from line 71 to line 81 of an
   *unchanged* file because something above it grew.  Fixture and marker rows
   are keyed on ``line``, so this reads as a delete plus an add.  It is not a
   deletion at all, needs no record, and is recognised structurally — a
   governance record for every edit above a fixture would be noise, and noise
   is what stops baselines from ever being regenerated.

Fail-closed obligations, every one required, no wildcards:

1. exact field set; every scalar non-empty; ``owner_package`` well-formed;
2. ``accepting_commit`` is a full SHA that resolves in this repository, and
   ``accepting_receipt`` is named;
3. ``disposition`` is ``replaced`` (old path must be gone) or ``extracted``
   (old path must still exist) — the record states which, and is checked;
4. every ``new_path`` exists in the current inventory; the list is sorted,
   unique and non-empty; ``old_path`` is never among them;
5. **no test is lost and none is invented**: every relocated identity keeps
   its exact test name, and each must reappear under one of this record's
   ``new_paths``.  A rename is not a relocation;
6. the observed relocation counts equal the pinned counts **exactly**.
   Pinning makes a record a ratchet rather than a waiver — a split cannot
   quietly grow later, exactly as ``observed_at_adjudication`` does for the
   width registry;
7. :func:`check_split_delta` rejects stale and extra records at regeneration.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

SPLIT_FIELDS = {
    "old_path",
    "disposition",
    "new_paths",
    "relocated_static_id_count",
    "relocated_collected_id_count",
    "relocated_fixture_count",
    "relocated_marker_count",
    "owner_package",
    "accepting_commit",
    "accepting_receipt",
}
_DISPOSITIONS = {"replaced", "extracted"}
_PACKAGE = re.compile(r"PKG-\d{2}-[A-Z0-9-]+")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_COUNT_FIELDS = (
    "relocated_static_id_count",
    "relocated_collected_id_count",
    "relocated_fixture_count",
    "relocated_marker_count",
)
_FIXTURE_IDENTITY = ("fixture", "scope")
_MARKER_IDENTITY = ("framework", "marker", "source")


def _commit_resolves(root: Path, value: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{value}^{{commit}}"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _valid_scalars(row: dict[str, Any]) -> bool:
    """Obligation 1: every scalar present, non-empty and well-formed."""
    scalars = ("old_path", "disposition", "owner_package", "accepting_commit",
               "accepting_receipt")
    if not all(isinstance(row.get(key), str) and row[key] for key in scalars):
        return False
    return (
        row["disposition"] in _DISPOSITIONS
        and bool(_PACKAGE.fullmatch(row["owner_package"]))
        and bool(_GIT_COMMIT.fullmatch(row["accepting_commit"]))
    )


def _valid_counts(row: dict[str, Any]) -> bool:
    """Obligation 6: four non-negative pins, at least one of them load-bearing."""
    if not all(
        isinstance(row.get(key), int)
        and not isinstance(row[key], bool)
        and row[key] >= 0
        for key in _COUNT_FIELDS
    ):
        return False
    return any(row[key] for key in _COUNT_FIELDS)


def _valid_new_paths(row: dict[str, Any]) -> bool:
    """Obligation 4: a sorted, unique, non-empty destination list."""
    paths = row.get("new_paths")
    if not isinstance(paths, list) or not all(
        isinstance(item, str) and item for item in paths
    ):
        return False
    if not paths or paths != sorted(paths) or len(paths) != len(set(paths)):
        return False
    return row.get("old_path") not in paths


def _valid_split_row(row: Any, root: Path) -> bool:
    """Obligations 1, 2, 4 and 6 — decomposed so each stays readable."""
    if not isinstance(row, dict) or set(row) != SPLIT_FIELDS:
        return False
    if not (_valid_scalars(row) and _valid_counts(row) and _valid_new_paths(row)):
        return False
    return _commit_resolves(root, row["accepting_commit"])


def split_authority(
    baseline: dict[str, Any], root: Path, problems: list[str],
) -> dict[str, dict[str, Any]]:
    """Return the validated ``module_split_transitions`` map by old path."""
    rows = baseline.get("module_split_transitions", [])
    if not isinstance(rows, list) or not all(
        _valid_split_row(row, root) for row in rows
    ):
        problems.append("module_split_transitions has invalid explicit metadata")
        return {}
    ordered = sorted(rows, key=lambda item: item["old_path"])
    if rows != ordered:
        problems.append("module_split_transitions must be sorted by old_path")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["old_path"] in result:
            problems.append(f"duplicate module split target: {row['old_path']}")
            continue
        result[row["old_path"]] = row
    return result


def _identity(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row.get(name) for name in fields)


def _same_path_rows(rows: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("path") == path]


def line_drift_pairs(
    previous: list[dict[str, Any]], current: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return previous rows that merely changed ``line`` within the same path.

    Phenomenon 3. A row is line drift only when an identical identity exists
    at the *same* path in the current scan and is not already matched by an
    identical row. Nothing about the row other than its line may differ.
    """
    available: dict[tuple[Any, ...], int] = {}
    current_keys = {
        tuple(sorted(row.items())) for row in current
    }
    for row in current:
        key = (row.get("path"), *_identity(row, fields))
        available[key] = available.get(key, 0) + 1
    drifted: list[dict[str, Any]] = []
    for row in previous:
        if tuple(sorted(row.items())) in current_keys:
            continue
        key = (row.get("path"), *_identity(row, fields))
        if available.get(key, 0) > 0:
            available[key] -= 1
            drifted.append(row)
    return drifted


def _relocated_rows(
    deleted: list[dict[str, Any]], current: list[dict[str, Any]],
    record: dict[str, Any], fields: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``deleted`` into rows relocated under ``record`` and the rest."""
    available: dict[tuple[Any, ...], int] = {}
    for path in record["new_paths"]:
        for row in _same_path_rows(current, path):
            key = _identity(row, fields)
            available[key] = available.get(key, 0) + 1
    relocated: list[dict[str, Any]] = []
    unexplained: list[dict[str, Any]] = []
    for row in deleted:
        key = _identity(row, fields)
        if available.get(key, 0) > 0:
            available[key] -= 1
            relocated.append(row)
        else:
            unexplained.append(row)
    return relocated, unexplained


def _relocated_ids(
    deleted: list[str], current: list[str], record: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Obligation 5 for node IDs: the exact test name must survive."""
    by_name: dict[str, int] = {}
    prefixes = tuple(f"{path}::" for path in record["new_paths"])
    for node_id in current:
        if node_id.startswith(prefixes):
            name = node_id.partition("::")[2]
            by_name[name] = by_name.get(name, 0) + 1
    relocated: list[str] = []
    unexplained: list[str] = []
    for node_id in deleted:
        name = node_id.partition("::")[2]
        if by_name.get(name, 0) > 0:
            by_name[name] -= 1
            relocated.append(node_id)
        else:
            unexplained.append(node_id)
    return relocated, unexplained


class SplitLedger:
    """Accumulates observed relocations so pinned counts can be verified."""

    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self._records = records
        self._observed: dict[tuple[str, str], int] = {}

    def record_for(self, path: str) -> dict[str, Any] | None:
        return self._records.get(path)

    def count(self, old_path: str, field: str, amount: int) -> None:
        key = (old_path, field)
        self._observed[key] = self._observed.get(key, 0) + amount

    def authorized_ids(self, deleted: list[str], current: list[str], field: str
                       ) -> tuple[set[str], list[str]]:
        """Return relocated IDs and the deletions no record explains."""
        authorized: set[str] = set()
        unexplained: list[str] = []
        by_path: dict[str, list[str]] = {}
        for node_id in deleted:
            by_path.setdefault(node_id.partition("::")[0], []).append(node_id)
        for old_path, ids in by_path.items():
            record = self._records.get(old_path)
            if record is None:
                unexplained.extend(ids)
                continue
            relocated, missing = _relocated_ids(ids, current, record)
            authorized.update(relocated)
            unexplained.extend(missing)
            self.count(old_path, field, len(relocated))
        return authorized, unexplained

    def authorized_rows(
        self, deleted: list[dict[str, Any]], current: list[dict[str, Any]],
        fields: tuple[str, ...], field: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return relocated rows and the deletions no record explains."""
        authorized: list[dict[str, Any]] = []
        unexplained: list[dict[str, Any]] = []
        by_path: dict[str, list[dict[str, Any]]] = {}
        for row in deleted:
            by_path.setdefault(str(row.get("path")), []).append(row)
        for old_path, rows in by_path.items():
            record = self._records.get(old_path)
            if record is None:
                unexplained.extend(rows)
                continue
            relocated, missing = _relocated_rows(rows, current, record, fields)
            authorized.extend(relocated)
            unexplained.extend(missing)
            self.count(old_path, field, len(relocated))
        return authorized, unexplained

    def count_problems(self) -> list[str]:
        """Obligation 6: observed relocations must equal the pinned counts.

        A record is *absorbed* once its transition has been baselined: the
        deletions it explained are no longer visible, so it observes zero on
        every later regeneration. Zero is therefore permitted, and any other
        value must equal the pin exactly — the record can never authorize a
        different number of relocations than the one it was accepted for.
        """
        problems: list[str] = []
        for old_path, record in sorted(self._records.items()):
            for field in _COUNT_FIELDS:
                observed = self._observed.get((old_path, field), 0)
                if observed not in (0, record[field]):
                    problems.append(
                        f"module split {old_path}: {field} pinned "
                        f"{record[field]}, observed {observed}"
                    )
        return problems


def path_disposition_problems(
    records: dict[str, dict[str, Any]], current_files: list[str],
) -> list[str]:
    """Obligations 3 and 4 against the live file inventory."""
    problems: list[str] = []
    present = set(current_files)
    for old_path, record in sorted(records.items()):
        gone = old_path not in present
        if record["disposition"] == "replaced" and not gone:
            problems.append(
                f"module split {old_path}: disposition 'replaced' but the "
                "path still exists"
            )
        if record["disposition"] == "extracted" and gone:
            problems.append(
                f"module split {old_path}: disposition 'extracted' but the "
                "path no longer exists"
            )
        missing = sorted(set(record["new_paths"]) - present)
        if missing:
            problems.append(
                f"module split {old_path}: new_paths absent from the "
                f"inventory: {missing}"
            )
    return problems


FIXTURE_IDENTITY = _FIXTURE_IDENTITY
MARKER_IDENTITY = _MARKER_IDENTITY


def _canonical(row: Any) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _deleted_strings(previous: list[str], current: list[str]) -> list[str]:
    live = set(current)
    return [item for item in previous if item not in live]


def _deleted_rows(
    previous: list[dict[str, Any]], current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    live = {_canonical(row) for row in current}
    return [row for row in previous if _canonical(row) not in live]


def _authorize_row_field(
    ledger: SplitLedger, previous: list[dict[str, Any]],
    current: list[dict[str, Any]], fields: tuple[str, ...], count_field: str,
) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (authorized canonical rows, relocated rows, drifted rows)."""
    deleted = _deleted_rows(previous, current)
    drifted = line_drift_pairs(deleted, current, fields)
    drifted_keys = {_canonical(row) for row in drifted}
    remaining = [row for row in deleted if _canonical(row) not in drifted_keys]
    relocated, unexplained = ledger.authorized_rows(
        remaining, current, fields, count_field
    )
    authorized = {_canonical(row) for row in relocated} | drifted_keys
    return authorized, relocated, drifted


def build_authorizations(
    records: dict[str, dict[str, Any]],
    previous_mapping: dict[str, Any], current_mapping: dict[str, Any],
    previous_roots: dict[str, list[str]], current_roots: dict[str, list[str]],
) -> tuple[dict[str, set[str]], list[dict[str, Any]], SplitLedger]:
    """Return per-label authorized deletions and the accounted marker rows.

    Nothing is authorized by default: a label absent from the result set
    keeps ``_assert_no_deletions``'s absolute refusal.
    """
    ledger = SplitLedger(records)
    authorized: dict[str, set[str]] = {}

    for name, previous in previous_roots.items():
        current = current_roots[name]
        relocated, _ = ledger.authorized_ids(
            _deleted_strings(previous, current), current,
            "relocated_collected_id_count",
        )
        authorized[f"collected.{name}"] = {_canonical(item) for item in relocated}

    replaced = {
        old_path
        for old_path, record in records.items()
        if record["disposition"] == "replaced"
    }
    authorized["mapping_static.python_test_files"] = {
        _canonical(path)
        for path in _deleted_strings(
            previous_mapping["python_test_files"],
            current_mapping["python_test_files"],
        )
        if path in replaced
    }

    relocated_ids, _ = ledger.authorized_ids(
        _deleted_strings(
            previous_mapping["python_static_test_ids"],
            current_mapping["python_static_test_ids"],
        ),
        current_mapping["python_static_test_ids"],
        "relocated_static_id_count",
    )
    authorized["mapping_static.python_static_test_ids"] = {
        _canonical(item) for item in relocated_ids
    }

    fixture_rows, _, _ = _authorize_row_field(
        ledger, previous_mapping["fixtures"], current_mapping["fixtures"],
        _FIXTURE_IDENTITY, "relocated_fixture_count",
    )
    authorized["mapping_static.fixtures"] = fixture_rows

    marker_rows, marker_relocated, marker_drifted = _authorize_row_field(
        ledger, previous_mapping["markers"], current_mapping["markers"],
        _MARKER_IDENTITY, "relocated_marker_count",
    )
    authorized["mapping_static.markers"] = marker_rows
    return authorized, marker_relocated + marker_drifted, ledger


def unaccounted_marker_growth(
    additions: list[dict[str, Any]], accounted: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return marker additions that no relocation or line drift explains.

    Marker growth stays forbidden. A marker that merely reappeared at its new
    home — or at a new line in the same file — is not growth, and is matched
    off against the deletion that explains it, one for one.
    """
    budget: dict[tuple[Any, ...], int] = {}
    for row in accounted:
        key = _identity(row, _MARKER_IDENTITY)
        budget[key] = budget.get(key, 0) + 1
    growth: list[dict[str, Any]] = []
    for row in additions:
        key = _identity(row, _MARKER_IDENTITY)
        if budget.get(key, 0) > 0:
            budget[key] -= 1
            continue
        growth.append(row)
    return growth
