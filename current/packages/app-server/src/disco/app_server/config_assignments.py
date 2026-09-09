"""Assignments repository split out of ConfigState (PY-0365): the per-role
model assignment matrix over the shared `ConfigStore` document. `ConfigState`
exposes an instance of this class as the plain `assignments` attribute.
"""

from __future__ import annotations

from disco.core.llm import ConfigStore, ModelRole, RouterConfig

from .config.dtos import AssignmentsDTO, AssignmentsPatch
from .config.mappers import _assignments_from

# The single catalogue entry a fresh install ships (development/scripts/seed_config.py):
# an endpoint-less stand-in so Settings has something to show before the user adds
# a real driver.
_PLACEHOLDER_DRIVER = "driver-unconfigured"


class ConfigAssignments:
    """The per-role model assignment matrix."""

    def __init__(self, store: ConfigStore) -> None:
        self._store = store

    def assignments(self) -> AssignmentsDTO:
        return _assignments_from(self._store.load())

    def update_assignments(self, patch: AssignmentsPatch) -> AssignmentsDTO:
        """Persist the patch over the current assignments. Raises ValueError on an
        unknown model key (the wire layer maps that to 400)."""
        cfg = self._store.load()
        default_model = patch.default_model or cfg.default_model
        assignments = dict(cfg.assignments)
        if patch.roles:
            for role_str, key in patch.roles.items():
                assignments[ModelRole(role_str)] = key  # ValueError on a bad role
        vision_model = (
            patch.vision_model
            if "vision_model" in patch.model_fields_set
            else cfg.vision_escalation_model
        )
        new_cfg = self._store.sections.save_assignments(
            default_model,
            assignments,
            vision_escalation_model=vision_model,
        )
        return _assignments_from(self._retire_placeholder_driver(new_cfg))

    def _retire_placeholder_driver(self, cfg: RouterConfig) -> RouterConfig:
        """Drop the first-run stand-in once a real model leads.

        It has no endpoint and cannot answer a request, so leaving it in the
        Model library after setup offered an entry that only ever fails (UI-29).
        Kept while anything still points at it — an orphaned assignment would be
        a worse outcome than a stale row.
        """
        if cfg.default_model == _PLACEHOLDER_DRIVER or _PLACEHOLDER_DRIVER not in cfg.models:
            return cfg
        if _PLACEHOLDER_DRIVER in cfg.assignments.values():
            return cfg
        if cfg.vision_escalation_model == _PLACEHOLDER_DRIVER:
            return cfg
        remaining = {k: v for k, v in cfg.models.items() if k != _PLACEHOLDER_DRIVER}
        return self._store.save(cfg.model_copy(update={"models": remaining}))
