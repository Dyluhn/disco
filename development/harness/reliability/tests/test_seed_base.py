"""Focused seed-range gates for provider-spending build-soak suites."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from harness.reliability._runner.campaign import _override_build_soak_units, _suite_seed_bases
from harness.reliability._runner.suite_execution import _prepare_suite_launch
from harness.reliability.matrix import load_matrix
from harness.reliability.run import main
from harness.reliability.state import INVALID

MATRIX = Path(__file__).parents[1] / "matrix.yaml"
PROVIDER_ENV = (
    "DISCO_RELIABILITY_SEED_CONFIG",
    "DISCO_RELIABILITY_SEED_SECRETS",
    "DISCO_RELIABILITY_SEED_SECRET_KEY",
    "DISCO_RELIABILITY_SEED_APPROVALS",
    "DISCO_RELIABILITY_EXPECTED_PROVIDER_HOST",
    "DISCO_RELIABILITY_EXPECTED_PROVIDER_MODEL",
)
VISION_PROVIDER_ENV = (
    "DISCO_RELIABILITY_EXPECTED_VISION_PROVIDER_HOST",
    "DISCO_RELIABILITY_EXPECTED_VISION_MODEL",
)


def _prepare_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_base: str | None,
    vision_host: str | None = None,
):
    for key in PROVIDER_ENV:
        monkeypatch.setenv(key, "test-value")
    for key in VISION_PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    if vision_host is not None:
        monkeypatch.setenv("DISCO_RELIABILITY_EXPECTED_VISION_PROVIDER_HOST", vision_host)
    context = {
        "repo": str(tmp_path),
        "frontend": str(tmp_path / "frontend"),
        "python": str(tmp_path / "python"),
        "commit": "abc",
        "revision": "1.0.0",
    }
    if seed_base is not None:
        context["seed_base"] = seed_base
    (tmp_path / "frontend").mkdir()
    Path(context["python"]).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    campaign_out = tmp_path / "campaign"
    campaign_out.mkdir()
    suite = load_matrix(MATRIX).suites["live-build-shapes"]
    return _prepare_suite_launch(suite, context=context, campaign_out=campaign_out)


def test_live_build_shapes_missing_seed_base_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = _prepare_shapes(tmp_path, monkeypatch, seed_base=None)

    assert isinstance(launch, dict)
    assert launch["status"] == INVALID
    assert "unknown reliability matrix placeholder: seed_base" in launch["reason"]


def test_campaign_refuses_missing_seed_base_before_creating_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    output = tmp_path / "campaigns"
    result = subprocess.run(
        [
            sys.executable,
            "development/harness/reliability/run.py",
            "--suite",
            "live-build-revisions",
            "--out",
            str(output),
            "--state",
            str(tmp_path / "state.json"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "--seed-base is required" in result.stderr
    assert not output.exists()


def test_live_build_shapes_expands_exact_seed_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = _prepare_shapes(tmp_path, monkeypatch, seed_base="90000")

    assert not isinstance(launch, dict)
    index = launch.command.index("--seed-base")
    assert launch.command[index + 1] == "90000"


def test_live_build_shapes_refuses_partial_visual_provider_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = _prepare_shapes(
        tmp_path,
        monkeypatch,
        seed_base="90000",
        vision_host="ollama.com",
    )

    assert isinstance(launch, dict)
    assert launch["status"] == INVALID
    assert "visual provider host/model must be configured together" in launch["reason"]


def test_seed_base_bounds() -> None:
    assert main(["--seed-base", "-1", "--list"]) == 2
    assert main(["--seed-base", "0", "--list"]) == 0


def test_build_soak_suites_receive_disjoint_seed_ranges() -> None:
    matrix = load_matrix(MATRIX)
    suites = [
        matrix.suites["live-build-shapes"],
        matrix.suites["live-build-revisions"],
        matrix.suites["live-agent-general"],
    ]

    assigned = _suite_seed_bases(argparse.Namespace(seed_base=90000), suites)

    assert assigned == {
        "live-build-shapes": "90000",
        "live-build-revisions": "90100",
        "live-agent-general": "90200",
    }


def test_build_soak_continuation_rewrites_units_and_iterations_together() -> None:
    suite = load_matrix(MATRIX).suites["live-build-shapes"]

    selected = _override_build_soak_units(argparse.Namespace(build_soak_units=47), [suite])

    assert selected[0].units == 47
    index = selected[0].command.index("--iterations")
    assert selected[0].command[index + 1] == "47"


def test_build_soak_continuation_refuses_ambiguous_or_oversized_selection() -> None:
    matrix = load_matrix(MATRIX)
    with pytest.raises(ValueError, match="exactly one build-soak suite"):
        _override_build_soak_units(
            argparse.Namespace(build_soak_units=47),
            [matrix.suites["live-build-shapes"], matrix.suites["live-build-revisions"]],
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        _override_build_soak_units(
            argparse.Namespace(build_soak_units=101), [matrix.suites["live-build-shapes"]]
        )
