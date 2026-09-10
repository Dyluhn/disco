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

``retired_count`` and ``renamed_count`` join it on the same terms, one term
per authority that can make an identity disappear: ``before - relocated -
retired - renamed + added == after``.

None of the three can be inflated to hide a deletion: the deletion itself must
still pass ``_assert_no_deletions``, which requires a record — a module split,
the finite retirement closeout, or a :mod:`._renames` one-to-one rename —
whose pinned counts equal the observed count exactly.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

COLLECTED_TRANSITION_KEYS = {"before_count", "after_count", "added_ids"}
COLLECTED_TRANSITION_OPTIONAL = {"relocated_count", "retired_count", "renamed_count"}
MAPPING_ADDITION_KEYS = {
    "python_test_files",
    "python_static_test_ids",
    "typescript_test_files",
    "typescript_static_test_ids",
    "fixtures",
}
PYTHON_ROOTS = {"packages", "harness", "integrations", "tests"}
_FIXTURE_FIELDS = {"path", "line", "fixture", "scope"}


def row_key(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def string_additions(
    value: Any,
    label: str,
    *,
    unique: bool,
    problems: list[str],
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
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


def fixture_claim_key(row: dict[str, Any]) -> str:
    """Stable fixture ownership identity; source line is observational metadata."""
    return json.dumps(
        {key: row[key] for key in ("path", "fixture", "scope")},
        sort_keys=True,
        separators=(",", ":"),
    )


def fixture_additions(
    value: Any,
    label: str,
    problems: list[str],
) -> list[dict[str, Any]]:
    rows = row_additions(value, label, problems)
    valid: list[dict[str, Any]] = []
    for row in rows:
        strings_valid = all(
            isinstance(row.get(key), str) and bool(row[key]) for key in ("path", "fixture", "scope")
        )
        line = row.get("line")
        if (
            set(row) != _FIXTURE_FIELDS
            or not strings_valid
            or not isinstance(line, int)
            or isinstance(line, bool)
            or line < 1
        ):
            problems.append(f"{label} fixture row schema mismatch")
            continue
        valid.append(row)
    identities = [fixture_claim_key(row) for row in valid]
    if len(identities) != len(set(identities)):
        problems.append(f"{label} contains duplicate fixture ownership identities")
    return valid


def claim_disjoint(
    label: str,
    values: list[str],
    claims: dict[str, set[str]],
    problems: list[str],
) -> None:
    owned = claims.setdefault(label, set())
    overlap = sorted(owned.intersection(values))
    if overlap:
        problems.append(f"{label} additions overlap across packages: {overlap}")
    owned.update(values)


def check_subset(
    label: str,
    additions: list[str],
    current: list[str],
    problems: list[str],
) -> None:
    excess = Counter(additions) - Counter(current)
    if excess:
        problems.append(
            f"{label} additions are absent from the current inventory: {sorted(excess.elements())}"
        )


def _relocated_count(
    row: dict[str, Any], label: str, problems: list[str], key: str = "relocated_count"
) -> int:
    value = row.get(key, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        problems.append(f"{label}.{key} must be a non-negative integer")
        return 0
    return value


def check_collected_row(
    transition: dict[str, Any],
    baseline: dict[str, Any],
    name: str,
    row: Any,
    claims: dict[str, set[str]],
    problems: list[str],
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
    added = string_additions(row["added_ids"], f"{label}.added_ids", unique=True, problems=problems)
    relocated = _relocated_count(row, label, problems)
    retired = _relocated_count(row, label, problems, "retired_count")
    renamed = _relocated_count(row, label, problems, "renamed_count")
    before = row["before_count"]
    after = row["after_count"]
    valid_counts = all(
        isinstance(count, int) and not isinstance(count, bool) and count >= 0
        for count in (before, after)
    )
    if not valid_counts:
        problems.append(f"{label} counts must be non-negative integers")
    elif before - relocated - retired - renamed + len(added) != after:
        problems.append(
            f"{label} count/addition mismatch: {before} - {relocated} - {retired} - "
            f"{renamed} + {len(added)} != {after}"
        )
    current = baseline.get("collected", {}).get("roots", {}).get(name)
    if not isinstance(current, list):
        problems.append(f"{label} has no current root authority")
        current = []
    check_subset(f"{label}.added_ids", added, current, problems)
    if valid_counts and after > len(current):
        problems.append(f"{label}.after_count exceeds current count {len(current)}")
    is_current = transition.get("source_identity_after") == baseline.get("source_identity")
    if valid_counts and is_current and after != len(current):
        problems.append(f"{label}.after_count must equal current count {len(current)}")
    claim_disjoint(f"collected_roots.{name}", added, claims, problems)
    return len(added)


def check_collected_additions(
    transition: dict[str, Any],
    baseline: dict[str, Any],
    claims: dict[str, set[str]],
    problems: list[str],
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


def check_null_advance(
    transition: dict[str, Any],
    problems: list[str],
) -> None:
    """Validate a transition row that owns no addition at all.

    Every source-identity advance must be attributable to a named package, so
    the chain needs a link even when an epic changes no test.  Epic 11-A is the
    first to reach a seal in that state: a pure source decomposition with a
    byte-identical inventory.  Before this, the only rows that existed happened
    to add tests, so ``must own at least one exact addition`` was never wrong --
    it was merely never exercised by a null epic.

    A null row is therefore not permitted to be *empty*, which would assert
    nothing.  It must PIN every python root with an unchanged count.  That
    turns absence of evidence into evidence of absence, because
    :func:`check_collected_row` independently re-proves those same counts
    against live collection on every run -- the terminal row's ``after_count``
    must equal the current count exactly.  A null row consequently cannot hide
    a deletion (still refused outright by ``_assert_no_deletions``), cannot
    hide a relocation, and cannot survive the tree drifting underneath it.
    """
    owner = transition.get("package", "<invalid>")
    rows = transition.get("collected_roots")
    if not isinstance(rows, dict) or set(rows) != PYTHON_ROOTS:
        problems.append(
            f"{owner} owns no addition, so it must pin every collected root "
            f"as unchanged; got {sorted(rows) if isinstance(rows, dict) else rows!r}"
        )
        return
    for name in sorted(PYTHON_ROOTS):
        row = rows[name]
        label = f"{owner}.collected_roots.{name}"
        if not isinstance(row, dict):
            problems.append(f"{label} must be an exact object")
            continue
        if row.get("before_count") != row.get("after_count"):
            problems.append(f"{label} null advance must not change its collected count")
        if row.get("retired_count", 0):
            problems.append(f"{label} null advance must not retire a test")
        if row.get("relocated_count", 0):
            problems.append(f"{label} null advance must not relocate a test")
        if row.get("renamed_count", 0):
            problems.append(f"{label} null advance must not rename a test")


def check_mapping_additions(
    transition: dict[str, Any],
    baseline: dict[str, Any],
    claims: dict[str, set[str]],
    problems: list[str],
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
            rows = fixture_additions(additions[key], label, problems)
            values = [fixture_claim_key(row) for row in rows]
            authority = [
                fixture_claim_key(row)
                for row in current.get(key, [])
                if isinstance(row, dict) and set(row) == _FIXTURE_FIELDS
            ]
        else:
            unique = key.endswith("_files")
            values = string_additions(additions[key], label, unique=unique, problems=problems)
            authority = current.get(key, [])
            if not isinstance(authority, list):
                authority = []
        check_subset(label, values, authority, problems)
        claim_disjoint(f"mapping_static.{key}", values, claims, problems)
        total += len(values)
    return total
