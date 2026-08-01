"""Contract-byte and generated-diagram authority checks.

Relocated verbatim from ``public_api`` in Epic 10-D; behaviour is unchanged.
``ACCEPTED_DIAGRAM_SHA256`` is not monkeypatched by the adversarial suite, so
it moves with the functions that read it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ._constants import ACCEPTED_DIAGRAM_SHA256, SHA256

_DIAGRAM_REL = "docs/architecture.generated.md"
_PKG02_BASE = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"


def check_contract_file(
    contract: dict[str, Any], root: Path, problems: list[str],
) -> None:
    rel = contract.get("path")
    expected = contract.get("sha256")
    if (
        not isinstance(rel, str)
        or not rel
        or not isinstance(expected, str)
        or not SHA256.fullmatch(expected)
    ):
        problems.append(f"invalid contract-file authority: {contract}")
        return
    path = root / rel
    if not path.is_file():
        problems.append(f"contract file missing: {rel}")
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected != actual or contract.get("bytes") != path.stat().st_size:
        problems.append(
            f"contract file drift: {rel} expected {expected}, actual {actual}"
        )


def check_diagram_transition(
    transition: dict[str, Any], root: Path,
    contracts: dict[str, dict[str, Any]], problems: list[str],
) -> None:
    required = {
        "owner",
        "reason",
        "from_sha256",
        "to_sha256",
        "from_parent",
        "path",
    }
    if set(transition) != required:
        problems.append(f"diagram transition schema mismatch: {transition}")
        return
    if (
        transition["owner"] != "PKG-02-GATE"
        or not transition["reason"]
        or transition["from_sha256"] != ACCEPTED_DIAGRAM_SHA256
        or transition["from_parent"] != _PKG02_BASE
        or transition["path"] != _DIAGRAM_REL
    ):
        problems.append(f"diagram transition accepted authority drift: {transition}")
        return
    path = root / transition["path"]
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    contract = contracts.get(transition["path"], {})
    if (
        transition["to_sha256"] != actual
        or contract.get("sha256") != actual
        or transition["to_sha256"] == transition["from_sha256"]
    ):
        problems.append("diagram transition target/contract bytes do not agree")


def contract_snapshot(root: Path, paths: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in sorted(paths):
        path = root / rel
        if not path.is_file():
            raise FileNotFoundError(f"contract file missing: {rel}")
        rows.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    return rows


def regenerated_contracts(previous: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    rows = previous.get("contract_files")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and isinstance(row.get("path"), str) for row in rows
    ):
        raise RuntimeError("accepted contract-file path authority is missing")
    paths = [row["path"] for row in rows]
    if (
        paths != sorted(paths) or len(paths) != len(set(paths))
        or _DIAGRAM_REL not in paths
    ):
        raise RuntimeError("accepted contract-file paths are invalid")
    actual = contract_snapshot(root, paths)
    accepted = {row["path"]: row for row in rows}
    changed = [row for row in actual if row["path"] != _DIAGRAM_REL
               and row != accepted[row["path"]]]
    if changed:
        raise RuntimeError(f"non-diagram contract bytes cannot be rebaselined: {changed}")
    return actual


def updated_diagram_transitions(
    previous: dict[str, Any], root: Path,
) -> list[dict[str, Any]]:
    rows = previous.get("diagram_transitions")
    if (
        not isinstance(rows, list) or len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("from_sha256") != ACCEPTED_DIAGRAM_SHA256
    ):
        raise RuntimeError("accepted diagram transition authority is missing")
    transition = dict(rows[0])
    diagram = root / "docs" / "architecture.generated.md"
    transition["to_sha256"] = hashlib.sha256(diagram.read_bytes()).hexdigest()
    return [transition]
