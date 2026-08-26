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
4. the exact member-token delta computed from the two signatures equals
   ``removed_members``/``added_members`` exactly — no over-authorization.
   Full member strings are stored rather than bare names, so a record cannot
   silently absorb a signature change to a *surviving* member.  The supported
   projections are class members/fields, discriminated-union alternatives,
   literal frozenset values, and function parameters;
5. :func:`check_member_delta` mirrors ``_check_bridge_delta``, so a stale or
   extra record fails regeneration;
6. ``accepting_commit`` must be a full SHA that resolves in this repository.

No wildcards, no defaults, no partial records.
"""

from __future__ import annotations

import ast
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
    """Return the exact governed member tokens for one public signature.

    The public-surface scanner records methods and typed fields separately,
    but both are semantic members of a public class.  Value aliases and
    functions use deliberately narrow AST projections below; unsupported or
    opaque expressions return ``None`` rather than becoming wildcard rows.
    """
    if not isinstance(signature, dict):
        return None
    members = signature.get("members")
    fields = signature.get("fields")
    if isinstance(members, list) and isinstance(fields, list):
        if not all(isinstance(item, str) for item in [*members, *fields]):
            return None
        return [*members, *fields]
    kind = signature.get("kind")
    text = signature.get("signature")
    if not isinstance(kind, str) or not isinstance(text, str):
        return None
    if kind == "value":
        parsed = _value_signature_parts(text)
        return None if parsed is None else parsed[1]
    if kind == "function":
        parsed = _function_signature_parts(text)
        return None if parsed is None else parsed[1]
    return None


def _value_signature_parts(signature: str) -> tuple[str, list[str], str] | None:
    """Parse one deliberately narrow, literal value transition.

    The public scanner stores assignments as text.  Only the two value shapes
    whose member additions are unambiguous are admitted here: a discriminated
    ``Annotated[A | B, Field(discriminator='kind')]`` union, and a literal
    ``frozenset({'a', 'b'})``.  Names, calls, comprehensions, and arbitrary
    metadata stay opaque and therefore cannot be authorized by a member row.
    """
    value = _assignment_value(signature)
    if value is None:
        return None
    return _frozenset_signature_parts(value) or _union_signature_parts(value)


def _assignment_value(signature: str) -> ast.expr | None:
    try:
        tree = ast.parse(signature, mode="exec")
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        return None
    assignment = tree.body[0]
    if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
        return None
    return assignment.value


def _frozenset_signature_parts(value: ast.expr) -> tuple[str, list[str], str] | None:
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) == 1
        and not value.keywords
    ):
        return None
    values = _string_set_elements(value.args[0])
    return None if values is None else ("frozenset", sorted(values), "frozenset")


def _string_set_elements(value: ast.expr) -> list[str] | None:
    if not isinstance(value, ast.Set) or not value.elts:
        return None
    values: list[str] = []
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        if not element.value or element.value in values:
            return None
        values.append(element.value)
    return values


def _union_signature_parts(value: ast.expr) -> tuple[str, list[str], str] | None:
    if not (
        isinstance(value, ast.Subscript)
        and isinstance(value.value, ast.Name)
        and value.value.id == "Annotated"
    ):
        return None
    arguments = _subscript_arguments(value.slice)
    if arguments is None or len(arguments) != 2:
        return None
    union = _union_names(arguments[0])
    metadata = arguments[1]
    if union is None or len(union) < 2 or len(union) != len(set(union)):
        return None
    if not _is_discriminator(metadata):
        return None
    return "discriminated_union", union, ast.dump(metadata, include_attributes=False)


def _is_discriminator(value: ast.expr) -> bool:
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "Field"
        and not value.args
        and len(value.keywords) == 1
    ):
        return False
    keyword = value.keywords[0]
    return (
        keyword.arg == "discriminator"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
        and bool(keyword.value.value)
    )


def _subscript_arguments(slice_node: ast.expr) -> list[ast.expr] | None:
    if isinstance(slice_node, ast.Tuple):
        return list(slice_node.elts)
    return [slice_node]


def _union_names(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _union_names(node.left)
        right = _union_names(node.right)
        if left is None or right is None:
            return None
        return [*left, *right]
    if isinstance(node, (ast.Name, ast.Attribute)):
        return [ast.unparse(node)]
    return None


def _function_signature_parts(
    signature: str,
) -> tuple[tuple[str, str, str | None], list[str]] | None:
    """Return function identity/return shape and exact parameter tokens."""
    try:
        tree = ast.parse(f"{signature}:\n    pass", mode="exec")
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(
        tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    function = tree.body[0]
    args = function.args
    parameters: list[str] = []
    positional = [*args.posonlyargs, *args.args]
    positional_defaults = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    for index, argument in enumerate(positional):
        parameters.append(_parameter_text(argument, positional_defaults[index]))
        if args.posonlyargs and index + 1 == len(args.posonlyargs):
            parameters.append("/")
    if args.vararg is not None:
        parameters.append("*" + _parameter_text(args.vararg, None))
    elif args.kwonlyargs:
        parameters.append("*")
    for argument, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parameters.append(_parameter_text(argument, default))
    if args.kwarg is not None:
        parameters.append("**" + _parameter_text(args.kwarg, None))
    return (
        (
            "async" if isinstance(function, ast.AsyncFunctionDef) else "def",
            function.name,
            ast.unparse(function.returns) if function.returns is not None else None,
        ),
        parameters,
    )


def _parameter_text(argument: ast.arg, default: ast.expr | None) -> str:
    text = ast.unparse(argument)
    if default is not None:
        text += "=" + ast.unparse(default)
    return text


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
    if not _valid_member_scalars(row):
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


def _valid_member_scalars(row: dict[str, Any]) -> bool:
    """Validate the scalar half of a member-transition record."""
    scalar_keys = (
        "surface", "path", "public_name", "old_signature_sha256",
        "new_signature_sha256", "owner_package", "accepting_commit",
        "accepting_receipt",
    )
    if not all(isinstance(row[key], str) and row[key] for key in scalar_keys):
        return False
    origin = row["origin"]
    return origin is None or isinstance(origin, str) and bool(origin)


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
    old_signature = old_target.get("public_signature")
    new_signature = new_target.get("public_signature")
    old_members = _members_of(old_signature)
    new_members = _members_of(new_signature)
    if old_members is None or new_members is None:
        problems.append(
            f"member transition target has no member list: {identity}"
        )
        return
    if _check_signature_shape(old_signature, new_signature, identity, problems):
        return
    if _check_parameter_order(
        old_signature, new_signature, old_members, new_members, identity, problems
    ):
        return
    removed = sorted(set(old_members) - set(new_members))
    added = sorted(set(new_members) - set(old_members))
    if record["removed_members"] != removed or record["added_members"] != added:
        problems.append(
            "member transition delta does not match the surfaces: "
            f"{identity}; removed={removed}; added={added}"
        )


def _check_signature_shape(
    old_signature: Any,
    new_signature: Any,
    identity: TargetIdentity,
    problems: list[str],
) -> bool:
    if not isinstance(old_signature, dict) or not isinstance(new_signature, dict):
        return False
    kind = old_signature.get("kind")
    if kind != new_signature.get("kind"):
        return False
    if kind == "function":
        return _check_function_shape(old_signature, new_signature, identity, problems)
    if kind == "value":
        return _check_value_shape(old_signature, new_signature, identity, problems)
    return False


def _check_function_shape(
    old_signature: dict[str, Any],
    new_signature: dict[str, Any],
    identity: TargetIdentity,
    problems: list[str],
) -> bool:
    old_parts = _function_signature_parts(old_signature.get("signature", ""))
    new_parts = _function_signature_parts(new_signature.get("signature", ""))
    if old_parts is not None and new_parts is not None and old_parts[0] == new_parts[0]:
        return False
    problems.append(
        "function member transition may change parameters only: "
        f"{identity}"
    )
    return True


def _check_value_shape(
    old_signature: dict[str, Any],
    new_signature: dict[str, Any],
    identity: TargetIdentity,
    problems: list[str],
) -> bool:
    old_parts = _value_signature_parts(old_signature.get("signature", ""))
    new_parts = _value_signature_parts(new_signature.get("signature", ""))
    if (
        old_parts is not None
        and new_parts is not None
        and old_parts[0] == new_parts[0]
        and old_parts[2] == new_parts[2]
    ):
        return False
    problems.append(
        "value member transition may change literal members only: "
        f"{identity}"
    )
    return True


def _check_parameter_order(
    old_signature: Any,
    new_signature: Any,
    old_members: list[str],
    new_members: list[str],
    identity: TargetIdentity,
    problems: list[str],
) -> bool:
    if not (
        isinstance(old_signature, dict)
        and isinstance(new_signature, dict)
        and old_signature.get("kind") == new_signature.get("kind") == "function"
    ):
        return False
    old_survivors = [member for member in old_members if member in new_members]
    new_survivors = [member for member in new_members if member in old_members]
    if old_survivors == new_survivors:
        return False
    problems.append(
        "function member transition cannot reorder surviving parameters: "
        f"{identity}"
    )
    return True


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
