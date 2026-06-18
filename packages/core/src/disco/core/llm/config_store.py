"""Persistent, shared model config — the single source of truth for the model
catalogue AND the per-role assignments, so Settings (add/edit/remove a model,
reassign a role) actually changes what the running system calls.

The full `RouterConfig` is persisted as JSON at `PMX_CONFIG`; `default_config()`
seeds it on first run, after which the file is authoritative (user edits are not
overwritten by code defaults). Both servers point at the same file: the app-server
writes it, the agent-server reads it per request. Writes are atomic (temp +
rename); a missing/corrupt file falls back to the seed rather than crashing.

Back-compat: an older file that held only the assignment overlay
({default_model, assignments}) is still honored over the seed catalogue, and is
migrated to the full format on the next write.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from ..env import disco_env
from .config import (
    EncodersSettings,
    ExtractionSettings,
    ImageGenSettings,
    LiveBrowserSettings,
    ModelEntry,
    ProjectStorageSettings,
    RouterConfig,
    SandboxSettings,
    SearchSettings,
    TtsSettings,
    apply_runtime_capabilities,
    default_config,
)
from .types import ModelRole

_ENV_PATH = "DISCO_CONFIG"
_DEFAULT_PATH = "disco-config.json"


class ConfigStore:
    """Loads/saves the full RouterConfig (catalogue + assignments)."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        base_factory: Callable[[], RouterConfig] = default_config,
    ) -> None:
        self._path = Path(path or disco_env("CONFIG", _DEFAULT_PATH))
        self._base_factory = base_factory
        # V2/V4 (§2): process-lifetime overlay of the async vision-probe results
        # (model catalogue key → True/False/None), produced ONCE at server startup
        # by wiring.probe_all_vision and installed via `apply_vision_probe`. None
        # until a probe has run — load() then behaves exactly as before (static
        # table + DRIVER_VISION env). config_store stays httpx-free: the network
        # probe lives in wiring.py; we only carry the already-computed dict here so
        # every per-request load() reflects a model server's REAL vision modality
        # (llama.cpp /props.modalities.vision, OpenRouter input_modalities) over the
        # static table.
        self._vision_probe: dict[str, bool | None] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def apply_vision_probe(self, results: dict[str, bool | None]) -> None:
        """V2/V4 (§2): install the startup vision-probe results as a process-lifetime
        overlay. Every subsequent `load()` passes them to `apply_runtime_capabilities`
        so a definitive probe (True/False) overrides the static table; a None entry is
        ignored (falls through to the table) and is harmless. Idempotent; called once
        from the agent-server lifespan after `wiring.probe_all_vision`."""
        self._vision_probe = results

    def load(self) -> RouterConfig:
        """The persisted config, or the seed if absent/corrupt. A legacy overlay
        file (assignments only) is applied over the seed catalogue. Runtime
        capability overlays (PMX_DRIVER_VISION, [BP-00]) apply over EVERY path —
        the file is authoritative for user edits, the env for live deployment
        facts, so a pre-vision file cannot pin the driver text-only."""
        base = self._base_factory()
        data = self._read()
        if data is None:
            cfg = base
        elif "models" in data:  # full config
            try:
                cfg = RouterConfig.model_validate(data)
            except Exception:  # noqa: BLE001 — a corrupt/stale file must not crash routing
                cfg = base
        else:
            cfg = self._apply_overlay(base, data)  # legacy {default_model, assignments}
        return apply_runtime_capabilities(cfg, probe_results=self._vision_probe)

    def save(self, config: RouterConfig) -> RouterConfig:
        """Persist the full config (atomically) and return it."""
        self._write(config.model_dump(mode="json"))
        return config

    # -- sandbox backend ------------------------------------------------------

    def save_sandbox(self, sandbox: SandboxSettings) -> RouterConfig:
        """Persist the active sandbox backend + connection over the current config."""
        return self.save(self.load().model_copy(update={"sandbox": sandbox}))

    def save_encoders(self, encoders: EncodersSettings) -> RouterConfig:
        """Persist the encoder mode (bundled-local vs remote) over the current config.
        The agent-server reloads per-request, so a change drives the NEXT research run."""
        return self.save(self.load().model_copy(update={"encoders": encoders}))

    def save_tts(self, tts: TtsSettings) -> RouterConfig:
        """Persist the audio-overview TTS settings (toggle, bundled-vs-remote, voices).
        The agent-server reloads per-request; disabling it also frees the model."""
        return self.save(self.load().model_copy(update={"tts": tts}))

    def save_image_gen(self, image_gen: ImageGenSettings) -> RouterConfig:
        """Persist the image generation provider (procedural/comfyui/openai). The
        agent-server reloads per-request; a change takes effect on the NEXT image-gen.
        When the provider is bundled (procedural), base_url is cleared."""
        if image_gen.provider == "procedural":
            image_gen = image_gen.model_copy(update={"base_url": ""})
        return self.save(self.load().model_copy(update={"image_gen": image_gen}))

    def save_search(self, search: SearchSettings) -> RouterConfig:
        """Persist the web-discovery provider (ddgs/searxng/tavily) over the config.

        When the provider is the bundled in-process tier (ddgs), the persisted
        base_url is cleared so a stale self-host LAN URL (e.g. from a previous
        searxng selection) cannot silently re-engage if the user later switches
        back to searxng without re-entering the URL."""
        if search.provider == "ddgs":
            search = search.model_copy(update={"base_url": ""})
        return self.save(self.load().model_copy(update={"search": search}))

    def save_extraction(self, extraction: ExtractionSettings) -> RouterConfig:
        """Persist the extraction provider (local/crawl4ai/firecrawl) over the config.

        When the provider is the bundled in-process tier (local), the persisted
        base_url is cleared so a stale self-host LAN URL (e.g. from a previous
        crawl4ai selection) cannot silently re-engage if the user later switches
        back to crawl4ai without re-entering the URL."""
        if extraction.provider == "local":
            extraction = extraction.model_copy(update={"base_url": ""})
        return self.save(self.load().model_copy(update={"extraction": extraction}))

    def save_live_browser(self, live_browser: LiveBrowserSettings) -> RouterConfig:
        """Persist the live-browser enable toggle. The agent-server reloads per-request."""
        return self.save(self.load().model_copy(update={"live_browser": live_browser}))

    # -- Build-project persistence --------------------------------------------

    def save_projects(self, projects: ProjectStorageSettings) -> RouterConfig:
        """Persist the user-chosen Build-project storage path over the current config.
        Same atomic write as save_sandbox; the agent-server reloads per-request so a
        new path drives the NEXT snapshot/rehydrate."""
        return self.save(self.load().model_copy(update={"projects": projects}))

    # -- assignments ----------------------------------------------------------

    def save_assignments(
        self, default_model: str, assignments: dict[ModelRole, str]
    ) -> RouterConfig:
        """Persist new assignments over the current catalogue. Raises ValueError on
        a key that isn't in the catalogue (routing couldn't resolve it)."""
        cfg = self.load()
        if default_model not in cfg.models:
            raise ValueError(f"unknown default_model {default_model!r}")
        for role, key in assignments.items():
            if key not in cfg.models:
                raise ValueError(f"unknown model {key!r} for role {role.value}")
        return self.save(
            cfg.model_copy(update={"default_model": default_model, "assignments": assignments})
        )

    # -- catalogue CRUD -------------------------------------------------------

    def add_model(self, key: str, entry: ModelEntry) -> RouterConfig:
        cfg = self.load()
        if key in cfg.models:
            raise ValueError(f"model {key!r} already exists")
        models = {**cfg.models, key: entry}
        return self.save(cfg.model_copy(update={"models": models}))

    def update_model(self, key: str, entry: ModelEntry) -> RouterConfig:
        cfg = self.load()
        if key not in cfg.models:
            raise ValueError(f"unknown model {key!r}")
        models = {**cfg.models, key: entry}
        return self.save(cfg.model_copy(update={"models": models}))

    def remove_model(self, key: str) -> RouterConfig:
        cfg = self.load()
        if key not in cfg.models:
            raise ValueError(f"unknown model {key!r}")
        if key == cfg.default_model:
            raise ValueError(f"{key!r} is the default model — reassign the default first")
        used_by = [r.value for r, k in cfg.assignments.items() if k == key]
        if used_by:
            raise ValueError(f"{key!r} is assigned to {', '.join(used_by)} — reassign first")
        models = {k: v for k, v in cfg.models.items() if k != key}
        return self.save(cfg.model_copy(update={"models": models}))

    # -- internals ------------------------------------------------------------

    def _apply_overlay(self, base: RouterConfig, overlay: dict) -> RouterConfig:
        default_model = overlay.get("default_model")
        if not isinstance(default_model, str) or default_model not in base.models:
            default_model = base.default_model
        assignments = dict(base.assignments)
        raw = overlay.get("assignments")
        if isinstance(raw, dict):
            for role_str, key in raw.items():
                role = _role(role_str)
                if role is not None and isinstance(key, str) and key in base.models:
                    assignments[role] = key
        return base.model_copy(update={"default_model": default_model, "assignments": assignments})

    def _read(self) -> dict | None:
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
