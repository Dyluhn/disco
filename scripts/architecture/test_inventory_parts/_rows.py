"""Pure row/count comparison helpers for the test-inventory gate.

No Git access, no collector invocation, no monkeypatched state — relocated
verbatim from ``test_inventory`` in Epic 10-D.  Behaviour is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PYTHON_ROOTS = ("packages", "harness", "integrations", "tests")
PLAYWRIGHT_TEST_DIRS = {
    "playwright.config.ts": "e2e",
    "live-smoke.config.ts": "e2e-live",
    "playwright.live.config.ts": "e2e-live",
    "playwright.security-policy.config.ts": "e2e-policy",
    "playwright.trace-policy.config.ts": "e2e-policy",
    "e2e-full/full.config.ts": "e2e-full/scenarios",
}
PLAYWRIGHT_CONFIGS = tuple(PLAYWRIGHT_TEST_DIRS)
LIVE_CONFIGS = ("live-smoke.config.ts", "playwright.live.config.ts")


def collection_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env["PYTEST_ADDOPTS"] = ""
    return env


def collector_commands() -> dict[str, Any]:
    return {
        "pytest": [
            "python",
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-m",
            "<marker>",
            "--collect-only",
            "-q",
            "<root>",
        ],
        "vitest": [
            "frontend/node_modules/.bin/vitest",
            "list",
            "--root",
            "<frontend-absolute>",
            "--config",
            "<frontend-absolute>/vite.config.ts",
            "--json",
        ],
        "playwright": {
            config: [
                "frontend/node_modules/.bin/playwright",
                "test",
                "--list",
                f"--config={config}",
            ]
            for config in PLAYWRIGHT_CONFIGS
        },
    }


def canonical_row(row: Any) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def stored_strings(
    owner: dict[str, Any],
    key: str,
    label: str,
    problems: list[str],
    *,
    unique: bool = True,
) -> list[str]:
    value = owner.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{label} must be an exact string list")
        return []
    if value != sorted(value):
        problems.append(f"{label} must be sorted")
    if unique and len(value) != len(set(value)):
        problems.append(f"{label} must contain unique identities")
    if not value:
        problems.append(f"{label} must not be empty")
    return value


def stored_rows(
    owner: dict[str, Any],
    key: str,
    label: str,
    problems: list[str],
) -> list[dict[str, Any]]:
    value = owner.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        problems.append(f"{label} must be an exact object list")
        return []
    if value != sorted(value, key=canonical_row):
        problems.append(f"{label} must be canonically sorted")
    return value


def compare_exact(
    label: str,
    stored: list[Any],
    actual: list[Any],
    problems: list[str],
) -> None:
    if stored == actual:
        return
    stored_canonical = [canonical_row(item) for item in stored]
    actual_canonical = [canonical_row(item) for item in actual]
    deleted = sorted(set(stored_canonical) - set(actual_canonical))
    added = sorted(set(actual_canonical) - set(stored_canonical))
    problems.append(
        f"{label} drift: stored {len(stored)}, actual {len(actual)}, "
        f"deleted={deleted}, added={added}"
    )


def check_count(
    owner: dict[str, Any],
    key: str,
    actual: int,
    label: str,
    problems: list[str],
) -> None:
    stored = owner.get(key)
    if not isinstance(stored, int) or isinstance(stored, bool) or stored != actual:
        problems.append(f"{label} count drift: stored {stored!r}, actual {actual}")


def forbidden_marker(row: dict[str, Any]) -> bool:
    marker = str(row.get("marker", "")).lower()
    return any(name in marker for name in ("xfail", "todo", "only"))


def frontend_relative(root: Path, raw_path: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / "frontend" / path
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"collector path escapes repository: {raw_path}") from exc
