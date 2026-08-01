"""Additive-transition row checks for the test inventory.

Relocated from ``inventory_static`` in Epic 10-D, which was at 695 of its 700
logical-line cap — a third authority out of budget that the 10-C handover did
not name. Behaviour is unchanged apart from one correction, described below.

**The relocation term.** ``check_collected_row`` enforced
``before_count + len(added_ids) == after_count``. That identity is only true
while deletions are impossible, which they were: every deletion was refused
outright. Now that :mod:`._splits` can authorize a relocation, the honest
identity is ``before - relocated + added == after``. The row states its own
``relocated_count`` so the arithmetic remains checkable from the row alone;
the field is optional and defaults to zero, so the frozen PKG-02-GATE row is
untouched and every pre-existing row keeps the original strict relation.

A relocation count cannot be inflated to hide a deletion: the deletion itself
must still pass ``_assert_no_deletions``, which requires a module-split record
whose pinned per-path counts equal the observed relocations exactly.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

COLLECTED_TRANSITION_KEYS = {"before_count", "after_count", "added_ids"}
COLLECTED_TRANSITION_OPTIONAL = {"relocated_count"}
MAPPING_ADDITION_KEYS = {
    "python_test_files",
    "python_static_test_ids",
    "typescript_test_files",
    "typescript_static_test_ids",
    "fixtures",
}
PYTHON_ROOTS = {"packages", "harness", "integrations", "tests"}


def row_key(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def string_additions(
    value: Any, label: str, *, unique: bool, problems: list[str],
) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        problems.append(f"{label} must be an exact string list")
        return []
    if value != sorted(value):
        problems.append(f"{label} must be sorted")
    if unique and len(value) != len(set(value)):
        problems.append(f"{label} must be unique")
    return value


def row_additions(value: Any, label: str, problems: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        problems.append(f"{label} must be an exact object list")
        return []
    keys = [row_key(item) for item in value]
    if keys != sorted(keys):
        problems.append(f"{label} must be canonically sorted")
    if len(keys) != len(set(keys)):
        problems.append(f"{label} must be unique")
    return value


def claim_disjoint(
    label: str, values: list[str], claims: dict[str, set[str]], problems: list[str],
) -> None:
    owned = claims.setdefault(label, set())
    overlap = sorted(owned.intersection(values))
    if overlap:
        problems.append(f"{label} additions overlap across packages: {overlap}")
    owned.update(values)


def check_subset(
    label: str, additions: list[str], current: list[str], problems: list[str],
) -> None:
    excess = Counter(additions) - Counter(current)
    if excess:
        problems.append(
            f"{label} additions are absent from the current inventory: "
            f"{sorted(excess.elements())}"
        )


def _relocated_count(row: dict[str, Any], label: str, problems: list[str]) -> int:
    value = row.get("relocated_count", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        problems.append(f"{label}.relocated_count must be a non-negative integer")
        return 0
    return value


def check_collected_row(
    transition: dict[str, Any], baseline: dict[str, Any], name: str, row: Any,
    claims: dict[str, set[str]], problems: list[str],
) -> int:
    owner = transition.get("package", "<invalid>")
    label = f"{owner}.collected_roots.{name}"
    if not isinstance(row, dict) or not (
        COLLECTED_TRANSITION_KEYS
        <= set(row)
        <= COLLECTED_TRANSITION_KEYS | COLLECTED_TRANSITION_OPTIONAL
    ):
        problems.append(f"{label} schema mismatch")
        return 0
    added = string_additions(
        row["added_ids"], f"{label}.added_ids", unique=True, problems=problems
    )
    relocated = _relocated_count(row, label, problems)
    before = row["before_count"]
    after = row["after_count"]
    valid_counts = all(
        isinstance(count, int) and not isinstance(count, bool) and count >= 0
        for count in (before, after)
    )
    if not valid_counts:
        problems.append(f"{label} counts must be non-negative integers")
    elif before - relocated + len(added) != after:
        problems.append(
            f"{label} count/addition mismatch: {before} - {relocated} + "
            f"{len(added)} != {after}"
        )
    current = baseline.get("collected", {}).get("roots", {}).get(name)
    if not isinstance(current, list):
        problems.append(f"{label} has no current root authority")
        current = []
    check_subset(f"{label}.added_ids", added, current, problems)
    if valid_counts and after > len(current):
        problems.append(f"{label}.after_count exceeds current count {len(current)}")
    is_current = transition.get("source_identity_after") == baseline.get(
        "source_identity"
    )
    if valid_counts and is_current and after != len(current):
        problems.append(f"{label}.after_count must equal current count {len(current)}")
    claim_disjoint(f"collected_roots.{name}", added, claims, problems)
    return len(added)


def check_collected_additions(
    transition: dict[str, Any], baseline: dict[str, Any],
    claims: dict[str, set[str]], problems: list[str],
) -> int:
    owner = transition.get("package", "<invalid>")
    rows = transition.get("collected_roots")
    if not isinstance(rows, dict) or not all(isinstance(name, str) for name in rows):
        problems.append(f"{owner} collected_roots must be an exact object")
        return 0
    unknown = sorted(set(rows) - PYTHON_ROOTS)
    if unknown:
        problems.append(f"{owner} collected_roots has unknown roots: {unknown}")
    expected_root = next(iter(rows)) if len(rows) == 1 else "multiple"
    if transition.get("root") != expected_root:
        problems.append(
            f"{owner} root summary must be {expected_root!r}, got {transition.get('root')!r}"
        )
    return sum(
        check_collected_row(transition, baseline, name, row, claims, problems)
        for name, row in rows.items()
    )


def check_mapping_additions(
    transition: dict[str, Any], baseline: dict[str, Any],
    claims: dict[str, set[str]], problems: list[str],
) -> int:
    owner = transition.get("package", "<invalid>")
    additions = transition.get("mapping_static_additions")
    if not isinstance(additions, dict) or set(additions) != MAPPING_ADDITION_KEYS:
        problems.append(f"{owner} mapping_static_additions schema mismatch")
        return 0
    current = baseline.get("mapping_static")
    if not isinstance(current, dict):
        current = {}
    total = 0
    for key in sorted(MAPPING_ADDITION_KEYS):
        label = f"{owner}.mapping_static_additions.{key}"
        if key == "fixtures":
            rows = row_additions(additions[key], label, problems)
            values = [row_key(row) for row in rows]
            authority = [row_key(row) for row in current.get(key, []) if isinstance(row, dict)]
        else:
            unique = key.endswith("_files")
            values = string_additions(
                additions[key], label, unique=unique, problems=problems
            )
            authority = current.get(key, [])
            if not isinstance(authority, list):
                authority = []
        check_subset(label, values, authority, problems)
        claim_disjoint(f"mapping_static.{key}", values, claims, problems)
        total += len(values)
    return total
