"""Command-line parsing for the gVisor stress harness."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

from .constants import (
    MAX_CONCURRENCY_PER_CONTAINER,
    MAX_CONTAINERS,
    MAX_EXECS_PER_CONTAINER,
    MIN_DURATION_CAP_S,
)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_int_at_most(maximum: int) -> Any:
    """Build an argparse type that keeps user-shaped task allocation finite."""

    def parse(value: str) -> int:
        parsed = positive_int(value)
        if parsed > maximum:
            raise argparse.ArgumentTypeError(f"must be <= {maximum}")
        return parsed

    return parse


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def duration_cap_value(value: str) -> float:
    parsed = positive_float(value)
    if parsed < MIN_DURATION_CAP_S:
        raise argparse.ArgumentTypeError(
            f"must be >= {MIN_DURATION_CAP_S:g} seconds to reserve supervised cleanup"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stress the real LocalSandboxService/docker-py path with concurrent "
            "runsc or runc containers and emit a JSON evidence report."
        )
    )
    parser.add_argument(
        "--containers",
        type=positive_int_at_most(MAX_CONTAINERS),
        default=6,
        metavar="M",
    )
    parser.add_argument(
        "--execs-per-container",
        type=positive_int_at_most(MAX_EXECS_PER_CONTAINER),
        default=40,
        metavar="K",
    )
    parser.add_argument(
        "--concurrency",
        type=positive_int_at_most(MAX_CONCURRENCY_PER_CONTAINER),
        default=4,
        help="maximum concurrent execs per container (default: 4)",
    )
    parser.add_argument(
        "--runtime",
        choices=("runsc", "runc"),
        default="runsc",
        help="Docker runtime; use runc for an A/B control (default: runsc)",
    )
    parser.add_argument(
        "--docker-socket",
        default="/var/run/docker.sock",
        help="Docker socket path or Docker base URL (default: /var/run/docker.sock)",
    )
    parser.add_argument(
        "--duration-cap",
        type=duration_cap_value,
        default=600.0,
        metavar="SECONDS",
        help="global workload plus teardown cap, minimum 10s (default: 600)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        metavar="FILE",
        help="write the JSON report to FILE; DEBUG evidence is written beside it",
    )
    return parser
