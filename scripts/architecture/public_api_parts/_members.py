"""Member-level public-API transition authority (Epic 10-D).

``_check_python_change`` is the sole gate on a changed public identity, and a
**member-level** change fails it twice over by construction: it rejects any
``public_signature`` change outright, and ``_valid_bridge`` hard-requires
``old_origin != new_origin``.  A class that loses a method while staying in
the same module therefore cannot be expressed by any bridge — which is why
``check_public_api`` was red for three epics on two accepted changes
(``HttpVerifyClient`` losing ``authenticated_request``; ``DefaultToolExecutor``
losing three methods migrated to ``ToolCatalog``).

This is the third authority beside ``additive_transitions`` and
``compatibility_bridges``.  It is consulted *instead of* the bridge path when
a record exists for the identity, and it is fail-closed on every prong:

1. exact field set, every value non-empty, ``owner_package`` well-formed, and
   ``surface`` is ``python``.  A frontend change is never expressible here —
   prongs 3 and 4 have no frontend input — and is authorized instead by
   :mod:`._frontend`, the fourth authority Epic 12-A added;
2. ``old_signature_sha256``/``new_signature_sha256`` pin the **stored** and
   **live** signature bytes, so a record authorizes exactly one transition;
3. ``old_origin == new_origin == record["origin"]`` — sameness is asserted,
   not merely permitted; an origin *change* remains a bridge's job;
4. the member delta computed from the two signatures equals
   ``removed_members``/``added_members`` exactly — no over-authorization.
   Full member strings are stored rather than bare names, so a record cannot
   silently absorb a signature change to a *surviving* member;
5. :func:`check_member_delta` mirrors ``_check_bridge_delta``, so a stale or
   extra record fails regeneration;
6. ``accepting_commit`` must be a full SHA that resolves in this repository.

No wildcards, no defaults, no partial records.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from ._constants import GIT_SHA, MEMBER_FIELDS, PACKAGE, TargetIdentity, TargetKey
from ._surface import canonical, target_origin

MemberKey = tuple[str, str]


def signature_sha256(signature: Any) -> str:
    """Return the digest of one canonicalised public signature."""
    return hashlib.sha256(canonical(signature).encode("utf-8")).hexdigest()


def _members_of(signature: Any) -> list[str] | None:
    if not isinstance(signature, dict):
        return None
    members = signature.get("members")
    if not isinstance(members, list) or not all(
        isinstance(item, str) for item in members
    ):
        return None
    return members


def commit_resolves(root: Path, value: str) -> bool:
    """Report whether ``value`` names a commit object in this repository.

    Shared with :mod:`._frontend`, whose prong 1 asserts the same thing about a
    frontend record's ``accepting_commit``.  Copying it would have made a third
    occurrence in the tree (``test_inventory_parts/_splits.py`` holds the
    second), which standing §6 treats as the threshold for a general mechanism
    rather than another clone.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{value}^{{commit}}"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _string_list(row: dict[str, Any], key: str) -> list[str] | None:
    value = row.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        return None
    if value != sorted(value) or len(value) != len(set(value)):
        return None
    return value


def _valid_member_row(row: Any, root: Path) -> bool:
    """Prong 1 and prong 6: schema, package form, and commit resolvability."""
    if not isinstance(row, dict) or set(row) != MEMBER_FIELDS:
        return False
    scalars = (
        "surface", "path", "public_name", "origin", "old_signature_sha256",
        "new_signature_sha256", "owner_package", "accepting_commit",
        "accepting_receipt",
    )
    if not all(isinstance(row[key], str) and row[key] for key in scalars if key != "origin"):
        return False
    if row["origin"] is not None and (not isinstance(row["origin"], str) or not row["origin"]):
        return False
    removed = _string_list(row, "removed_members")
    added = _string_list(row, "added_members")
    if removed is None or added is None or not (removed or added):
        return False
    if row["surface"] != "python":
        return False
    if not PACKAGE.fullmatch(row["owner_package"]):
        return False
    if not GIT_SHA.fullmatch(row["accepting_commit"]):
        return False
    return commit_resolves(root, row["accepting_commit"])


def member_authority(
    baseline: dict[str, Any], root: Path, problems: list[str],
) -> dict[MemberKey, dict[str, Any]]:
    """Return the validated ``member_transitions`` map, or record problems."""
    rows = baseline.get("member_transitions", [])
    if not isinstance(rows, list) or not all(
        _valid_member_row(row, root) for row in rows
    ):
        problems.append("member_transitions has invalid explicit metadata")
        return {}
    if rows != sorted(rows, key=canonical):
        problems.append("member_transitions must be canonically sorted")
    result: dict[MemberKey, dict[str, Any]] = {}
    for row in rows:
        key = (row["path"], row["public_name"])
        if key in result:
            problems.append(f"duplicate member transition target: {key}")
            continue
        result[key] = row
    return result


def _check_signature_pins(
    identity: TargetIdentity, record: dict[str, Any],
    old_target: dict[str, Any], new_target: dict[str, Any], problems: list[str],
) -> None:
    """Prong 2: the record pins the exact stored and live signature bytes."""
    old_signature = old_target.get("public_signature")
    new_signature = new_target.get("public_signature")
    old_sha = signature_sha256(old_signature)
    new_sha = signature_sha256(new_signature)
    if record["old_signature_sha256"] != old_sha:
        problems.append(
            "member transition old_signature_sha256 does not pin the accepted "
            f"signature: {identity}; actual={old_sha}"
        )
    if record["new_signature_sha256"] != new_sha:
        problems.append(
            "member transition new_signature_sha256 does not pin the live "
            f"signature: {identity}; actual={new_sha}"
        )
    if old_sha == new_sha:
        problems.append(
            f"member transition records an identical signature: {identity}"
        )


def _check_origin_sameness(
    identity: TargetIdentity, record: dict[str, Any],
    old_target: dict[str, Any], new_target: dict[str, Any], problems: list[str],
) -> None:
    """Prong 3: both origins exist, are equal, and equal the recorded origin."""
    old_origin = target_origin(old_target)
    new_origin = target_origin(new_target)
    if old_origin != new_origin or record["origin"] != old_origin:
        problems.append(
            "member transition requires one unchanged origin equal to the "
            f"record: {identity}; old={old_origin}; new={new_origin}; "
            f"record={record['origin']}"
        )


def _check_member_delta(
    identity: TargetIdentity, record: dict[str, Any],
    old_target: dict[str, Any], new_target: dict[str, Any], problems: list[str],
) -> None:
    """Prong 4: the computed delta equals the recorded delta exactly."""
    old_members = _members_of(old_target.get("public_signature"))
    new_members = _members_of(new_target.get("public_signature"))
    if old_members is None or new_members is None:
        problems.append(
            f"member transition target has no member list: {identity}"
        )
        return
    removed = sorted(set(old_members) - set(new_members))
    added = sorted(set(new_members) - set(old_members))
    if record["removed_members"] != removed or record["added_members"] != added:
        problems.append(
            "member transition delta does not match the surfaces: "
            f"{identity}; removed={removed}; added={added}"
        )


def check_member_change(
    identity: TargetIdentity, old_keys: list[TargetKey], new_keys: list[TargetKey],
    previous_targets: dict[TargetKey, dict[str, Any]],
    current_targets: dict[TargetKey, dict[str, Any]],
    record: dict[str, Any], problems: list[str],
) -> None:
    """Authorize one member-level change, or explain exactly why not."""
    if len(old_keys) != 1 or len(new_keys) != 1:
        problems.append(
            f"member transition requires one accepted and one live target: {identity}"
        )
        return
    old_target = previous_targets[old_keys[0]]
    new_target = current_targets[new_keys[0]]
    _check_signature_pins(identity, record, old_target, new_target, problems)
    _check_origin_sameness(identity, record, old_target, new_target, problems)
    _check_member_delta(identity, record, old_target, new_target, problems)


def check_member_delta(
    previous: dict[str, Any], inventory: dict[str, Any], root: Path,
    current_identities: set[TargetIdentity], changed_members: set[TargetIdentity],
    problems: list[str],
) -> None:
    """Prong 5: mirror ``_check_bridge_delta`` so stale/extra records fail."""
    previous_rows = member_authority(previous, root, problems)
    current_rows = member_authority(inventory, root, problems)
    preserved = {
        ("python", path, name)
        for path, name in previous_rows
        if ("python", path, name) in current_identities
        and ("python", path, name) not in changed_members
    }
    expected = preserved | changed_members
    current = {("python", path, name) for path, name in current_rows}
    if current != expected:
        problems.append(
            "member transitions do not exactly authorize regeneration: "
            f"missing={sorted(expected - current)}, "
            f"extra={sorted(current - expected)}"
        )
    for _, path, name in preserved:
        key = (path, name)
        if current_rows.get(key) != previous_rows[key]:
            problems.append(f"accepted member transition changed: {key}")


def check_authority_disjoint(
    members: dict[MemberKey, dict[str, Any]],
    bridges: dict[MemberKey, dict[str, Any]],
    problems: list[str],
) -> None:
    """One identity may not be claimed by both a bridge and a member record."""
    overlap = sorted(set(members) & set(bridges))
    if overlap:
        problems.append(
            f"member transition and compatibility bridge claim the same target: {overlap}"
        )


def describe(record: dict[str, Any]) -> str:
    """Return one stable human-readable line for receipts."""
    return json.dumps(
        {
            "path": record["path"],
            "public_name": record["public_name"],
            "removed": len(record["removed_members"]),
            "added": len(record["added_members"]),
            "owner_package": record["owner_package"],
            "accepting_commit": record["accepting_commit"],
        },
        sort_keys=True,
    )
