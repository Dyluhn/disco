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
    RoleFallbackSettings,
    RouterConfig,
    SandboxSettings,
    SearchSettings,
    TtsSettings,
    apply_runtime_capabilities,
    build_kernel_experimental_enabled,
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
        experimental_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._path = Path(path or disco_env("CONFIG", _DEFAULT_PATH))
        self._base_factory = base_factory
        # The SINGLE authority deciding whether a persisted `pi_experimental` build
        # kernel may load/save as ACTIVE (codex finding #2). Gate authority used to
        # be split — the app-server validated persistence against its OWN env while
        # the agent-server decided activation against its own — so a stale value
        # could load as active in the wrong process. Now the store normalizes at
        # BOTH load and save against one injected predicate; it defaults to the
        # shared core env gate (`PI_KERNEL_EXPERIMENTAL`), so there is ONE ambient-env
        # reader, and the build executor (agent-server) is the authority by
        # constructing its store in its own process.
        self._experimental_enabled: Callable[[], bool] = (
            experimental_enabled or build_kernel_experimental_enabled
        )
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
        # Finding #4 — WRITE-THROUGH normalization. `_gate_build_kernel` only normalized
        # `pi_experimental`→`disco` IN MEMORY when the gate is off, so the stale
        # `pi_experimental` stayed PERSISTED and would auto-activate the moment the gate
        # later flipped on — WITHOUT a fresh user selection (and an env-split between the
        # app- and agent-server made it worse). Persist the normalized value HERE, at the
        # read boundary, so a dormant gated-off `pi_experimental` is erased from disk the
        # first time it is loaded under a closed gate. A later gate flip then finds
        # `disco` and the user must re-select to activate the experimental kernel. Only a
        # real file is migrated (never the seed), and never a legacy overlay (it carries
        # no build_kernel, so the seed default `disco` normalizes to a no-op) — so this
        # writes at most ONCE per stale file and is a no-op on every subsequent load. The
        # write persists the PRE-overlay config so the runtime capability overlay
        # (vision-probe etc.) is never baked into the file.
        if data is not None:
            normalized = self._normalize_build_kernel(cfg.build_kernel)
            if normalized != cfg.build_kernel:
                cfg = cfg.model_copy(update={"build_kernel": normalized})
                self._write(cfg.model_dump(mode="json"))
        cfg = apply_runtime_capabilities(cfg, probe_results=self._vision_probe)
        return self._gate_build_kernel(cfg)

    def experimental_kernels_enabled(self) -> bool:
        """Whether the experimental Pi build kernel may be selected/activated — the
        store's single gate authority (finding #2). Other layers read it THROUGH the
        store rather than the ambient env directly, so there is one reader."""
        return self._experimental_enabled()

    def _normalize_build_kernel(self, build_kernel: str) -> str:
        """The SINGLE gate-normalization rule (finding #2/#4): a `pi_experimental`
        selection collapses to `disco` unless the experimental gate is open. Used by
        every persistence boundary — `load` (`_gate_build_kernel`), the full-config
        `save`, and `save_build_kernel` — so no path can persist or load a gated-off
        `pi_experimental` as active. No-op for any other value."""
        if build_kernel == "pi_experimental" and not self._experimental_enabled():
            return "disco"
        return build_kernel

    def _gate_build_kernel(self, cfg: RouterConfig) -> RouterConfig:
        """Normalize a persisted `pi_experimental` selection to `disco` unless the
        experimental gate is open (finding #2), so a stale/dormant value can never
        LOAD as active — and therefore can't silently activate the stub if the gate
        later flips on in a different process than the one that persisted it. No-op
        for any other value."""
        normalized = self._normalize_build_kernel(cfg.build_kernel)
        if normalized != cfg.build_kernel:
            return cfg.model_copy(update={"build_kernel": normalized})
        return cfg

    def save(self, config: RouterConfig) -> RouterConfig:
        """Persist the full config (atomically) and return it.

        Gate-normalizes `build_kernel` first (finding #4): the public full-config save
        must NOT persist `pi_experimental` as active while the experimental gate is off —
        otherwise a stale value could load as active if the gate later flips, bypassing
        the `save_build_kernel`/`load` normalization. No-op for the `disco` default."""
        normalized = self._normalize_build_kernel(config.build_kernel)
        if normalized != config.build_kernel:
            config = config.model_copy(update={"build_kernel": normalized})
        self._write(config.model_dump(mode="json"))
        return config

    # -- sandbox backend ------------------------------------------------------

    def save_sandbox(self, sandbox: SandboxSettings) -> RouterConfig:
        """Persist the active sandbox backend + connection over the current config.

        PRESERVES every backend's saved connection block: the incoming settings carry
        the now-ACTIVE backend's flat fields, and `with_preserved_connections` folds in
        the previously-persisted per-backend map so switching the active backend never
        clears the inactive backends' setup (the gVisor-socket-blanking outage)."""
        previous = self.load().sandbox
        merged = sandbox.with_preserved_connections(previous)
        return self.save(self.load().model_copy(update={"sandbox": merged}))

    def save_encoders(self, encoders: EncodersSettings) -> RouterConfig:
        """Persist the encoder mode (bundled-local vs remote) over the current config.
        The agent-server reloads per-request, so a change drives the NEXT research run."""
        return self.save(self.load().model_copy(update={"encoders": encoders}))

    def save_tts(self, tts: TtsSettings) -> RouterConfig:
        """Persist the audio-overview TTS settings (toggle, bundled-vs-remote, voices).
        The agent-server reloads per-request; disabling it also frees the model."""
        return self.save(self.load().model_copy(update={"tts": tts}))

    def save_image_gen(self, image_gen: ImageGenSettings) -> RouterConfig:
        """Persist the image generation provider (comfyui/openai/openrouter — W-50: no
        bundled procedural tier). The agent-server reloads per-request; a change takes
        effect on the NEXT image-gen. OpenRouter has a FIXED origin + uses the reserved
        OpenRouter key, so its base_url/api_key_env are cleared — a stale value from
        another provider must never carry over and get the OpenRouter Bearer key sent to
        the wrong host."""
        if image_gen.provider == "openrouter":
            image_gen = image_gen.model_copy(update={"base_url": "", "api_key_env": ""})
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

    def save_role_fallback(self, settings: RoleFallbackSettings) -> None:
        """Persist auxiliary-role local fallback settings over the current config."""
        self.save(self.load().model_copy(update={"role_fallback": settings}))

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

    def save_build_kernel(self, build_kernel: str) -> RouterConfig:
        """Persist the Build kernel selector (Disco Pi campaign A2). The agent-server
        reloads per-request, so a change drives the NEXT control op / run.

        Gate authority (finding #2): never PERSIST `pi_experimental` as active while
        the experimental gate is off — normalize to `disco` first, so a later gate
        flip (possibly in another process) can't silently activate a stale value."""
        build_kernel = self._normalize_build_kernel(build_kernel)
        return self.save(self.load().model_copy(update={"build_kernel": build_kernel}))

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
