from __future__ import annotations

from pathlib import Path

import pytest

from harness.reliability.matrix import load_matrix

MATRIX = Path(__file__).parents[1] / "matrix.yaml"


def test_repository_matrix_is_complete_and_selectable() -> None:
    matrix = load_matrix(MATRIX)
    assert matrix.claims
    assert matrix.suites
    assert matrix.select_suites(proofs={"live"}, surfaces={"build"})
    assert all(
        suite.proof == "live"
        for suite in matrix.select_suites(proofs={"live"}, surfaces={"build"})
    )


def test_matrix_rejects_uncovered_claim(tmp_path: Path) -> None:
    path = tmp_path / "matrix.yaml"
    path.write_text(
        """
schema_version: 1
claims:
  - id: x
    surface: build
    proof: live
    target: 1
    description: uncovered
suites:
  - id: y
    proof: hermetic
    kind: generic
    cwd: .
    timeout_s: 1
    memory_gib: 1
    units: 1
    command: ["true"]
    claims: [missing]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown claim"):
        load_matrix(path)
