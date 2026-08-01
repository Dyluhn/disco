"""Model-catalogue repository split out of ConfigState (PY-0365): the
absolute, manual model story — add/edit/remove catalogue entries in the
shared `ConfigStore` document, persisted immediately. `ConfigState` exposes an
instance of this class as the plain `models` attribute (not a `@property` —
an instance attribute costs nothing against the class's public-method count,
while a property would still count).
"""

from __future__ import annotations

from typing import Any

from disco.core.llm import ConfigStore, SecretStore

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import ModelDTO, ModelUpsert
from .config.mappers import _entry_from, _models_from


class ConfigModelCatalogue:
    """CRUD over the model catalogue held in the shared `ConfigStore` document."""

    def __init__(self, store: ConfigStore, secrets: SecretStore) -> None:
        self._store = store
        self._secrets = secrets

    def _approve_model_origin(self, entry: Any, model_id: str | None = None) -> None:
        _origin_wiring.approve_model_origin(self._store, self._secrets, entry, model_id=model_id)

    def models(self) -> list[ModelDTO]:
        return _models_from(self._store.load())

    def add_model(self, upsert: ModelUpsert) -> list[ModelDTO]:
        """Add a model to the catalogue. New models are their own endpoint (the
        endpoint key = the catalogue id). Raises ValueError on a duplicate id."""
        entry = _entry_from(upsert, provider=upsert.id)
        self._store.add_model(upsert.id, entry)
        self._approve_model_origin(entry, model_id=upsert.id)
        return _models_from(self._store.load())

    def update_model(self, model_id: str, upsert: ModelUpsert) -> list[ModelDTO]:
        """Edit an existing model. Preserves its endpoint key so a seeded model
        sharing a backend isn't silently split off. Raises ValueError if missing."""
        existing = self._store.load().models.get(model_id)
        provider = existing.provider if existing is not None else model_id
        entry = _entry_from(upsert, provider=provider)
        self._store.update_model(model_id, entry)
        self._approve_model_origin(entry, model_id=model_id)
        return _models_from(self._store.load())

    def remove_model(self, model_id: str) -> list[ModelDTO]:
        """Remove a model. Raises ValueError if it's the default or assigned to a
        role (the user must reassign first — we never silently break routing)."""
        return _models_from(self._store.remove_model(model_id))
