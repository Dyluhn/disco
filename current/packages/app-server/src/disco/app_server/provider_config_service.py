"""Provider settings service extracted from ConfigState.

Keeps provider CRUD, encrypted provider keys, and provider-catalogue toggles
behind the existing ConfigStore/SecretStore plus origin_approval_wiring
chokepoints. ConfigState retains the public/private API as thin delegators.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from disco.core.llm import ConfigStore, SecretStore
from disco.core.llm.config import ProviderSettings, RouterConfig

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


def _provider_already_has_model(
    cfg: RouterConfig, provider: ProviderSettings, model_id: str
) -> bool:
    return any(
        entry.api_key_env == provider.secret_name and entry.model_id == model_id
        for entry in cfg.models.values()
    )


def _resolved_label(
    body: ProviderEnableBody, catalogue_model: ProviderCatalogueModelDTO | None
) -> str | None:
    return body.label or (catalogue_model.label if catalogue_model else None)


def _resolved_context_window(
    body: ProviderEnableBody, catalogue_model: ProviderCatalogueModelDTO | None
) -> int | None:
    if body.context_window is not None:
        return body.context_window
    return catalogue_model.context_window if catalogue_model else None


def _resolved_max_output_tokens(
    body: ProviderEnableBody, catalogue_model: ProviderCatalogueModelDTO | None
) -> int | None:
    if body.max_output_tokens is not None:
        return body.max_output_tokens
    return catalogue_model.max_output_tokens if catalogue_model else None


def _resolved_vision_declared(
    catalogue_model: ProviderCatalogueModelDTO | None,
) -> bool | None:
    if catalogue_model is None:
        return None
    if catalogue_model.vision_status == "vision":
        return True
    if catalogue_model.vision_status == "text-only":
        return False
    return None


def _resolved_capabilities(
    catalogue_model: ProviderCatalogueModelDTO | None, context_window: int
) -> list[str]:
    # Capabilities: provider catalogues rarely report them (Go reports none),
    # and an EMPTY set silently disqualifies the model from the Build/agent
    # driver pickers (tool-calling gate) — the enable "works" but the model
    # only surfaces in capability-agnostic roles, which reads as broken.
    # Default modern-serving table stakes (tool_calling + json_mode; +
    # long_context per its >~64k semantics); a genuinely tool-less model
    # fails loudly at run time, which is diagnosable — invisibility is not.
    # ANCHORED_EDIT stays opt-in by design (unknown models must not get it).
    capabilities = catalogue_model.capabilities if catalogue_model else []
    if capabilities:
        return capabilities
    long_context = ["long_context"] if context_window >= 65536 else []
    return ["tool_calling", "json_mode"] + long_context


def _resolved_prices(
    catalogue_model: ProviderCatalogueModelDTO | None,
) -> tuple[float, float, bool]:
    # Pricing: only claim a pay model the catalogue actually reported.
    # Absent pricing → "unknown" (rendered as such), never a fake Free.
    price_in = catalogue_model.price_in_per_m if catalogue_model is not None else None
    price_out = catalogue_model.price_out_per_m if catalogue_model is not None else None
    prices_known = price_in is not None or price_out is not None
    return (
        price_in if price_in is not None else 0.0,
        price_out if price_out is not None else 0.0,
        prices_known,
    )


def _validated_provider_base_url(raw: str) -> str:
    """Normalize and validate a provider endpoint once for create and update."""
    base_url = raw.strip().rstrip("/")
    if not base_url:
        raise ValueError("base_url is empty")
    from disco.core.host_egress import origin_for_url

    try:
        valid_origin = origin_for_url(base_url)
    except ValueError as exc:
        raise ValueError("base_url must be an absolute HTTP(S) URL") from exc
    if valid_origin is None:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    return base_url


class ProviderInUseError(Exception):
    """Raised when deleting a provider would strand catalogue models."""

    def __init__(self, model_names: list[str]) -> None:
        super().__init__(", ".join(model_names))
        self.model_names = model_names


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
            requires_api_key=provider.requires_api_key,
        )

    def _save_providers(self, providers: dict[str, ProviderSettings]) -> None:
        cfg = self._store.load()
        self._store.save(cfg.model_copy(update={"providers": providers}))

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
        api_key = body.api_key.strip()
        if not label:
            raise ValueError("label is empty")
        base_url = _validated_provider_base_url(body.base_url)
        if body.requires_api_key and not api_key:
            raise ValueError("api_key is empty")
        provider_id = self._unique_provider_id(label)
        secret_name = f"provider_{provider_id}"
        if api_key:
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
            requires_api_key=body.requires_api_key,
        )
        self._store.save(
            cfg.model_copy(update={"providers": {**cfg.providers, provider_id: provider}})
        )
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
            updates["base_url"] = _validated_provider_base_url(patch.base_url)
        if patch.kind is not None:
            updates["kind"] = patch.kind
        if patch.requires_api_key is not None:
            updates["requires_api_key"] = patch.requires_api_key
        if patch.api_key is not None:
            api_key = patch.api_key.strip()
            if not api_key:
                raise ValueError("api_key is empty")
            try:
                self._secrets.set_secret(provider.secret_name, api_key)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
        updated = provider.model_copy(update=updates)
        if updated.requires_api_key and not (
            patch.api_key or self._resolve_secret_value(provider.secret_name)
        ):
            raise ValueError("api_key is empty")
        self._store.save(
            cfg.model_copy(update={"providers": {**cfg.providers, provider_id: updated}})
        )
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
        self._store.save(cfg.model_copy(update={"providers": providers}))
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
        if _provider_already_has_model(cfg, provider, model_id):
            return _models_from(cfg)
        catalogue_id = self._unique_provider_catalogue_id(
            provider.id, model_id, _resolved_label(body, catalogue_model)
        )
        # Context: NEVER silently default — context_window drives the engine's
        # context budgeting, so a wrong-low guess (the old 8192) over-snips every
        # build on a big model. The caller must supply it when the provider's
        # catalogue doesn't report one.
        context_window = _resolved_context_window(body, catalogue_model)
        if context_window is None:
            raise ValueError(
                f"context window required: {provider.label} does not report context "
                "windows — pass context_window with the model's real limit"
            )
        price_in, price_out, prices_known = _resolved_prices(catalogue_model)
        upsert = ModelUpsert(
            id=catalogue_id,
            model_id=model_id,
            base_url=provider.base_url,
            api_key_env=provider.secret_name,
            context_window=context_window,
            max_output_tokens=_resolved_max_output_tokens(body, catalogue_model),
            capabilities=_resolved_capabilities(catalogue_model, context_window),
            # Provider catalogue metadata seeds the advisory capability set, not
            # the operator's manual override. Keep the pin unset so a live probe
            # can correct stale provider metadata; only Settings may force it.
            vision=body.vision,
            vision_declared=_resolved_vision_declared(catalogue_model),
            requires_api_key=provider.requires_api_key,
            price_in_per_m=price_in,
            price_out_per_m=price_out,
            pricing_mode="metered" if prices_known else "unknown",
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
