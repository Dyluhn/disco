"""Exact, single-use public API retirements and contract migration for PKG-35."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ._surface import canonical

AUTHORITY_PATH = "development/architecture/research-api-closeout.json"
AUTHORITY_SHA256 = "5a65f72ea5ab713c2956b84587101cba275059fb1872af4eda430f6924b118dd"


def authority(root: Path, previous: dict[str, Any]) -> dict[str, Any]:
    blob = (root / AUTHORITY_PATH).read_bytes()
    if hashlib.sha256(blob).hexdigest() != AUTHORITY_SHA256:
        raise RuntimeError("research API closeout authority digest mismatch")
    record = json.loads(blob)
    if record.get("package") != "PKG-35-DEEP-RESEARCH-CLOSEOUT":
        raise RuntimeError("research API closeout owner mismatch")
    if (
        hashlib.sha256(canonical(previous).encode()).hexdigest()
        != record["baseline_public_api_sha256"]
    ):
        raise RuntimeError("research API closeout is single-use at its exact sealed baseline")
    return record


def retired_targets(root: Path, previous: dict[str, Any], deleted: list[tuple]) -> None:
    record = authority(root, previous)
    if sorted(map(list, deleted)) != record["retired_targets"]:
        raise RuntimeError("research API retirements must match all five exact sealed targets")


def contract_changes(root: Path, previous: dict[str, Any], changed: list[dict[str, Any]]) -> None:
    record = authority(root, previous)
    expected = record["contract_transitions"]
    before = {row["path"]: row for row in previous["contract_files"]}
    if any(before.get(row["before"]["path"]) != row["before"] for row in expected):
        raise RuntimeError("research contract transition does not match prior bytes")
    if sorted(changed, key=canonical) != sorted((row["after"] for row in expected), key=canonical):
        raise RuntimeError("research contract transition must match both exact proposed files")
