"""Pure helpers shared by gVisor stress owners."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from .types import HarnessSetupError

_LOG = logging.getLogger("disco.tools.sandbox.gvisor_stress")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def clipped(value: object, limit: int = 2_000) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def normalize_docker_socket(value: str) -> str:
    """Turn the required plain-path default into docker-py's URI form."""
    value = value.strip()
    if not value:
        raise HarnessSetupError("--docker-socket may not be empty")
    if "://" in value:
        return value
    return f"unix://{os.path.abspath(value)}"


def debug_log_path(report_path: Path) -> Path:
    if report_path.suffix:
        return report_path.with_name(f"{report_path.stem}.debug.log")
    return report_path.with_name(f"{report_path.name}.debug.log")


def cleanup_reserve(duration_cap_s: float) -> float:
    """Reserve time to drain bounded SDK workers, then sweep real resources."""
    return min(150.0, max(10.0, duration_cap_s * 0.25), duration_cap_s * 0.50)
