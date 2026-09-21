"""The provider-spending runner requires an explicit task-seed range."""

from __future__ import annotations

import pytest
from harness.build_soak._runner.cli import _argument_parser, _CliFailure, _select_scenarios


def test_seed_base_is_required() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _argument_parser().parse_args(["--scenario", "static_html_minimal"])

    assert exc_info.value.code == 2


def test_seed_base_accepts_zero_explicitly() -> None:
    args = _argument_parser().parse_args(["--scenario", "static_html_minimal", "--seed-base", "0"])

    assert args.seed_base == 0


def test_seed_base_rejects_negative_values() -> None:
    args = _argument_parser().parse_args(["--scenario", "static_html_minimal", "--seed-base", "-1"])

    with pytest.raises(_CliFailure, match="--seed-base must be nonnegative"):
        _select_scenarios(
            args,
            scenario_loader=lambda _: {"static_html_minimal": {}},
        )
