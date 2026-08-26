"""Exact member projections for non-class Python public signatures."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture.public_api_parts import _members  # noqa: E402


def _target(signature: dict[str, Any], origin: str) -> dict[str, Any]:
    module, name = origin.rsplit(".", 1)
    return {
        "public_signature": signature,
        "import_origins": [
            {
                "public_name": name,
                "name": name,
                "module": module.rsplit(".", 1)[-1],
                "level": 1,
                "alias": None,
                "kind": "from",
            }
        ],
        "path": "packages/demo/src/disco/demo/__init__.py",
    }


def _record(
    old: dict[str, Any], new: dict[str, Any], added: list[str], removed: list[str]
) -> dict[str, Any]:
    return {
        "origin": "disco.demo.models.Public",
        "old_signature_sha256": _members.signature_sha256(old),
        "new_signature_sha256": _members.signature_sha256(new),
        "added_members": added,
        "removed_members": removed,
    }


def _check(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    added: list[str],
    removed: list[str] | None = None,
) -> list[str]:
    identity = ("python", "packages/demo/src/disco/demo/__init__.py", "Public")
    old_key = (*identity, "a" * 64)
    new_key = (*identity, "b" * 64)
    origin = "disco.demo.models.Public"
    problems: list[str] = []
    _members.check_member_change(
        identity,
        [old_key],
        [new_key],
        {old_key: _target(old, origin)},
        {new_key: _target(new, origin)},
        _record(old, new, added, removed or []),
        problems,
    )
    return problems


def test_discriminated_union_addition_is_exact() -> None:
    old = {
        "kind": "value",
        "signature": "Public=Annotated[OldEvent | AnotherEvent, Field(discriminator='kind')]",
    }
    new = {
        "kind": "value",
        "signature": (
            "Public=Annotated[OldEvent | AnotherEvent | NewEvent, "
            "Field(discriminator='kind')]"
        ),
    }
    assert _members._members_of(old) == ["OldEvent", "AnotherEvent"]
    assert _members._members_of(new) == ["OldEvent", "AnotherEvent", "NewEvent"]
    assert _check(old, new, added=["NewEvent"]) == []
    assert any("delta does not match" in problem for problem in _check(old, new, added=[]))
    assert any(
        "delta does not match" in problem
        for problem in _check(old, new, added=["ForgedEvent"])
    )


def test_discriminated_union_metadata_change_is_not_a_member_delta() -> None:
    old = {
        "kind": "value",
        "signature": "Public=Annotated[OldEvent | NewEvent, Field(discriminator='kind')]",
    }
    new = {
        "kind": "value",
        "signature": "Public=Annotated[OldEvent | NewEvent, Field(discriminator='type')]",
    }
    assert _members._members_of(old) == _members._members_of(new)
    assert any(
        "may change literal members only" in problem for problem in _check(old, new, added=[])
    )


def test_literal_frozenset_addition_is_exact_and_opaque_values_fail_closed() -> None:
    old = {"kind": "value", "signature": "Public=frozenset({'read', 'write'})"}
    new = {"kind": "value", "signature": "Public=frozenset({'read', 'write', 'inspect'})"}
    assert _members._members_of(old) == ["read", "write"]
    assert _members._members_of(new) == ["inspect", "read", "write"]
    assert _check(old, new, added=["inspect"]) == []
    assert any("delta does not match" in problem for problem in _check(old, new, added=[]))
    assert _members._members_of({"kind": "value", "signature": "Public=frozenset(TOOLS)"}) is None
    assert (
        _members._members_of(
            {"kind": "value", "signature": "Public=frozenset({name for name in TOOLS})"}
        )
        is None
    )


def test_function_optional_parameter_addition_is_exact() -> None:
    old = {
        "kind": "function",
        "signature": "def Public(*, phase: Phase, output: str | None=None) -> Scope",
    }
    new = {
        "kind": "function",
        "signature": (
            "def Public(*, phase: Phase, output: str | None=None, "
            "allow_abort: bool=True) -> Scope"
        ),
    }
    assert _members._members_of(old) == ["*", "phase: Phase", "output: str | None=None"]
    assert _members._members_of(new) == [
        "*",
        "phase: Phase",
        "output: str | None=None",
        "allow_abort: bool=True",
    ]
    assert _check(old, new, added=["allow_abort: bool=True"]) == []
    assert any("delta does not match" in problem for problem in _check(old, new, added=[]))


def test_function_return_change_is_not_authorized_as_a_parameter_delta() -> None:
    old = {"kind": "function", "signature": "def Public(value: str) -> Scope"}
    new = {"kind": "function", "signature": "def Public(value: str) -> OtherScope"}
    assert _members._members_of(old) == _members._members_of(new) == ["value: str"]
    assert any(
        "may change parameters only" in problem for problem in _check(old, new, added=[])
    )


def test_unsupported_signature_kinds_remain_unrepresentable() -> None:
    assert _members._members_of({"kind": "value", "signature": "Public=make_tools()"}) is None
    assert _members._members_of({"kind": "value", "signature": "Public=Old | New"}) is None
    assert _members._members_of({"kind": "function", "signature": "def Public(...):"}) is None
