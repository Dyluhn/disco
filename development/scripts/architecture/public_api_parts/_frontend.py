"""Frontend declaration-level public-API transition authority (Epic 12-A).

``_check_changed_identities`` appended *"frontend incompatible change cannot be
bridged"* unconditionally for any frontend identity whose descriptor digest
moved, so no record type could authorize a signature change to an already-public
frontend declaration.  Binding Amendment A3 requires exactly two of them —
``AgentEvent`` gains three union members and ``WSServerFrame`` splits into a
wire union and a client-synthesized union — and no formulation of A3 avoids
them, because a type outside the ``AgentEvent`` union cannot be narrowed to,
which is A3's entire point.  Epic 12-A is the first campaign boundary to change
a frontend public declaration: 0 of the 17 accepted additive transitions carry
``surface: "frontend"``.

``member_transitions`` cannot be reused, for three measured reasons.  It
hard-rejects ``surface != "python"``; ``target_origin`` returns ``None`` for a
frontend descriptor because ``initializer_module`` only resolves
``packages/*/src/**/__init__.py``; and ``_members_of`` returns ``None`` because
a frontend descriptor carries ``declaration = {kind, name, signature}`` rather
than a ``public_signature`` with a member list.  Two of that authority's four
prongs therefore have no frontend input at all.

This is the fourth authority beside ``additive_transitions``,
``compatibility_bridges`` and ``member_transitions``.  It is consulted *instead
of* the unconditional rejection when a record claims the identity, and it is
fail-closed on every prong:

1. exact field set, every value a non-empty string, ``owner_package``
   well-formed, ``surface`` is ``frontend``, both digests well-formed **and
   distinct**, and ``accepting_commit`` a full SHA that resolves in this
   repository.  The distinctness check lives here rather than in the change
   check so that a record claiming a transition from a digest to itself is
   refused on every load, not only during a regeneration that reaches it;
2. ``old_target_sha256``/``new_target_sha256`` pin the removed and added
   ``TargetKey`` digests exactly, so one record authorizes exactly one
   transition;
3. ``declaration_kind`` equals the ``kind`` of **both** the accepted and the
   live declaration — a ``TypeAliasDeclaration`` silently becoming an
   ``InterfaceDeclaration`` is a different change and may not ride this record;
4. ``old_signature``/``new_signature`` equal the accepted and live declaration
   text **byte-for-byte**.  Full text is stored rather than a computed delta
   deliberately: a frontend signature is raw source, any textual delta would be
   brittle against formatting, and a reviewer should read the actual change;
5. :func:`check_declaration_delta` mirrors ``check_member_delta``, so a stale or
   extra record fails regeneration and a preserved record may not be edited;
6. :func:`check_declaration_disjoint` proves no identity is claimed by this
   authority and a bridge or member record at once.

:func:`check_one_declaration` re-validates every carried record against the live
surface on each ``check_public_api`` call, so a record cannot outlive the
declaration it describes.

No wildcards, no defaults, no partial records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._constants import (
    DECLARATION_FIELDS,
    GIT_SHA,
    PACKAGE,
    SHA256,
    TargetIdentity,
    TargetKey,
)
from ._members import commit_resolves
from ._surface import canonical

DeclarationKey = tuple[str, str]


def _declaration_of(target: dict[str, Any]) -> dict[str, str] | None:
    """Return one frontend descriptor's declaration, or ``None`` if unusable."""
    declaration = target.get("declaration")
    if not isinstance(declaration, dict):
        return None
    kind = declaration.get("kind")
    signature = declaration.get("signature")
    if not isinstance(kind, str) or not kind:
        return None
    if not isinstance(signature, str) or not signature:
        return None
    return {"kind": kind, "signature": signature}


def _valid_declaration_row(row: Any, root: Path) -> bool:
    """Prong 1: schema, surface, package form, digest form and resolvability."""
    if not isinstance(row, dict) or set(row) != DECLARATION_FIELDS:
        return False
    if not all(isinstance(row[key], str) and row[key] for key in DECLARATION_FIELDS):
        return False
    if row["surface"] != "frontend":
        return False
    if not PACKAGE.fullmatch(row["owner_package"]):
        return False
    if not SHA256.fullmatch(row["old_target_sha256"]):
        return False
    if not SHA256.fullmatch(row["new_target_sha256"]):
        return False
    if row["old_target_sha256"] == row["new_target_sha256"]:
        return False
    if not GIT_SHA.fullmatch(row["accepting_commit"]):
        return False
    return commit_resolves(root, row["accepting_commit"])


def declaration_authority(
    baseline: dict[str, Any], root: Path, problems: list[str],
) -> dict[DeclarationKey, dict[str, Any]]:
    """Return the validated ``frontend_declaration_transitions`` map."""
    rows = baseline.get("frontend_declaration_transitions", [])
    if not isinstance(rows, list) or not all(
        _valid_declaration_row(row, root) for row in rows
    ):
        problems.append(
            "frontend_declaration_transitions has invalid explicit metadata"
        )
        return {}
    if rows != sorted(rows, key=canonical):
        problems.append("frontend_declaration_transitions must be canonically sorted")
    result: dict[DeclarationKey, dict[str, Any]] = {}
    for row in rows:
        key = (row["path"], row["public_name"])
        if key in result:
            problems.append(f"duplicate frontend declaration transition target: {key}")
            continue
        result[key] = row
    return result


def _check_target_pins(
    identity: TargetIdentity, record: dict[str, Any],
    old_key: TargetKey, new_key: TargetKey, problems: list[str],
) -> None:
    """Prong 2: the record pins the exact removed and added target digests."""
    if record["old_target_sha256"] != old_key[3]:
        problems.append(
            "frontend declaration transition old_target_sha256 does not pin the "
            f"accepted target: {identity}; actual={old_key[3]}"
        )
    if record["new_target_sha256"] != new_key[3]:
        problems.append(
            "frontend declaration transition new_target_sha256 does not pin the "
            f"live target: {identity}; actual={new_key[3]}"
        )


def _check_declaration_text(
    identity: TargetIdentity, record: dict[str, Any],
    old_target: dict[str, Any], new_target: dict[str, Any], problems: list[str],
) -> None:
    """Prongs 3 and 4: one stable kind, and both declaration texts pinned."""
    old_declaration = _declaration_of(old_target)
    new_declaration = _declaration_of(new_target)
    if old_declaration is None or new_declaration is None:
        problems.append(
            f"frontend declaration transition target has no declaration: {identity}"
        )
        return
    kinds = {old_declaration["kind"], new_declaration["kind"], record["declaration_kind"]}
    if len(kinds) != 1:
        problems.append(
            "frontend declaration transition requires one unchanged declaration "
            f"kind equal to the record: {identity}; kinds={sorted(kinds)}"
        )
    if record["old_signature"] != old_declaration["signature"]:
        problems.append(
            "frontend declaration transition old_signature does not equal the "
            f"accepted declaration: {identity}"
        )
    if record["new_signature"] != new_declaration["signature"]:
        problems.append(
            "frontend declaration transition new_signature does not equal the "
            f"live declaration: {identity}"
        )


def check_declaration_change(
    identity: TargetIdentity, old_keys: list[TargetKey], new_keys: list[TargetKey],
    previous_targets: dict[TargetKey, dict[str, Any]],
    current_targets: dict[TargetKey, dict[str, Any]],
    record: dict[str, Any], problems: list[str],
) -> None:
    """Authorize one frontend declaration change, or explain exactly why not."""
    if len(old_keys) != 1 or len(new_keys) != 1:
        problems.append(
            "frontend declaration transition requires one accepted and one live "
            f"target: {identity}"
        )
        return
    _check_target_pins(identity, record, old_keys[0], new_keys[0], problems)
    _check_declaration_text(
        identity, record, previous_targets[old_keys[0]],
        current_targets[new_keys[0]], problems,
    )


def check_one_declaration(
    record: dict[str, Any], identity: TargetIdentity,
    targets: dict[TargetKey, dict[str, Any]], problems: list[str],
) -> None:
    """A carried record must still describe exactly one live declaration."""
    keys = [key for key in targets if key[:3] == identity]
    if len(keys) != 1:
        problems.append(
            "frontend declaration transition no longer names one live public "
            f"target: {[describe(record)]}"
        )
        return
    if record["new_target_sha256"] != keys[0][3]:
        problems.append(
            "frontend declaration transition new_target_sha256 does not match "
            f"the live target: {describe(record)}; actual={keys[0][3]}"
        )
    declaration = _declaration_of(targets[keys[0]])
    if (
        declaration is None
        or record["declaration_kind"] != declaration["kind"]
        or record["new_signature"] != declaration["signature"]
    ):
        problems.append(
            "frontend declaration transition does not match the live "
            f"declaration: {describe(record)}"
        )


def check_declaration_delta(
    previous: dict[str, Any], inventory: dict[str, Any], root: Path,
    current_identities: set[TargetIdentity],
    changed_declarations: set[TargetIdentity], problems: list[str],
) -> None:
    """Prong 5: mirror ``check_member_delta`` so stale/extra records fail."""
    previous_rows = declaration_authority(previous, root, problems)
    current_rows = declaration_authority(inventory, root, problems)
    preserved = {
        ("frontend", path, name)
        for path, name in previous_rows
        if ("frontend", path, name) in current_identities
        and ("frontend", path, name) not in changed_declarations
    }
    expected = preserved | changed_declarations
    current = {("frontend", path, name) for path, name in current_rows}
    if current != expected:
        problems.append(
            "frontend declaration transitions do not exactly authorize "
            f"regeneration: missing={sorted(expected - current)}, "
            f"extra={sorted(current - expected)}"
        )
    for _, path, name in preserved:
        key = (path, name)
        if current_rows.get(key) != previous_rows[key]:
            problems.append(
                f"accepted frontend declaration transition changed: {key}"
            )


def check_declaration_disjoint(
    declarations: dict[DeclarationKey, dict[str, Any]],
    bridges: dict[DeclarationKey, dict[str, Any]],
    members: dict[DeclarationKey, dict[str, Any]],
    problems: list[str],
) -> None:
    """Prong 6: one identity may not be claimed by two authorities at once."""
    overlap = sorted(set(declarations) & (set(bridges) | set(members)))
    if overlap:
        problems.append(
            "frontend declaration transition and a Python authority claim the "
            f"same target: {overlap}"
        )


def describe(record: dict[str, Any]) -> str:
    """Return one stable human-readable line for receipts."""
    return json.dumps(
        {
            "path": record["path"],
            "public_name": record["public_name"],
            "declaration_kind": record["declaration_kind"],
            "owner_package": record["owner_package"],
            "accepting_commit": record["accepting_commit"],
        },
        sort_keys=True,
    )
