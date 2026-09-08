"""Finite API migration authority must not authorize unrelated future changes."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture.public_api_parts import _closeout, _members  # noqa: E402
from architecture.public_api_parts._surface import canonical  # noqa: E402


@pytest.fixture
def closeout(tmp_path, monkeypatch):
    before = {"contract_files": [{"path": "contract.md", "sha256": "a" * 64, "bytes": 1}]}
    deleted = [["frontend", "old.ts", "OldPlan", "b" * 64]]
    changed = [{"path": "contract.md", "sha256": "c" * 64, "bytes": 2}]
    record = {
        "package": "PKG-35-DEEP-RESEARCH-CLOSEOUT",
        "baseline_public_api_sha256": hashlib.sha256(canonical(before).encode()).hexdigest(),
        "retired_targets": deleted,
        "contract_transitions": [{"before": before["contract_files"][0], "after": changed[0]}],
    }
    path = tmp_path / _closeout.AUTHORITY_PATH
    path.parent.mkdir(parents=True)
    blob = json.dumps(record).encode()
    path.write_bytes(blob)
    monkeypatch.setattr(_closeout, "AUTHORITY_SHA256", hashlib.sha256(blob).hexdigest())
    return tmp_path, before, deleted, changed


def test_exact_api_retirement_and_contract_transition_are_authorized(closeout):
    root, before, deleted, changed = closeout
    _closeout.retired_targets(root, before, deleted)
    _closeout.contract_changes(root, before, changed)


@pytest.mark.parametrize("mutation", ["missing", "extra", "wildcard", "future", "authority"])
def test_retirement_rejects_inexact_or_reused_authority(closeout, mutation):
    root, before, deleted, _ = closeout
    if mutation == "missing":
        deleted = []
    elif mutation == "extra":
        deleted = deleted + [["frontend", "another.ts", "Other", "d" * 64]]
    elif mutation == "wildcard":
        deleted = [["frontend", "*", "OldPlan", "b" * 64]]
    elif mutation == "future":
        before = {**before, "source_identity": "later"}
    else:
        (root / _closeout.AUTHORITY_PATH).write_text("{}")
    with pytest.raises(RuntimeError):
        _closeout.retired_targets(root, before, deleted)


@pytest.mark.parametrize("mutation", ["bytes", "extra", "missing", "future"])
def test_contract_migration_rejects_unapproved_bytes(closeout, mutation):
    root, before, _, changed = closeout
    changed = copy.deepcopy(changed)
    if mutation == "bytes":
        changed[0]["sha256"] = "e" * 64
    elif mutation == "extra":
        changed.append({"path": "unrelated.md", "sha256": "e" * 64, "bytes": 2})
    elif mutation == "missing":
        changed = []
    else:
        before = {"contract_files": changed}
    with pytest.raises(RuntimeError):
        _closeout.contract_changes(root, before, changed)


def test_named_value_transition_pins_the_full_definition_and_kind():
    before = {"public_signature": {"kind": "value", "signature": "Event = A | B"}}
    after = {"public_signature": {"kind": "value", "signature": "Event = A | B | Checkpoint"}}
    record = {"removed_members": ["Event = A | B"], "added_members": ["Event = A | B | Checkpoint"]}
    problems = []
    _members._check_member_delta(("python", "events.py", "Event"), record, before, after, problems)
    assert problems == []
    _members._check_member_delta(
        ("python", "events.py", "Event"),
        {**record, "added_members": ["Checkpoint"]},
        before,
        after,
        problems,
    )
    assert problems
    problems = []
    changed_kind = {
        "public_signature": {
            "kind": "class",
            "signature": "class Event",
            "members": [],
            "fields": [],
        }
    }
    _members._check_member_delta(
        ("python", "events.py", "Event"), record, before, changed_kind, problems
    )
    assert any("kind" in problem for problem in problems)
