"""Bounded model, provider, secret, and skill settings services."""

from __future__ import annotations

from typing import Any

from disco.core import SkillStore
from disco.core.llm import ConfigStore, SecretStore
from disco.core.llm.config import ProviderSettings
from disco.core.stripe_host_service import STRIPE_SECRET_REF

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import (
    ModelDTO,
    ModelUpsert,
    OpenRouterKeyStatus,
    ProviderCatalogueModelDTO,
    ProviderCreate,
    ProviderDTO,
    ProviderEnableBody,
    ProviderPatch,
    SecretsListDTO,
    SecretStatus,
    SkillCreate,
    SkillDTO,
    SkillPatch,
)
from .config.mappers import _entry_from, _models_from
from .provider_config_service import ProviderConfigService


class _ConfigStateAdapterPort:
    _store: ConfigStore
    _secrets: SecretStore
    _skills_store: SkillStore
    _provider_config: ProviderConfigService


class ModelConfigStateAdapter(_ConfigStateAdapterPort):
    def _approve_origin(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
    ) -> None:
        _origin_wiring.sign_origin(self._store, self._secrets, url, purpose, secret_ref)

    def approve_origin(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
    ) -> None:
        _origin_wiring.approve_origin(self._store, self._secrets, url, purpose, secret_ref)

    def _approve_model_origin(self, entry: Any, model_id: str | None = None) -> None:
        _origin_wiring.approve_model_origin(
            self._store,
            self._secrets,
            entry,
            model_id=model_id,
        )

    def models(self) -> list[ModelDTO]:
        return _models_from(self._store.load())

    def add_model(self, upsert: ModelUpsert) -> list[ModelDTO]:
        entry = _entry_from(upsert, provider=upsert.id)
        self._store.add_model(upsert.id, entry)
        self._approve_model_origin(entry, model_id=upsert.id)
        return _models_from(self._store.load())

    def update_model(self, model_id: str, upsert: ModelUpsert) -> list[ModelDTO]:
        existing = self._store.load().models.get(model_id)
        provider = existing.provider if existing is not None else model_id
        entry = _entry_from(upsert, provider=provider)
        self._store.update_model(model_id, entry)
        self._approve_model_origin(entry, model_id=model_id)
        return _models_from(self._store.load())

    def remove_model(self, model_id: str) -> list[ModelDTO]:
        return _models_from(self._store.remove_model(model_id))


class ProviderConfigStateAdapter(_ConfigStateAdapterPort):
    def _provider_dto(self, provider: ProviderSettings) -> ProviderDTO:
        return self._provider_config._provider_dto(provider)

    def _save_providers(self, providers: dict[str, ProviderSettings]) -> None:
        self._provider_config._save_providers(providers)

    def providers(self) -> list[ProviderDTO]:
        return self._provider_config.providers()

    def provider_settings(self, provider_id: str) -> ProviderSettings:
        return self._provider_config.provider_settings(provider_id)

    def provider_origin_approved(self, provider: ProviderDTO) -> bool:
        return self._provider_config.provider_origin_approved(provider)

    def _unique_provider_id(self, label: str) -> str:
        return self._provider_config._unique_provider_id(label)

    def create_provider(self, body: ProviderCreate) -> ProviderDTO:
        return self._provider_config.create_provider(body)

    def update_provider(self, provider_id: str, patch: ProviderPatch) -> ProviderDTO:
        return self._provider_config.update_provider(provider_id, patch)

    def delete_provider(self, provider_id: str) -> None:
        self._provider_config.delete_provider(provider_id)

    def _unique_provider_catalogue_id(
        self,
        provider_id: str,
        model_id: str,
        label: str | None,
    ) -> str:
        return self._provider_config._unique_provider_catalogue_id(
            provider_id,
            model_id,
            label,
        )

    def enable_provider_model(
        self,
        provider_id: str,
        body: ProviderEnableBody,
        catalogue_model: ProviderCatalogueModelDTO | None = None,
    ) -> list[ModelDTO]:
        return self._provider_config.enable_provider_model(
            provider_id,
            body,
            catalogue_model,
        )

    def disable_provider_model(
        self,
        provider_id: str,
        catalogue_id: str,
    ) -> list[ModelDTO]:
        return self._provider_config.disable_provider_model(provider_id, catalogue_id)


class SecretConfigStateAdapter(_ConfigStateAdapterPort):
    _RESERVED_SECRETS = frozenset({"openrouter", STRIPE_SECRET_REF})

    @classmethod
    def _reserved_secret(cls, name: str) -> bool:
        return name in cls._RESERVED_SECRETS or name.startswith(
            ("stripe.binding.", "stripe.webhook.")
        )

    def openrouter_key_status(self) -> OpenRouterKeyStatus:
        present = self._secrets.has_openrouter_key()
        usable = present and bool(self._secrets.get_openrouter_key())
        return OpenRouterKeyStatus(
            configured=present,
            locked=present and not usable,
            can_store=self._secrets.can_store,
        )

    def set_openrouter_key(self, key: str) -> OpenRouterKeyStatus:
        if not key.strip():
            raise ValueError("key is empty")
        try:
            self._secrets.set_openrouter_key(key.strip())
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return self.openrouter_key_status()

    def clear_openrouter_key(self) -> OpenRouterKeyStatus:
        self._secrets.clear_openrouter_key()
        return self.openrouter_key_status()

    def list_secrets(self) -> SecretsListDTO:
        names = [name for name in self._secrets.secret_names() if not self._reserved_secret(name)]
        locked_names = [
            name for name in self._secrets.undecryptable_names() if not self._reserved_secret(name)
        ]
        return SecretsListDTO(
            names=sorted(names),
            locked_names=sorted(locked_names),
            locked=bool(locked_names),
            can_store=self._secrets.can_store,
        )

    def secret_status(self, name: str) -> SecretStatus:
        return SecretStatus(
            name=name,
            configured=self._secrets.has_secret(name),
            locked=self._secrets.locked,
            can_store=self._secrets.can_store,
        )

    def set_secret(self, name: str, value: str) -> SecretStatus:
        from disco.core.llm.secret_refs import is_control_secret_ref

        name = name.strip()
        if self._reserved_secret(name):
            route = "/api/openrouter/key" if name == "openrouter" else "/api/stripe/config/{app}"
            raise ValueError(f"use the dedicated {route} route for the {name} credential")
        if is_control_secret_ref(name):
            raise ValueError(f"{name} is an internal control secret and cannot be a provider ref")
        if not value.strip():
            raise ValueError("value is empty")
        try:
            self._secrets.set_secret(name, value.strip())
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return self.secret_status(name)

    def clear_secret(self, name: str) -> SecretStatus:
        from disco.core.llm.secret_refs import is_control_secret_ref

        name = name.strip()
        if self._reserved_secret(name):
            route = "/api/openrouter/key" if name == "openrouter" else "/api/stripe/config/{app}"
            raise ValueError(f"use the dedicated {route} route for the {name} credential")
        if is_control_secret_ref(name):
            raise ValueError(f"{name} is an internal control secret and cannot be cleared")
        self._secrets.clear_secret(name)
        return self.secret_status(name)


class SkillConfigStateAdapter(_ConfigStateAdapterPort):
    @staticmethod
    def _skill_dto(skill: Any) -> SkillDTO:
        return SkillDTO(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            enabled=skill.enabled,
            body=skill.body,
            surfaces=skill.surfaces,
        )

    def skills(self) -> list[SkillDTO]:
        return [self._skill_dto(skill) for skill in self._skills_store.list()]

    def create_skill(self, create: SkillCreate) -> SkillDTO:
        skill = self._skills_store.create(
            name=create.name,
            description=create.description,
            body=create.body,
            enabled=create.enabled,
            surfaces=create.surfaces,
        )
        return self._skill_dto(skill)

    def update_skill(self, skill_id: str, patch: SkillPatch) -> SkillDTO | None:
        existing = self._skills_store.get(skill_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                key: value
                for key, value in {
                    "name": patch.name,
                    "description": patch.description,
                    "enabled": patch.enabled,
                    "body": patch.body,
                    "surfaces": patch.surfaces,
                }.items()
                if value is not None
            }
        )
        self._skills_store.save(updated)
        return self._skill_dto(updated)

    def delete_skill(self, skill_id: str) -> bool:
        return self._skills_store.delete(skill_id)
