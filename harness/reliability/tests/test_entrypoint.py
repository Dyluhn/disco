from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness.reliability.matrix import load_matrix


def test_documented_direct_script_list_entrypoint() -> None:
    """H-005: the governing guide invokes run.py as a file, not with ``-m``."""

    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "harness/reliability/run.py", "--list"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert "hermetic" in result.stdout
    assert "live" in result.stdout


@pytest.mark.parametrize("proof", ["hermetic", "live"])
def test_surface_all_is_a_wildcard_for_dry_run(proof: str) -> None:
    """H-006: ``--surface all`` must select every suite for the proof."""

    root = Path(__file__).resolve().parents[3]
    matrix = load_matrix(root / "harness/reliability/matrix.yaml")
    expected = {suite.id for suite in matrix.select_suites(proofs={proof})}
    result = subprocess.run(
        [
            sys.executable,
            "harness/reliability/run.py",
            "--proof",
            proof,
            "--surface",
            "all",
            "--dry-run",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    selected = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    assert selected == expected
    assert selected
