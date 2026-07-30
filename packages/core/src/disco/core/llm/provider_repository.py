"""Provider persistence and source-compatible configuration adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from ..origin_approvals import OriginApprovalStore
from .config import (
    EncodersSettings,
    ExtractionSettings,
    ImageGenSettings,
    LiveBrowserSettings,
    McpSettings,
    ModelEntry,
    ProjectStorageSettings,
    ProviderSettings,
    RoleFallbackSettings,
    RouterConfig,
    SandboxSettings,
    SearchSettings,
    TtsSettings,
)
from .config_repository import _VersionedSidecar
from .secrets import SecretStore
from .types import ModelRole


class ProviderRepository(_VersionedSidecar):
    """Independent owner of provider connection metadata."""

    def __init__(self, router_path: Path) -> None:
        from .config_repository import _sidecar_path

        super().__init__(_sidecar_path(router_path, "providers"), "providers")

    def load(self) -> dict[str, object] | None:
        return self._load()

    def save(self, providers: Mapping[str, ProviderSettings]) -> None:
        self._save({key: provider.model_dump(mode="json") for key, provider in providers.items()})


class _ConfigStoreAdapterBase:
    _path: Path

    def _update_general(self, change: Callable[[RouterConfig], RouterConfig]) -> RouterConfig:
        raise NotImplementedError

    def _update_router(self, change: Callable[[RouterConfig], RouterConfig]) -> RouterConfig:
        raise NotImplementedError

    def _replace_providers(self, providers: Mapping[str, ProviderSettings]) -> RouterConfig:
        raise NotImplementedError


class _OriginApprovalAdapter(_ConfigStoreAdapterBase):
    def approval_store(self, *, secret_store: SecretStore | None = None) -> OriginApprovalStore:
        return OriginApprovalStore(config_path=self._path, secret_store=secret_store)

    def origin_approved(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
        *,
        secret_store: SecretStore | None = None,
    ) -> bool:
        return self.approval_store(secret_store=secret_store).is_approved(url, purpose, secret_ref)

    def approve_origin(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.approval_store(secret_store=secret_store).approve(url, purpose, secret_ref)

    def replace_origin_purpose(
        self,
        url: str,
        purpose: str,
        secret_refs: tuple[str, ...] = ("",),
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.approval_store(secret_store=secret_store).replace_purpose(url, purpose, secret_refs)

    def revoke_origin_purpose(
        self,
        purpose: str,
        *,
        secret_store: SecretStore | None = None,
    ) -> int:
        return self.approval_store(secret_store=secret_store).revoke_purpose(purpose)


class _GeneralSettingsAdapter(_ConfigStoreAdapterBase):
    def save_sandbox(self, sandbox: SandboxSettings) -> RouterConfig:
        def change(config: RouterConfig) -> RouterConfig:
            merged = sandbox.with_preserved_connections(config.sandbox)
            return config.model_copy(update={"sandbox": merged})

        return self._update_general(change)

    def save_encoders(self, encoders: EncodersSettings) -> RouterConfig:
        return self._update_general(lambda config: config.model_copy(update={"encoders": encoders}))

    def save_tts(self, tts: TtsSettings) -> RouterConfig:
        return self._update_general(lambda config: config.model_copy(update={"tts": tts}))

    def save_image_gen(self, image_gen: ImageGenSettings) -> RouterConfig:
        if image_gen.provider == "openrouter":
            image_gen = image_gen.model_copy(update={"base_url": "", "api_key_env": ""})
        return self._update_general(
            lambda config: config.model_copy(update={"image_gen": image_gen})
        )

    def save_search(self, search: SearchSettings) -> RouterConfig:
        if search.provider == "ddgs":
            search = search.model_copy(update={"base_url": ""})
        return self._update_general(lambda config: config.model_copy(update={"search": search}))

    def save_role_fallback(self, settings: RoleFallbackSettings) -> None:
        self._update_general(lambda config: config.model_copy(update={"role_fallback": settings}))

    def save_extraction(self, extraction: ExtractionSettings) -> RouterConfig:
        if extraction.provider == "local":
            extraction = extraction.model_copy(update={"base_url": ""})
        return self._update_general(
            lambda config: config.model_copy(update={"extraction": extraction})
        )

    def save_live_browser(self, live_browser: LiveBrowserSettings) -> RouterConfig:
        return self._update_general(
            lambda config: config.model_copy(update={"live_browser": live_browser})
        )

    def save_build_kernel(self, build_kernel: str) -> RouterConfig:
        normalized = "disco" if build_kernel != "disco" else build_kernel
        return self._update_general(
            lambda config: config.model_copy(update={"build_kernel": normalized})
        )

    def save_projects(self, projects: ProjectStorageSettings) -> RouterConfig:
        return self._update_general(lambda config: config.model_copy(update={"projects": projects}))

    def save_mcp(self, mcp: McpSettings) -> RouterConfig:
        return self._update_general(lambda config: config.model_copy(update={"mcp": mcp}))


class _ModelCatalogueAdapter(_ConfigStoreAdapterBase):
    def save_assignments(
        self, default_model: str, assignments: dict[ModelRole, str]
    ) -> RouterConfig:
        def change(config: RouterConfig) -> RouterConfig:
            if default_model not in config.models:
                raise ValueError(f"unknown default_model {default_model!r}")
            for role, key in assignments.items():
                if key not in config.models:
                    raise ValueError(f"unknown model {key!r} for role {role.value}")
            return config.model_copy(
                update={"default_model": default_model, "assignments": assignments}
            )

        return self._update_router(change)

    def add_model(self, key: str, entry: ModelEntry) -> RouterConfig:
        def change(config: RouterConfig) -> RouterConfig:
            if key in config.models:
                raise ValueError(f"model {key!r} already exists")
            return config.model_copy(update={"models": {**config.models, key: entry}})

        return self._update_router(change)

    def update_model(self, key: str, entry: ModelEntry) -> RouterConfig:
        def change(config: RouterConfig) -> RouterConfig:
            if key not in config.models:
                raise ValueError(f"unknown model {key!r}")
            return config.model_copy(update={"models": {**config.models, key: entry}})

        return self._update_router(change)

    def remove_model(self, key: str) -> RouterConfig:
        def change(config: RouterConfig) -> RouterConfig:
            if key not in config.models:
                raise ValueError(f"unknown model {key!r}")
            if key == config.default_model:
                raise ValueError(f"{key!r} is the default model — reassign the default first")
            used_by = [role.value for role, value in config.assignments.items() if value == key]
            if used_by:
                raise ValueError(f"{key!r} is assigned to {', '.join(used_by)} — reassign first")
            models = {
                model_key: value for model_key, value in config.models.items() if model_key != key
            }
            return config.model_copy(update={"models": models})

        return self._update_router(change)

    def save_providers(self, providers: Mapping[str, ProviderSettings]) -> RouterConfig:
        return self._replace_providers(providers)
