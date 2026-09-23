"""The finite migration cannot authorize unrelated deletions or erase history."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development/scripts"))
from architecture.test_inventory_parts import _reliability, _retirements


@pytest.fixture
def replacement_case(tmp_path, monkeypatch):
    roots = {"packages": ["old", "keep"], "harness": ["h"], "tests": ["t"], "integrations": ["i"]}
    mapping = {
        key: []
        for key in (
            "python_test_files",
            "python_static_test_ids",
            "typescript_test_files",
            "typescript_static_test_ids",
            "fixtures",
            "markers",
        )
    }
    old = {
        "source_identity": "b" * 40,
        "collected": {"roots": roots},
        "mapping_static": mapping,
        "additive_transitions": [
            {"package": "PKG-01-OLD", "collected_roots": {}, "receipt": "immutable"}
        ],
    }
    blob = json.dumps(old).encode()
    current = copy.deepcopy(old)
    current["source_identity"] = "c" * 40
    current["collected"]["roots"]["packages"] = ["keep", "new"]
    retired = {key: [] for key in _retirements.values(roots, mapping)}
    retired["collected.packages"] = ["old"]
    record = {
        "package": _reliability.PACKAGE,
        "baseline_commit": "a" * 40,
        "baseline_inventory_sha256": hashlib.sha256(blob).hexdigest(),
        "source_identity_before": old["source_identity"],
        "retired": retired,
        "dispositions": [
            {
                "retired_id": "old",
                "reason": "Explicit policy replaces inference",
                "replacements": ["new"],
            }
        ],
    }
    current["additive_transitions"].append(
        {
            "package": _reliability.PACKAGE,
            "source_identity_before": "b" * 40,
            "source_identity_after": "c" * 40,
            "collected_roots": {
                "packages": {
                    "before_count": 2,
                    "after_count": 2,
                    "retired_count": 1,
                    "added_ids": ["new"],
                }
            },
        }
    )
    encoded = json.dumps(record).encode()
    path = tmp_path / _reliability.AUTHORITY_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(encoded)
    monkeypatch.setattr(_reliability, "BASELINE_COMMIT", "a" * 40)
    monkeypatch.setattr(_reliability, "BASELINE_SHA256", hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(_reliability, "AUTHORITY_SHA256", hashlib.sha256(encoded).hexdigest())

    def git(command, **kwargs):
        if "rev-list" in command:
            return ("c" * 40 + " " + "a" * 40).encode()
        return blob

    monkeypatch.setattr(_reliability.subprocess, "check_output", git)
    return tmp_path, record, old, current


def test_exact_replacements_preserve_history(replacement_case):
    root, record, old, current = replacement_case
    problems = []
    assert _reliability.historical_inventory(root, current, problems) == old
    assert not problems
    allowed = _reliability.regeneration_authorizations(
        root, old["source_identity"], current["collected"]["roots"], current["mapping_static"], {}
    )
    assert allowed["collected.packages"] == {'"old"'}
    assert current["additive_transitions"][0] == old["additive_transitions"][0]


@pytest.mark.parametrize(
    "mutation", ["history", "count", "before", "missing_replacement", "extra_removal"]
)
def test_rejects_laundering_and_lost_coverage(replacement_case, mutation):
    root, record, old, current = replacement_case
    row = current["additive_transitions"][-1]
    if mutation == "history":
        current["additive_transitions"][0]["receipt"] = "rewritten"
    elif mutation == "count":
        row["collected_roots"]["packages"]["retired_count"] = 2
    elif mutation == "before":
        row["source_identity_before"] = "d" * 40
    elif mutation == "missing_replacement":
        current["collected"]["roots"]["packages"].remove("new")
    else:
        current["collected"]["roots"]["packages"].remove("keep")
    problems = []
    assert _reliability.historical_inventory(root, current, problems) is None
    assert problems


def test_allowance_cannot_be_reused(replacement_case):
    root, record, old, current = replacement_case
    with pytest.raises(RuntimeError, match="single-use"):
        _reliability.regeneration_authorizations(
            root,
            current["source_identity"],
            current["collected"]["roots"],
            current["mapping_static"],
            {},
        )


@pytest.mark.parametrize("target", ["record", "parent", "source_inventory"])
def test_source_and_record_pins_fail_closed(replacement_case, monkeypatch, target):
    root, record, old, current = replacement_case
    original = _reliability.subprocess.check_output
    if target == "record":
        (root / _reliability.AUTHORITY_PATH).write_text("{}")
    else:

        def git(command, **kwargs):
            if target == "parent" and "rev-list" in command:
                return ("c" * 40 + " " + "d" * 40).encode()
            if target == "source_inventory" and command[-1].startswith("c" * 40 + ":"):
                return b"changed"
            return original(command, **kwargs)

        monkeypatch.setattr(_reliability.subprocess, "check_output", git)
    problems = []
    assert _reliability.historical_inventory(root, current, problems) is None
    assert problems
