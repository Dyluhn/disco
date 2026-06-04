"""Persistent, shared assignment store — the single source of truth for which
model each role uses, so a change in the Settings UI actually changes which model
the running system calls.

The catalogue (the assignable models + their endpoints) stays code-defined in
`default_config()`; only the ASSIGNMENT overlay (`default_model` + per-role model
keys) is persisted here, as JSON at `PMX_CONFIG`. Both servers point at the same
file: the app-server writes it when the user reassigns a role, and the
agent-server reads it per request to resolve routes. Keeping the catalogue in code
avoids a stale-on-disk catalogue when `default_config()` changes; model CRUD (a
mutable catalogue) can extend this store later.

Writes are atomic (temp file + rename) so a concurrent reader never sees a partial
file. Loads are defensive: an unreadable/invalid/stale overlay falls back to the
base config rather than crashing the router.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from .config import RouterConfig, default_config
from .types import ModelRole

_ENV_PATH = "PMX_CONFIG"
_DEFAULT_PATH = "perpleximanus-config.json"


class ConfigStore:
    """Loads/saves the assignment overlay over a code-defined base catalogue."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        base_factory: Callable[[], RouterConfig] = default_config,
    ) -> None:
        self._path = Path(path or os.environ.get(_ENV_PATH, _DEFAULT_PATH))
        self._base_factory = base_factory

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RouterConfig:
        """The base catalogue with the persisted assignment overlay applied. A
        missing/corrupt/stale overlay yields the base config unchanged (the system
        keeps working on its defaults rather than failing)."""
        base = self._base_factory()
        overlay = self._read_overlay()
        if overlay is None:
            return base
        default_model = overlay.get("default_model")
        if not isinstance(default_model, str) or default_model not in base.models:
            default_model = base.default_model
        assignments = dict(base.assignments)
        raw = overlay.get("assignments")
        if isinstance(raw, dict):
            for role_str, key in raw.items():
                role = _role(role_str)
                # Only honor real roles assigned to models that exist (a stale key
                # is dropped, not a crash — fail safe to the base assignment).
                if role is not None and isinstance(key, str) and key in base.models:
                    assignments[role] = key
        return base.model_copy(update={"default_model": default_model, "assignments": assignments})

    def save_assignments(
        self, default_model: str, assignments: dict[ModelRole, str]
    ) -> RouterConfig:
        """Persist the overlay (validating every key exists in the catalogue) and
        return the resulting full config. Raises ValueError on an unknown key — an
        assignment to a non-existent model is a structural error, not a capability
        prediction the UI is forbidden from making."""
        base = self._base_factory()
        if default_model not in base.models:
            raise ValueError(f"unknown default_model {default_model!r}")
        for role, key in assignments.items():
            if key not in base.models:
                raise ValueError(f"unknown model {key!r} for role {role.value}")
        payload = {
            "default_model": default_model,
            "assignments": {role.value: key for role, key in assignments.items()},
        }
        self._write(payload)
        return self.load()

    # -- internals ------------------------------------------------------------

    def _read_overlay(self) -> dict | None:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _write(self, payload: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)  # atomic on POSIX


def _role(value: str) -> ModelRole | None:
    try:
        return ModelRole(value)
    except ValueError:
        return None
