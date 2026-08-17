"""Shared imports and fixtures for the `test_project_version_*` split.

Non-collected by design (module name does not match pytest's `test_*.py`
discovery pattern) — moved verbatim out of `test_project_version_proof.py`
(module logical-line budget) alongside the topic test modules that import
from it. Nothing here is a test.
"""

from __future__ import annotations

from pathlib import Path

from disco.tools.projects.store import (
    VersionRecord,
)


def _write(workspace: Path, rel: str, data: bytes) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _version_dir(root: Path, cid: str, record: VersionRecord) -> Path:
    return root / cid / "versions" / f"{record.seq:03d}-{record.tree_digest[:12]}"
