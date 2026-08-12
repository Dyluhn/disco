"""Exact same-origin Python member-transition regressions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from architecture.public_api_parts import _members  # noqa: E402

_PATH = "packages/core/src/disco/core/__init__.py"
_IDENTITY = ("python", _PATH, "AgentErrorEvent")


def _signature(*, classified: bool) -> dict[str, Any]:
    fields = ["error: str"]
    if classified:
        fields.extend(
            [
                "failure_class: str | None",
                "failure_reason: str | None",
            ]
        )
    return {
        "kind": "class",
        "signature": "class AgentErrorEvent(BaseEvent, LLMConvertible)",
        "members": ["def to_llm_message(self) -> LLMMessage"],
        "fields": fields,
    }


def _target(signature: dict[str, Any]) -> dict[str, Any]:
    return {
        "surface": "python",
        "path": _PATH,
        "public_name": "AgentErrorEvent",
        "public_signature": signature,
        "import_origins": [
            {
                "public_name": "AgentErrorEvent",
                "name": "AgentErrorEvent",
                "module": "events",
                "level": 1,
                "alias": None,
                "kind": "from",
            }
        ],
        "occurrence": 1,
    }


def test_member_transition_authorizes_exact_typed_field_delta() -> None:
    old_signature = _signature(classified=False)
    new_signature = _signature(classified=True)
    old_key = (*_IDENTITY, "a" * 64)
    new_key = (*_IDENTITY, "b" * 64)
    old_target = _target(old_signature)
    new_target = _target(new_signature)
    record = {
        "origin": "disco.core.events.AgentErrorEvent",
        "removed_members": [],
        "added_members": [
            "failure_class: str | None",
            "failure_reason: str | None",
        ],
        "old_signature_sha256": _members.signature_sha256(old_signature),
        "new_signature_sha256": _members.signature_sha256(new_signature),
    }
    previous = {old_key: old_target}
    current = {new_key: new_target}

    problems: list[str] = []
    _members.check_member_change(
        _IDENTITY, [old_key], [new_key], previous, current, record, problems
    )
    assert problems == []

    forged: list[str] = []
    _members.check_member_change(
        _IDENTITY,
        [old_key],
        [new_key],
        previous,
        current,
        {**record, "added_members": ["failure_class: str | None"]},
        forged,
    )
    assert any("delta does not match" in problem for problem in forged)
