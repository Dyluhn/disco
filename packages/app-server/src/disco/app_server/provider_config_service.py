"""Provider settings service extracted from ConfigState.

Keeps provider CRUD, encrypted provider keys, and provider-catalogue toggles
behind the existing ConfigStore/SecretStore plus origin_approval_wiring
chokepoints. ConfigState retains the public/private API as thin delegators.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from disco.core.llm import ConfigStore, SecretStore
from disco.core.llm.config import ProviderSettings

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import (
    ModelDTO,
    ModelUpsert,
    ProviderCatalogueModelDTO,
    ProviderCreate,
    ProviderDTO,
    ProviderEnableBody,
    ProviderPatch,
)
from .config.mappers import _models_from


class ProviderInUseError(Exception):
    """Raised when deleting a provider would strand catalogue models."""

    def __init__(self, model_names: list[str]) -> None:
        super().__init__(", ".join(model_names))
        self.model_names = model_names


def _provider_context_window(
    body: ProviderEnableBody,
    catalogue_model: ProviderCatalogueModelDTO | None,
    provider_label: str,
) -> int:
    context_window = body.context_window
    if context_window is None and catalogue_model is not None:
        context_window = catalogue_model.context_window
    if context_window is None:
        raise ValueError(
            f"context window required: {provider_label} does not report context "
            "windows — pass context_window with the model's real limit"
        )
    return context_window


def _provider_capabilities(
    context_window: int,
    catalogue_model: ProviderCatalogueModelDTO | None,
) -> list[str]:
    capabilities = list(catalogue_model.capabilities) if catalogue_model else []
    if capabilities:
        return capabilities
    defaults = ["tool_calling", "json_mode"]
    if context_window >= 65536:
        defaults.append("long_context")
    return defaults


def _provider_prices(
    catalogue_model: ProviderCatalogueModelDTO | None,
) -> tuple[Literal["metered", "unknown"], float, float]:
    if catalogue_model is None:
        return ("unknown", 0.0, 0.0)
    known = (
        catalogue_model.price_in_per_m is not None or catalogue_model.price_out_per_m is not None
    )
    return (
        "metered" if known else "unknown",
        catalogue_model.price_in_per_m or 0.0,
        catalogue_model.price_out_per_m or 0.0,
    )


class ProviderConfigService:
    """Provider config and catalogue mutations over the shared config stores."""

    def __init__(
        self,
        store: ConfigStore,
        secrets: SecretStore,
        *,
        add_model: Callable[[ModelUpsert], list[ModelDTO]],
        remove_model: Callable[[str], list[ModelDTO]],
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._add_model = add_model
        self._remove_model = remove_model

    def _provider_dto(self, provider: ProviderSettings) -> ProviderDTO:
        return ProviderDTO(
            id=provider.id,
            label=provider.label,
            base_url=provider.base_url,
            kind=provider.kind,
            secret_name=provider.secret_name,
            has_key=bool(self._resolve_secret_value(provider.secret_name)),
        )

    def _save_providers(self, providers: dict[str, ProviderSettings]) -> None:
        self._store.save_providers(providers)

    def providers(self) -> list[ProviderDTO]:
        return [self._provider_dto(p) for p in self._store.load().providers.values()]

    def provider_settings(self, provider_id: str) -> ProviderSettings:
        provider = self._store.load().providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        return provider

    def provider_origin_approved(self, provider: ProviderDTO) -> bool:
        return _origin_wiring.provider_origin_approved(self._store, self._secrets, provider)

    def _unique_provider_id(self, label: str) -> str:
        import re

        base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "provider"
        existing = self._store.load().providers
        if base not in existing:
            return base
        i = 2
        while f"{base}-{i}" in existing:
            i += 1
        return f"{base}-{i}"

    def create_provider(self, body: ProviderCreate) -> ProviderDTO:
        label = body.label.strip()
        base_url = body.base_url.strip().rstrip("/")
        api_key = body.api_key.strip()
        if not label:
            raise ValueError("label is empty")
        if not base_url:
            raise ValueError("base_url is empty")
        from disco.core.host_egress import origin_for_url

        try:
            valid_origin = origin_for_url(base_url)
        except ValueError as exc:
            raise ValueError("base_url must be an absolute HTTP(S) URL") from exc
        if valid_origin is None:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if not api_key:
            raise ValueError("api_key is empty")
        provider_id = self._unique_provider_id(label)
        secret_name = f"provider_{provider_id}"
        try:
            self._secrets.set_secret(secret_name, api_key)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        cfg = self._store.load()
        provider = ProviderSettings(
            id=provider_id,
            label=label,
            base_url=base_url,
            kind=body.kind,
            secret_name=secret_name,
        )
        self._store.save_providers({**cfg.providers, provider_id: provider})
        _origin_wiring.approve_provider_origin(self._store, self._secrets, provider)
        return self._provider_dto(provider)

    def update_provider(self, provider_id: str, patch: ProviderPatch) -> ProviderDTO:
        cfg = self._store.load()
        provider = cfg.providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        updates: dict[str, Any] = {}
        if patch.label is not None:
            label = patch.label.strip()
            if not label:
                raise ValueError("label is empty")
            updates["label"] = label
        if patch.base_url is not None:
            base_url = patch.base_url.strip().rstrip("/")
            if not base_url:
                raise ValueError("base_url is empty")
            from disco.core.host_egress import origin_for_url

            try:
                valid_origin = origin_for_url(base_url)
            except ValueError as exc:
                raise ValueError("base_url must be an absolute HTTP(S) URL") from exc
            if valid_origin is None:
                raise ValueError("base_url must be an absolute HTTP(S) URL")
            updates["base_url"] = base_url
        if patch.kind is not None:
            updates["kind"] = patch.kind
        if patch.api_key is not None:
            api_key = patch.api_key.strip()
            if not api_key:
                raise ValueError("api_key is empty")
            try:
                self._secrets.set_secret(provider.secret_name, api_key)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
        updated = provider.model_copy(update=updates)
        self._store.save_providers({**cfg.providers, provider_id: updated})
        # A base-URL save or explicit key rotation is the operator approval
        # gesture. Cosmetic edits never bless a previously unapproved origin.
        if patch.base_url is not None or patch.api_key is not None:
            _origin_wiring.approve_provider_origin(self._store, self._secrets, updated)
        return self._provider_dto(updated)

    def delete_provider(self, provider_id: str) -> None:
        cfg = self._store.load()
        provider = cfg.providers.get(provider_id)
        if provider is None:
            raise KeyError(provider_id)
        refs = [
            f"{key} ({entry.model_id})"
            for key, entry in cfg.models.items()
            if entry.api_key_env == provider.secret_name
        ]
        if refs:
            raise ProviderInUseError(refs)
        providers = {k: v for k, v in cfg.providers.items() if k != provider_id}
        self._store.save_providers(providers)
        self._secrets.clear_secret(provider.secret_name)

    def _unique_provider_catalogue_id(
        self, provider_id: str, model_id: str, label: str | None
    ) -> str:
        import re

        stem = label.strip() if label and label.strip() else model_id
        slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "model"
        base = f"prov-{provider_id}-{slug}"
        existing = self._store.load().models
        if base not in existing:
            return base
        i = 2
        while f"{base}-{i}" in existing:
            i += 1
        return f"{base}-{i}"

    def enable_provider_model(
        self,
        provider_id: str,
        body: ProviderEnableBody,
        catalogue_model: ProviderCatalogueModelDTO | None = None,
    ) -> list[ModelDTO]:
        provider = self.provider_settings(provider_id)
        model_id = body.model_id.strip()
        if not model_id:
            raise ValueError("model_id is empty")
        cfg = self._store.load()
        for entry in cfg.models.values():
            if entry.api_key_env == provider.secret_name and entry.model_id == model_id:
                return _models_from(cfg)
        catalogue_id = self._unique_provider_catalogue_id(
            provider.id,
            model_id,
            body.label or (catalogue_model.label if catalogue_model else None),
        )
        context_window = _provider_context_window(body, catalogue_model, provider.label)
        capabilities = _provider_capabilities(context_window, catalogue_model)
        pricing_mode, price_in_per_m, price_out_per_m = _provider_prices(catalogue_model)
        upsert = ModelUpsert(
            id=catalogue_id,
            model_id=model_id,
            base_url=provider.base_url,
            api_key_env=provider.secret_name,
            context_window=context_window,
            max_output_tokens=(
                body.max_output_tokens
                if body.max_output_tokens is not None
                else (catalogue_model.max_output_tokens if catalogue_model else None)
            ),
            capabilities=capabilities,
            price_in_per_m=price_in_per_m,
            price_out_per_m=price_out_per_m,
            pricing_mode=pricing_mode,
        )
        return self._add_model(upsert)

    def disable_provider_model(self, provider_id: str, catalogue_id: str) -> list[ModelDTO]:
        provider = self.provider_settings(provider_id)
        cfg = self._store.load()
        entry = cfg.models.get(catalogue_id)
        if entry is None:
            raise ValueError(f"unknown model {catalogue_id!r}")
        if entry.api_key_env != provider.secret_name:
            raise ValueError(f"{catalogue_id!r} does not belong to provider {provider_id!r}")
        return self._remove_model(catalogue_id)

    def _resolve_secret_value(self, name: str) -> str | None:
        from disco.core.llm.secret_refs import resolve_provider_secret

        return resolve_provider_secret(name, self._secrets)
