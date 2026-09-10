"""Frontend public targets that MOVED: the fifth public-API authority.

A public target's identity is ``(surface, path, public_name)``. Move a frontend
component into a new module and the identity is gone, so the immutability rule
reads a pure relocation as a deletion — the same "one phenomenon wearing
another's clothes" problem :mod:`architecture.test_inventory_parts._renames`
solves for test identities, one level up.

Python already has an answer. ``compatibility_bridges`` lets a moved
implementation "retain the old public name" because a Python package re-exports
from its ``__init__.py`` and the scanner counts that re-export as the name. The
frontend has no equivalent: ``ts_scan.mjs`` records ``export { X } from "./y"``
as a declaration literally named ``export``, so a TypeScript re-export cannot
keep ``X`` alive at its old path. Before this module the only thing that could
sanction the deletion was the PKG-35 research API closeout, which is finite,
single-use, and was spent by the V48 landing.

That made every frontend module split unlandable. V51 hit it twice: three
download components moved out of ``ActivityFeed.tsx`` to bring it back under its
line budget, and ``getFallbackWarning`` became ``getSetupNote`` in a module of
its own name when UI-2 turned a red warning into a neutral optional-feature
note. Neither lost a public capability; both read as deletions.

Fail-closed obligations, every one required, no wildcards:

1. exact field set, every value a non-empty string, ``surface`` is
   ``frontend``, ``owner_package`` well-formed, ``accepting_commit`` a full SHA
   that resolves in this repository, and ``accepting_receipt`` named;
2. something must actually move: the old and new identities differ;
3. **one-to-one, in both directions**: old identities unique, new identities
   unique, and no identity is both. Two targets collapsing onto one name is a
   deletion, not a relocation, and a chain would let a record be re-aimed;
4. **the destination must exist**: the new identity resolves to exactly one
   live public target, and ``declaration_kind`` equals that target's declaration
   kind — a component that reappears as a type alias is a different change;
5. **the origin must be gone**: the old identity must be absent from the live
   surface. A record cannot sanction a deletion that did not happen;
6. :func:`check_relocation_delta` rejects stale and extra records at
   regeneration, and an accepted record may not be edited afterwards;
7. :func:`check_one_relocation` re-proves 4 and 5 on every ``check_public_api``
   run, so a landed record cannot outlive the target it describes.

What this deliberately does NOT do: it says nothing about the declaration text.
A relocation that also changes the signature is two changes; the second one is
``frontend_declaration_transitions``' business, at the new identity, on its own
record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._constants import GIT_SHA, PACKAGE, RELOCATION_FIELDS, TargetIdentity, TargetKey
from ._members import commit_resolves
from ._surface import canonical


def _identity(row: dict[str, Any], side: str) -> TargetIdentity:
    return ("frontend", row[f"{side}_path"], row[f"{side}_public_name"])


def _valid_relocation_row(row: Any, root: Path) -> bool:
    """Obligations 1 and 2: schema, surface, package form, resolvable commit."""
    if not isinstance(row, dict) or set(row) != RELOCATION_FIELDS:
        return False
    if not all(isinstance(row[key], str) and row[key] for key in RELOCATION_FIELDS):
        return False
    if row["surface"] != "frontend":
        return False
    if not PACKAGE.fullmatch(row["owner_package"]):
        return False
    if _identity(row, "old") == _identity(row, "new"):
        return False
    if not GIT_SHA.fullmatch(row["accepting_commit"]):
        return False
    return commit_resolves(root, row["accepting_commit"])


def relocation_authority(
    baseline: dict[str, Any],
    root: Path,
    problems: list[str],
) -> dict[TargetIdentity, dict[str, Any]]:
    """Return the validated ``frontend_target_relocations`` map by old identity."""
    rows = baseline.get("frontend_target_relocations", [])
    if not isinstance(rows, list) or not all(_valid_relocation_row(row, root) for row in rows):
        problems.append("frontend_target_relocations has invalid explicit metadata")
        return {}
    if rows != sorted(rows, key=canonical):
        problems.append("frontend_target_relocations must be canonically sorted")
    result: dict[TargetIdentity, dict[str, Any]] = {}
    destinations: set[TargetIdentity] = set()
    for row in rows:
        old, new = _identity(row, "old"), _identity(row, "new")
        if old in result:
            problems.append(f"duplicate frontend target relocation origin: {old}")
            continue
        if new in destinations:
            problems.append(f"duplicate frontend target relocation destination: {new}")
            continue
        result[old] = row
        destinations.add(new)
    # Obligation 3: no identity may be both an origin and a destination.
    chained = sorted(set(result) & destinations)
    if chained:
        problems.append(f"frontend target relocation chains onto itself: {chained}")
    return result


def _declaration_kind(target: dict[str, Any]) -> str | None:
    declaration = target.get("declaration")
    if not isinstance(declaration, dict):
        return None
    kind = declaration.get("kind")
    return kind if isinstance(kind, str) and kind else None


def check_one_relocation(
    record: dict[str, Any],
    targets: dict[TargetKey, dict[str, Any]],
    problems: list[str],
) -> bool:
    """Obligations 4 and 5 against a live surface. True when both hold."""
    old, new = _identity(record, "old"), _identity(record, "new")
    if any(key[:3] == old for key in targets):
        problems.append(
            f"frontend target relocation origin is still public: {describe(record)}"
        )
        return False
    keys = [key for key in targets if key[:3] == new]
    if len(keys) != 1:
        problems.append(
            "frontend target relocation does not name one live public target: "
            f"{describe(record)}"
        )
        return False
    kind = _declaration_kind(targets[keys[0]])
    if kind != record["declaration_kind"]:
        problems.append(
            "frontend target relocation declaration_kind does not match the live "
            f"target: {describe(record)}; actual={kind}"
        )
        return False
    return True


def sanctioned_deletions(
    records: dict[TargetIdentity, dict[str, Any]],
    current_targets: dict[TargetKey, dict[str, Any]],
    problems: list[str],
) -> set[TargetIdentity]:
    """Return the old identities whose relocation the live surface proves."""
    return {
        old
        for old, record in sorted(records.items())
        if check_one_relocation(record, current_targets, problems)
    }


def check_relocation_delta(
    previous: dict[str, Any],
    inventory: dict[str, Any],
    root: Path,
    relocated_now: set[TargetIdentity],
    problems: list[str],
) -> None:
    """Obligation 6: exactly the accepted records plus this run's relocations."""
    previous_rows = relocation_authority(previous, root, problems)
    current_rows = relocation_authority(inventory, root, problems)
    expected = set(previous_rows) | relocated_now
    if set(current_rows) != expected:
        problems.append(
            "frontend target relocations do not exactly authorize regeneration: "
            f"missing={sorted(expected - set(current_rows))}, "
            f"extra={sorted(set(current_rows) - expected)}"
        )
    for old, row in sorted(previous_rows.items()):
        if current_rows.get(old) != row:
            problems.append(f"accepted frontend target relocation changed: {old}")


def describe(record: dict[str, Any]) -> str:
    """Return one stable human-readable line for receipts."""
    return json.dumps(
        {
            "old": f"{record['old_path']}::{record['old_public_name']}",
            "new": f"{record['new_path']}::{record['new_public_name']}",
            "declaration_kind": record["declaration_kind"],
            "owner_package": record["owner_package"],
            "accepting_commit": record["accepting_commit"],
        },
        sort_keys=True,
    )
