"""Accepted-authority metadata and regeneration-delta checks.

Relocated from ``public_api`` in Epic 10-D and extended with the member-level
transition authority (:mod:`._members`).  ``root`` is threaded through because
the member authority must prove its ``accepting_commit`` resolves in this
repository; nothing else about these checks changed.

Epic 12-A added the frontend declaration authority (:mod:`._frontend`).  Before
it, ``_check_changed_identities`` rejected every changed frontend identity
unconditionally, so a signature change to an already-public frontend
declaration could not be authorized by any record type.  The two surfaces now
have symmetric treatment: a changed identity is authorized by the record that
claims it, and rejected with a surface-specific message when none does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import _closeout, _frontend, _members
from ._constants import (
    BRIDGE_FIELDS,
    PACKAGE,
    SHA256,
    SURFACES,
    TRANSITION_FIELDS,
    TargetIdentity,
    TargetKey,
)
from ._surface import canonical, surface_targets, target_key, target_origin


def valid_bridge(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == BRIDGE_FIELDS
        and all(isinstance(row[key], str) and row[key] for key in BRIDGE_FIELDS)
        and bool(PACKAGE.fullmatch(row["owner_package"]))
        and row["removal_package"] == "PKG-13-FACADES"
        and row["old_origin"] != row["new_origin"]
    )


def transition_authority(
    baseline: dict[str, Any],
    problems: list[str],
) -> dict[TargetKey, dict[str, Any]]:
    rows = baseline.get("additive_transitions")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        problems.append("additive_transitions must be an exact object list")
        return {}
    if rows != sorted(rows, key=canonical):
        problems.append("additive_transitions must be canonically sorted")
    result: dict[TargetKey, dict[str, Any]] = {}
    for row in rows:
        if (
            set(row) != TRANSITION_FIELDS
            or row.get("surface") not in SURFACES
            or any(not isinstance(row.get(key), str) or not row[key] for key in TRANSITION_FIELDS)
            or not SHA256.fullmatch(row["target_sha256"])
            or not PACKAGE.fullmatch(row["owner_package"])
        ):
            problems.append(f"invalid additive transition metadata: {row}")
            continue
        key = target_key(row)
        if key in result:
            problems.append(f"duplicate additive transition target: {key}")
            continue
        result[key] = row
    return result


def bridge_authority(
    baseline: dict[str, Any],
    problems: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = baseline.get("compatibility_bridges")
    if not isinstance(rows, list) or not all(valid_bridge(row) for row in rows):
        problems.append("compatibility_bridges has invalid explicit metadata")
        return {}
    if rows != sorted(rows, key=canonical):
        problems.append("compatibility_bridges must be canonically sorted")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["path"], row["public_name"])
        if key in result:
            problems.append(f"duplicate compatibility bridge target: {key}")
            continue
        result[key] = row
    return result


def _check_one_bridge(
    bridge: dict[str, Any],
    identity: TargetIdentity,
    targets: dict[TargetKey, dict[str, Any]],
    transitions: dict[TargetKey, dict[str, Any]],
    problems: list[str],
) -> None:
    keys = [key for key in targets if key[:3] == identity]
    if len(keys) != 1:
        problems.append(f"compatibility bridge no longer preserves public name: {[bridge]}")
        return
    actual_origin = target_origin(targets[keys[0]])
    if actual_origin != bridge["new_origin"]:
        problems.append(
            "compatibility bridge new_origin does not match live origin: "
            f"{bridge}; actual={actual_origin}"
        )
    transition = transitions.get(keys[0])
    if transition is None or transition["owner_package"] != bridge["owner_package"]:
        problems.append(f"compatibility bridge requires a same-owner additive transition: {bridge}")


def _check_one_member(
    record: dict[str, Any],
    identity: TargetIdentity,
    targets: dict[TargetKey, dict[str, Any]],
    problems: list[str],
) -> None:
    """A member record must still name exactly one live public identity."""
    keys = [key for key in targets if key[:3] == identity]
    if len(keys) != 1:
        problems.append(
            "member transition no longer names one live public target: "
            f"{[_members.describe(record)]}"
        )
        return
    live_sha = _members.signature_sha256(targets[keys[0]].get("public_signature"))
    if record["new_signature_sha256"] != live_sha:
        problems.append(
            "member transition new_signature_sha256 does not match the live "
            f"signature: {_members.describe(record)}; actual={live_sha}"
        )


def check_metadata(
    baseline: dict[str, Any],
    python_surface: list[dict[str, Any]],
    frontend_surface: list[dict[str, Any]],
    root: Path,
    problems: list[str],
) -> None:
    try:
        targets = surface_targets(python_surface, frontend_surface)
    except ValueError as error:
        problems.append(str(error))
        return
    transitions = transition_authority(baseline, problems)
    bridges = bridge_authority(baseline, problems)
    members = _members.member_authority(baseline, root, problems)
    declarations = _frontend.declaration_authority(baseline, root, problems)
    _members.check_authority_disjoint(members, bridges, problems)
    _frontend.check_declaration_disjoint(declarations, bridges, members, problems)
    stale_transitions = sorted(set(transitions) - set(targets))
    if stale_transitions:
        problems.append(f"additive transition target is not public: {stale_transitions}")
    for (path, public_name), bridge in bridges.items():
        _check_one_bridge(bridge, ("python", path, public_name), targets, transitions, problems)
    for (path, public_name), record in members.items():
        _check_one_member(record, ("python", path, public_name), targets, problems)
    for (path, public_name), record in declarations.items():
        _frontend.check_one_declaration(record, ("frontend", path, public_name), targets, problems)


def _check_python_change(
    identity: TargetIdentity,
    added: set[TargetKey],
    removed: set[TargetKey],
    previous_targets: dict[TargetKey, dict[str, Any]],
    current_targets: dict[TargetKey, dict[str, Any]],
    current_bridges: dict[tuple[str, str], dict[str, Any]],
    current_members: dict[tuple[str, str], dict[str, Any]],
    problems: list[str],
) -> None:
    old_keys = [key for key in removed if key[:3] == identity]
    new_keys = [key for key in added if key[:3] == identity]
    record = current_members.get((identity[1], identity[2]))
    if record is not None:
        _members.check_member_change(
            identity,
            old_keys,
            new_keys,
            previous_targets,
            current_targets,
            record,
            problems,
        )
        return
    bridge = current_bridges.get((identity[1], identity[2]))
    if len(old_keys) != 1 or len(new_keys) != 1 or bridge is None:
        problems.append(f"Python incompatible change requires one bridge: {identity}")
        return
    old_target = previous_targets[old_keys[0]]
    new_target = current_targets[new_keys[0]]
    if old_target["public_signature"] != new_target["public_signature"]:
        problems.append(f"compatibility bridge cannot change signature: {identity}")
    old_origin = target_origin(old_target)
    new_origin = target_origin(new_target)
    if (
        old_origin is None
        or new_origin is None
        or old_origin == new_origin
        or bridge["old_origin"] != old_origin
        or bridge["new_origin"] != new_origin
    ):
        problems.append(
            "compatibility bridge origins do not match accepted/current "
            f"surfaces: {identity}; old={old_origin}; new={new_origin}"
        )


def _check_frontend_change(
    identity: TargetIdentity,
    added: set[TargetKey],
    removed: set[TargetKey],
    previous_targets: dict[TargetKey, dict[str, Any]],
    current_targets: dict[TargetKey, dict[str, Any]],
    current_declarations: dict[tuple[str, str], dict[str, Any]],
    problems: list[str],
) -> None:
    """Authorize a changed frontend declaration, or reject it as before."""
    record = current_declarations.get((identity[1], identity[2]))
    if record is None:
        problems.append(f"frontend incompatible change cannot be bridged: {identity}")
        return
    _frontend.check_declaration_change(
        identity,
        [key for key in removed if key[:3] == identity],
        [key for key in added if key[:3] == identity],
        previous_targets,
        current_targets,
        record,
        problems,
    )


def _check_changed_identities(
    changed: set[TargetIdentity],
    added: set[TargetKey],
    removed: set[TargetKey],
    previous_targets: dict[TargetKey, dict[str, Any]],
    current_targets: dict[TargetKey, dict[str, Any]],
    current_bridges: dict[tuple[str, str], dict[str, Any]],
    current_members: dict[tuple[str, str], dict[str, Any]],
    current_declarations: dict[tuple[str, str], dict[str, Any]],
    problems: list[str],
) -> None:
    for identity in sorted(changed):
        if identity[0] == "frontend":
            _check_frontend_change(
                identity,
                added,
                removed,
                previous_targets,
                current_targets,
                current_declarations,
                problems,
            )
        else:
            _check_python_change(
                identity,
                added,
                removed,
                previous_targets,
                current_targets,
                current_bridges,
                current_members,
                problems,
            )


def _check_transition_delta(
    previous: dict[str, Any],
    inventory: dict[str, Any],
    added: set[TargetKey],
    current_keys: set[TargetKey],
    problems: list[str],
) -> None:
    previous_rows = transition_authority(previous, problems)
    current_rows = transition_authority(inventory, problems)
    preserved = set(previous_rows) & current_keys
    expected = preserved | added
    if set(current_rows) != expected:
        problems.append(
            "additive transitions do not exactly authorize regeneration: "
            f"missing={sorted(expected - set(current_rows))}, "
            f"extra={sorted(set(current_rows) - expected)}"
        )
    for key in preserved:
        if current_rows.get(key) != previous_rows[key]:
            problems.append(f"accepted additive transition changed: {key}")


def _check_bridge_delta(
    previous: dict[str, Any],
    inventory: dict[str, Any],
    current_identities: set[TargetIdentity],
    changed_bridges: set[TargetIdentity],
    problems: list[str],
) -> None:
    previous_rows = bridge_authority(previous, problems)
    current_rows = bridge_authority(inventory, problems)
    preserved = {
        ("python", path, name)
        for path, name in previous_rows
        if ("python", path, name) in current_identities
        and ("python", path, name) not in changed_bridges
    }
    expected = preserved | changed_bridges
    current = {("python", path, name) for path, name in current_rows}
    if current != expected:
        problems.append(
            "compatibility bridges do not exactly authorize regeneration: "
            f"missing={sorted(expected - current)}, "
            f"extra={sorted(current - expected)}"
        )
    for _, path, name in preserved:
        key = (path, name)
        if current_rows.get(key) != previous_rows[key]:
            problems.append(f"accepted compatibility bridge changed: {key}")


def check_regeneration_delta(
    previous: dict[str, Any],
    inventory: dict[str, Any],
    root: Path,
    previous_targets: dict[TargetKey, dict[str, Any]],
    current_targets: dict[TargetKey, dict[str, Any]],
    problems: list[str],
) -> None:
    previous_keys = set(previous_targets)
    current_keys = set(current_targets)
    added = current_keys - previous_keys
    removed = previous_keys - current_keys
    current_identities = {key[:3] for key in current_keys}
    deleted = [key for key in removed if key[:3] not in current_identities]
    if deleted:
        try:
            _closeout.retired_targets(root, previous, deleted)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            problems.append(f"public API regeneration deletes targets: {sorted(deleted)}; {error}")
    changed = {key[:3] for key in removed if key[:3] in current_identities}
    changed_python = {item for item in changed if item[0] == "python"}
    changed_frontend = {item for item in changed if item[0] == "frontend"}
    current_bridges = bridge_authority(inventory, problems)
    current_members = _members.member_authority(inventory, root, problems)
    current_declarations = _frontend.declaration_authority(inventory, root, problems)
    # An identity is member-authorized only if a record claims it; everything
    # else still needs a bridge. The two authorities are proven disjoint in
    # check_metadata, so this partition cannot double-authorize.
    member_identities = {("python", path, name) for path, name in current_members}
    changed_members = changed_python & member_identities
    changed_bridges = changed_python - member_identities
    # The frontend surface has one authority rather than two, so the partition
    # is simply "claimed or not"; an unclaimed change keeps the original
    # unconditional rejection.
    declaration_identities = {("frontend", path, name) for path, name in current_declarations}
    changed_declarations = changed_frontend & declaration_identities
    _check_changed_identities(
        changed,
        added,
        removed,
        previous_targets,
        current_targets,
        current_bridges,
        current_members,
        current_declarations,
        problems,
    )
    _check_transition_delta(previous, inventory, added, current_keys, problems)
    _check_bridge_delta(
        previous,
        inventory,
        current_identities,
        changed_bridges,
        problems,
    )
    _members.check_member_delta(
        previous,
        inventory,
        root,
        current_identities,
        changed_members,
        problems,
    )
    _frontend.check_declaration_delta(
        previous,
        inventory,
        root,
        current_identities,
        changed_declarations,
        problems,
    )
