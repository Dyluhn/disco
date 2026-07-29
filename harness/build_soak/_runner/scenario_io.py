"""Bounded Build Soak scenario io owner."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .common import (
    _BATCH_SUMMARY_NAME,
    _SCENARIOS,
    _TRIGGER_AFTER_FIRST_FILE_WRITE,
)


def batch_summary_name(requested: str | None) -> str:
    """Validate a batch-report filename. A bare, safe filename only.

    Rejecting separators matters: a name like ``../batch-summary.json`` would put
    a promotion-visible report back into a parent tree, quietly undoing the
    structural exclusion a non-promoting lane is relying on.
    """

    if requested is None or requested == "":
        return _BATCH_SUMMARY_NAME
    if "/" in requested or "\\" in requested or requested in {".", ".."}:
        raise ValueError(f"batch summary name must be a bare filename, got {requested!r}")
    if not requested.endswith(".json"):
        raise ValueError(f"batch summary name must end in .json, got {requested!r}")
    return requested


def load_scenarios(path: str | Path = _SCENARIOS) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios = raw.get("scenarios") or []
    defaults = raw.get("defaults") or {}

    def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(base)
        for key, value in override.items():
            # A governed verification policy is one exact admission contract,
            # not a bag of defaults. Inheriting web claim floors into an AppKit
            # or future device policy would silently ask the wrong verifier.
            if key == "governed_verification":
                out[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out

    loaded = [merge(defaults, s) for s in scenarios if s.get("id")]
    return {str(s["id"]): s for s in loaded}


def _materialize_task_seed(value: Any, seed: int) -> Any:
    """Replace the frozen ``{{seed}}`` token throughout one scenario copy."""
    if isinstance(value, str):
        return value.replace("{{seed}}", str(seed))
    if isinstance(value, list):
        return [_materialize_task_seed(item, seed) for item in value]
    if isinstance(value, dict):
        return {key: _materialize_task_seed(item, seed) for key, item in value.items()}
    return copy.deepcopy(value)


def _driver_catalog_contains(payload: dict[str, Any], model: str) -> bool:
    """Whether the live agent reports the exact saved key as driver-eligible."""

    models = payload.get("models")
    if not isinstance(models, list):
        return False
    return any(
        isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] == model
        for entry in models
    )


def _declared_workspace_paths(scenario: dict[str, Any]) -> list[str]:
    files = ((scenario.get("assertions") or {}).get("workspace") or {}).get("files") or []
    return [str(f["path"]) for f in files if f.get("path")]


def _preview_required(scenario: dict[str, Any]) -> bool:
    return bool(((scenario.get("assertions") or {}).get("preview") or {}).get("required"))


def _browser_verification_required(scenario: dict[str, Any]) -> bool:
    assertion = (scenario.get("assertions") or {}).get("browser_verification") or {}
    return isinstance(assertion, dict) and assertion.get("required") is True


def _is_cancel_after_first_write(cancel_at: dict[str, Any] | None) -> bool:
    return (
        isinstance(cancel_at, dict) and cancel_at.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE
    )
