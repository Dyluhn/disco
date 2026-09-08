"""Finite closeout retirement authority refuses historical laundering and new removals."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import test_inventory  # noqa: E402
from architecture.test_inventory_parts import _retirements  # noqa: E402


def _inventory():
    return {
        "source_identity": "b" * 40,
        "collected": {
            "roots": {
                "packages": ["old", "keep"],
                "harness": ["h"],
                "tests": ["t"],
                "integrations": ["i"],
            }
        },
        "mapping_static": {
            "python_test_files": ["old.py", "keep.py"],
            "python_static_test_ids": ["old.py::test_old", "keep.py::test_keep"],
            "typescript_test_files": [],
            "typescript_static_test_ids": [],
            "fixtures": [],
            "markers": [],
        },
        "additive_transitions": [
            {"package": "PKG-01-OLD", "collected_roots": {}, "receipt": "immutable"}
        ],
    }


@pytest.fixture
def authority_case(tmp_path, monkeypatch):
    old = _inventory()
    baseline_blob = json.dumps(old).encode()
    old_sha = hashlib.sha256(baseline_blob).hexdigest()
    current = copy.deepcopy(old)
    current["source_identity"] = "c" * 40
    current["collected"]["roots"]["packages"] = ["keep", "new"]
    current["mapping_static"]["python_test_files"] = ["keep.py"]
    current["mapping_static"]["python_static_test_ids"] = ["keep.py::test_keep"]
    before = _retirements.values(old["collected"]["roots"], old["mapping_static"])
    after = _retirements.values(current["collected"]["roots"], current["mapping_static"])
    record = {
        "baseline_commit": "a" * 40,
        "baseline_inventory_sha256": old_sha,
        "source_identity_before": old["source_identity"],
        "package": _retirements.PACKAGE,
        "retired": {
            key: [item for item in value if item not in after[key]] for key, value in before.items()
        },
    }
    record["behavior_ledger_sha256"] = hashlib.sha256(b"ledger").hexdigest()
    ledger_path = tmp_path / "development/architecture/research-test-retirement-dispositions.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_bytes(b"ledger")
    blob = json.dumps(record).encode()
    path = tmp_path / _retirements.AUTHORITY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    monkeypatch.setattr(_retirements, "BASELINE_COMMIT", "a" * 40)
    monkeypatch.setattr(_retirements, "BASELINE_SHA256", old_sha)
    monkeypatch.setattr(_retirements, "AUTHORITY_SHA256", hashlib.sha256(blob).hexdigest())

    def git(command, **_kwargs):
        if "rev-list" in command:
            return (("c" * 40) + " " + ("a" * 40)).encode()
        return baseline_blob

    monkeypatch.setattr(_retirements.subprocess, "check_output", git)
    monkeypatch.setattr(
        test_inventory,
        "_split_authorizations",
        lambda *_args: ({}, [], SimpleNamespace(count_problems=lambda: [])),
    )
    current["additive_transitions"].append(
        {
            "package": _retirements.PACKAGE,
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
    return tmp_path, record, old, current


def test_exact_retirements_preserve_the_original_receipt(authority_case):
    root, _record, old, current = authority_case
    problems = []
    assert _retirements.historical_inventory(root, current, problems) == old
    assert problems == []
    assert current["additive_transitions"][0] == old["additive_transitions"][0]


@pytest.mark.parametrize("mutation", ["history", "count", "before", "missing", "unrecorded"])
def test_closeout_rejects_laundering_or_unrecorded_removals(authority_case, mutation):
    root, _record, _old, current = authority_case
    closure = current["additive_transitions"][-1]
    if mutation == "history":
        current["additive_transitions"][0]["receipt"] = "rewritten"
    elif mutation == "count":
        closure["collected_roots"]["packages"]["retired_count"] = 2
    elif mutation == "before":
        closure["source_identity_before"] = "d" * 40
    elif mutation == "missing":
        del closure["collected_roots"]["packages"]
    else:
        current["collected"]["roots"]["packages"].remove("keep")
    problems = []
    assert _retirements.historical_inventory(root, current, problems) is None
    assert problems


@pytest.mark.parametrize("mutation", ["extra", "missing", "wildcard", "line_drift"])
def test_retirement_lists_must_equal_real_removals(authority_case, mutation):
    _root, record, old, current = authority_case
    if mutation == "extra":
        record["retired"]["collected.packages"].append("keep")
    elif mutation == "missing":
        record["retired"]["collected.packages"] = []
    elif mutation == "wildcard":
        record["retired"]["collected.packages"] = ["*"]
    else:
        old["mapping_static"]["fixtures"] = [{"path": "keep.py", "line": 1}]
        current["mapping_static"]["fixtures"] = [{"path": "keep.py", "line": 2}]
    with pytest.raises(RuntimeError, match="not exact"):
        _retirements.exact_removals(
            record, old, current["collected"]["roots"], current["mapping_static"], {}
        )


@pytest.mark.parametrize("artifact", ["authority", "ledger"])
def test_modified_authority_bytes_are_rejected(authority_case, artifact):
    root, _record, _old, current = authority_case
    path = root / (
        _retirements.AUTHORITY_PATH
        if artifact == "authority"
        else "development/architecture/research-test-retirement-dispositions.json"
    )
    path.write_bytes(path.read_bytes() + b" ")
    problems = []
    assert _retirements.historical_inventory(root, current, problems) is None
    assert "digest mismatch" in problems[0]


def test_foreign_source_parent_is_rejected(authority_case, monkeypatch):
    root, _record, _old, current = authority_case
    original_git = _retirements.subprocess.check_output

    def foreign_parent(command, **kwargs):
        if "rev-list" in command:
            return (("c" * 40) + " " + ("d" * 40)).encode()
        return original_git(command, **kwargs)

    monkeypatch.setattr(_retirements.subprocess, "check_output", foreign_parent)
    problems = []
    assert _retirements.historical_inventory(root, current, problems) is None
    assert problems


def test_retirements_cannot_authorize_a_future_removal(authority_case):
    root, _record, _old, current = authority_case
    with pytest.raises(RuntimeError, match="single-use"):
        _retirements.regeneration_authorizations(
            root,
            "c" * 40,
            _retirements.PACKAGE,
            current["collected"]["roots"],
            current["mapping_static"],
            {},
        )
    assert (
        _retirements.regeneration_authorizations(
            root,
            "c" * 40,
            "PKG-36-NEXT",
            current["collected"]["roots"],
            current["mapping_static"],
            {},
        )
        == {}
    )
    with pytest.raises(RuntimeError, match="unexplained deletion"):
        test_inventory._assert_no_deletions("next", ["keep"], [], authorized=set())


def test_marker_line_drift_cannot_consume_a_new_skip_in_another_file():
    from architecture.test_inventory_parts import _splits

    old = {
        "framework": "pytest",
        "path": "harness/old.py",
        "line": 84,
        "marker": "pytest.mark.skipif",
        "source": "",
    }
    moved = {**old, "line": 83}
    added = {**old, "path": "packages/new.py"}
    assert _splits.unaccounted_marker_growth([added, moved], [old]) == [added]


def test_recorded_marker_relocation_only_matches_its_declared_destination():
    from architecture.test_inventory_parts import _splits

    old = {
        "framework": "pytest",
        "path": "harness/old.py",
        "line": 84,
        "marker": "pytest.mark.skipif",
        "source": "",
    }
    moved = {**old, "path": "harness/new.py", "line": 10}
    added = {**old, "path": "packages/unrelated.py"}
    ledger = _splits.SplitLedger({"harness/old.py": {"new_paths": ["harness/new.py"]}})
    assert _splits.unaccounted_marker_growth([added, moved], [old], ledger) == [added]
