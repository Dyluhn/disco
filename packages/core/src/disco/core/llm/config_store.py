"""Thin compatibility aggregate over versioned configuration repositories.

The flattened router document remains a rollback-readable full snapshot. General
settings and provider metadata are independently authoritative sidecars; valid
legacy documents remain readable and migrate on the next material write.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from ..env import disco_env
from .config import (
    ProviderSettings,
    RouterConfig,
    apply_runtime_capabilities,
    default_config,
)
from .config_repository import (
    ConfigTransaction,
    GeneralSettingsRepository,
    RouterDocumentRepository,
)
from .config_security import migrate_secret_refs_and_trust, with_security_diagnostics
from .provider_repository import (
    ProviderRepository,
    _GeneralSettingsAdapter,
    _ModelCatalogueAdapter,
    _OriginApprovalAdapter,
)
from .types import ModelRole

_DEFAULT_PATH = "disco-config.json"


class ConfigStore(
    _OriginApprovalAdapter,
    _GeneralSettingsAdapter,
    _ModelCatalogueAdapter,
):
    """Coordinate bounded repositories behind the historical public API."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        base_factory: Callable[[], RouterConfig] = default_config,
    ) -> None:
        self._path = Path(path or disco_env("CONFIG", _DEFAULT_PATH))
        self._base_factory = base_factory
        self._router_repository = RouterDocumentRepository(self._path)
        self._settings_repository = GeneralSettingsRepository(self._path)
        self._provider_repository = ProviderRepository(self._path)
        self._transaction = ConfigTransaction(self._path)
        self._vision_probe: dict[str, bool | None] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def apply_vision_probe(self, results: dict[str, bool | None]) -> None:
        """Install process-lifetime capability facts without persisting them."""
        self._vision_probe = dict(results)

    def load(self) -> RouterConfig:
        """Load the compatible aggregate and apply process-only capability facts."""
        with self._transaction:
            config = self._load_unlocked(persist_migrations=True)
        return apply_runtime_capabilities(config, probe_results=self._vision_probe)

    def save(self, config: RouterConfig) -> RouterConfig:
        """Atomically persist a full compatible snapshot to all bounded owners."""
        config = self._gate_build_kernel(config)
        with self._transaction:
            self._persist_all(config)
        return config

    def _load_unlocked(self, *, persist_migrations: bool) -> RouterConfig:
        base = self._base_factory()
        raw = self._router_repository.load()
        build_kernel_changed = False
        if raw is None:
            config = base
        elif "models" in raw:
            normalized, build_kernel_changed = self._normalize_build_kernel_data(raw)
            try:
                config = RouterConfig.model_validate(normalized)
            except Exception:  # noqa: BLE001 - malformed legacy aggregate falls back
                config = base
                build_kernel_changed = False
        else:
            config = self._apply_overlay(base, raw)

        settings = self._settings_repository.load()
        providers = self._provider_repository.load()
        config = self._overlay_bounded_repositories(config, settings, providers)
        has_persisted_state = raw is not None or settings is not None or providers is not None

        if has_persisted_state:
            migrated, security_changed = migrate_secret_refs_and_trust(
                config,
                self.origin_approved,
            )
            config = migrated
            if persist_migrations and (build_kernel_changed or security_changed):
                self._persist_all(config)
        else:
            config = with_security_diagnostics(config, self.origin_approved)
        return self._gate_build_kernel(config)

    def _overlay_bounded_repositories(
        self,
        config: RouterConfig,
        settings: Mapping[str, object] | None,
        providers: Mapping[str, object] | None,
    ) -> RouterConfig:
        if settings is None and providers is None:
            return config
        payload = config.model_dump(mode="json")
        if settings is not None:
            payload.update(settings)
        if providers is not None:
            payload["providers"] = dict(providers)
        return RouterConfig.model_validate(payload)

    def _persist_all(self, config: RouterConfig) -> None:
        self._settings_repository.save(config)
        self._provider_repository.save(config.providers)
        self._router_repository.save(config)

    def _update_general(self, change: Callable[[RouterConfig], RouterConfig]) -> RouterConfig:
        with self._transaction:
            config = self._gate_build_kernel(change(self._load_unlocked(persist_migrations=False)))
            self._settings_repository.save(config)
            self._router_repository.save(config)
        return config

    def _update_router(self, change: Callable[[RouterConfig], RouterConfig]) -> RouterConfig:
        with self._transaction:
            config = self._gate_build_kernel(change(self._load_unlocked(persist_migrations=False)))
            self._router_repository.save(config)
        return config

    def _replace_providers(
        self,
        providers: Mapping[str, ProviderSettings],
    ) -> RouterConfig:
        with self._transaction:
            config = self._load_unlocked(persist_migrations=False).model_copy(
                update={"providers": dict(providers)}
            )
            self._provider_repository.save(config.providers)
            self._router_repository.save(config)
        return config

    def _normalize_build_kernel_data(
        self,
        data: Mapping[str, object],
    ) -> tuple[dict[str, object], bool]:
        normalized = dict(data)
        value = normalized.get("build_kernel")
        if value is None or value == "disco":
            return normalized, False
        normalized["build_kernel"] = "disco"
        return normalized, True

    def _gate_build_kernel(self, config: RouterConfig) -> RouterConfig:
        if config.build_kernel == "disco":
            return config
        return config.model_copy(update={"build_kernel": "disco"})

    def _apply_overlay(
        self,
        base: RouterConfig,
        overlay: Mapping[str, object],
    ) -> RouterConfig:
        default_model = overlay.get("default_model")
        if not isinstance(default_model, str) or default_model not in base.models:
            default_model = base.default_model
        assignments = dict(base.assignments)
        raw_assignments = overlay.get("assignments")
        if isinstance(raw_assignments, dict):
            for role_value, model_key in raw_assignments.items():
                role = _role(str(role_value))
                if role is not None and isinstance(model_key, str) and model_key in base.models:
                    assignments[role] = model_key
        return base.model_copy(update={"default_model": default_model, "assignments": assignments})


def _role(value: str) -> ModelRole | None:
    try:
        return ModelRole(value)
    except ValueError:
        return None
