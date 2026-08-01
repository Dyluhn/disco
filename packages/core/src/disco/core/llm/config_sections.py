"""Per-section persistence for the config document — the eleven `save_*`
writers that used to live on `ConfigStore`, sharing only the atomic
read/write primitive the document core (`load`/`save`) already owns.

Split out of `ConfigStore` (PY-0459): a config-file *writer* is a distinct
authority from the document-load/save core. Each `save_*` here loads the
current document through the owning store, applies exactly one section's
update, and persists it back — identical behaviour to when these were
`ConfigStore` methods, just relocated. `ConfigStore` exposes an instance of
this class as the plain attribute `sections` (not a `@property` — an
instance attribute costs nothing against the class's public-method count,
while a property would still count).
"""

from __future__ import annotations

from typing import Protocol

from .config import (
    EncodersSettings,
    ExtractionSettings,
    ImageGenSettings,
    LiveBrowserSettings,
    ProjectStorageSettings,
    RoleFallbackSettings,
    RouterConfig,
    SandboxSettings,
    SearchSettings,
    TtsSettings,
)
from .types import ModelRole


class _Document(Protocol):
    """The document-core surface a section writer needs: load + save.

    A `Protocol`, not a `from .config_store import ConfigStore` import, so
    this module never depends on `config_store` — `ConfigStore` depends on
    `config_sections`, not the other way around."""

    def load(self) -> RouterConfig: ...
    def save(self, config: RouterConfig) -> RouterConfig: ...


class ConfigSectionWriter:
    """Persists one section of the `RouterConfig` document at a time."""

    def __init__(self, document: _Document) -> None:
        self._document = document

    # -- sandbox backend ------------------------------------------------------

    def save_sandbox(self, sandbox: SandboxSettings) -> RouterConfig:
        """Persist the active sandbox backend + connection over the current config.

        PRESERVES every backend's saved connection block: the incoming settings carry
        the now-ACTIVE backend's flat fields, and `with_preserved_connections` folds in
        the previously-persisted per-backend map so switching the active backend never
        clears the inactive backends' setup (the gVisor-socket-blanking outage)."""
        previous = self._document.load().sandbox
        merged = sandbox.with_preserved_connections(previous)
        return self._document.save(self._document.load().model_copy(update={"sandbox": merged}))

    def save_encoders(self, encoders: EncodersSettings) -> RouterConfig:
        """Persist the encoder mode (bundled-local vs remote) over the current config.
        The agent-server reloads per-request, so a change drives the NEXT research run."""
        cfg = self._document.load()
        return self._document.save(cfg.model_copy(update={"encoders": encoders}))

    def save_tts(self, tts: TtsSettings) -> RouterConfig:
        """Persist the audio-overview TTS settings (toggle, bundled-vs-remote, voices).
        The agent-server reloads per-request; disabling it also frees the model."""
        cfg = self._document.load()
        return self._document.save(cfg.model_copy(update={"tts": tts}))

    def save_image_gen(self, image_gen: ImageGenSettings) -> RouterConfig:
        """Persist the image generation provider (comfyui/openai/openrouter — W-50: no
        bundled procedural tier). The agent-server reloads per-request; a change takes
        effect on the NEXT image-gen. OpenRouter has a FIXED origin + uses the reserved
        OpenRouter key, so its base_url/api_key_env are cleared — a stale value from
        another provider must never carry over and get the OpenRouter Bearer key sent to
        the wrong host."""
        if image_gen.provider == "openrouter":
            image_gen = image_gen.model_copy(update={"base_url": "", "api_key_env": ""})
        cfg = self._document.load()
        return self._document.save(cfg.model_copy(update={"image_gen": image_gen}))

    def save_search(self, search: SearchSettings) -> RouterConfig:
        """Persist the configured web-discovery provider over the config.

        When the provider is the bundled in-process tier (ddgs), the persisted
        base_url is cleared so a stale self-host LAN URL (e.g. from a previous
        searxng selection) cannot silently re-engage if the user later switches
        back to searxng without re-entering the URL."""
        if search.provider == "ddgs":
            search = search.model_copy(update={"base_url": ""})
        cfg = self._document.load()
        return self._document.save(cfg.model_copy(update={"search": search}))

    def save_role_fallback(self, settings: RoleFallbackSettings) -> None:
        """Persist auxiliary-role local fallback settings over the current config."""
        cfg = self._document.load()
        self._document.save(cfg.model_copy(update={"role_fallback": settings}))

    def save_extraction(self, extraction: ExtractionSettings) -> RouterConfig:
        """Persist the extraction provider (local/crawl4ai/firecrawl) over the config.

        When the provider is the bundled in-process tier (local), the persisted
        base_url is cleared so a stale self-host LAN URL (e.g. from a previous
        crawl4ai selection) cannot silently re-engage if the user later switches
        back to crawl4ai without re-entering the URL."""
        if extraction.provider == "local":
            extraction = extraction.model_copy(update={"base_url": ""})
        cfg = self._document.load()
        return self._document.save(cfg.model_copy(update={"extraction": extraction}))

    def save_live_browser(self, live_browser: LiveBrowserSettings) -> RouterConfig:
        """Persist the live-browser enable toggle. The agent-server reloads per-request."""
        return self._document.save(
            self._document.load().model_copy(update={"live_browser": live_browser})
        )

    def save_build_kernel(self, build_kernel: str) -> RouterConfig:
        """Persist the vestigial Build kernel selector after legacy normalization."""
        build_kernel = "disco" if build_kernel != "disco" else build_kernel
        return self._document.save(
            self._document.load().model_copy(update={"build_kernel": build_kernel})
        )

    # -- Build-project persistence --------------------------------------------

    def save_projects(self, projects: ProjectStorageSettings) -> RouterConfig:
        """Persist the user-chosen Build-project storage path over the current config.
        Same atomic write as save_sandbox; the agent-server reloads per-request so a
        new path drives the NEXT snapshot/rehydrate."""
        return self._document.save(self._document.load().model_copy(update={"projects": projects}))

    # -- assignments ----------------------------------------------------------

    def save_assignments(
        self, default_model: str, assignments: dict[ModelRole, str]
    ) -> RouterConfig:
        """Persist new assignments over the current catalogue. Raises ValueError on
        a key that isn't in the catalogue (routing couldn't resolve it)."""
        cfg = self._document.load()
        if default_model not in cfg.models:
            raise ValueError(f"unknown default_model {default_model!r}")
        for role, key in assignments.items():
            if key not in cfg.models:
                raise ValueError(f"unknown model {key!r} for role {role.value}")
        return self._document.save(
            cfg.model_copy(update={"default_model": default_model, "assignments": assignments})
        )
