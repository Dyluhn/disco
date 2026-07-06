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
from ..host_egress import origin_for_url
from ..origin_approvals import OriginApprovalStore
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
    default_config,
)
from .secret_refs import migrate_legacy_secret_ref
from .secrets import SecretStore
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
        return self.approval_store(secret_store=secret_store).is_approved(
            url, purpose, secret_ref
        )

    def approve_origin(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.approval_store(secret_store=secret_store).approve(url, purpose, secret_ref)

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
        build_kernel_changed = False
        if data is None:
            cfg = base
        elif "models" in data:  # full config
            data, build_kernel_changed = self._normalize_build_kernel_data(data)
            try:
                cfg = RouterConfig.model_validate(data)
            except Exception:  # noqa: BLE001 — a corrupt/stale file must not crash routing
                cfg = base
                build_kernel_changed = False
        else:
            cfg = self._apply_overlay(base, data)  # legacy {default_model, assignments}
        # Legacy build-kernel write-through. This is intentionally before runtime
        # capability overlays so probe/env-derived fields are never baked into the
        # user's file.
        if data is not None:
            migrated, security_changed = self._migrate_secret_refs_and_trust(cfg)
            if security_changed:
                cfg = migrated
                self._write(cfg.model_dump(mode="json"))
                build_kernel_changed = False
            if build_kernel_changed:
                self._write(cfg.model_dump(mode="json"))
        else:
            cfg = self._with_security_diagnostics(cfg)
        cfg = apply_runtime_capabilities(cfg, probe_results=self._vision_probe)
        return self._gate_build_kernel(cfg)

    def _normalize_build_kernel(self, build_kernel: str) -> str:
        """Normalize legacy/unknown build-kernel choices to the only live kernel."""
        return "disco" if build_kernel != "disco" else build_kernel

    def _normalize_build_kernel_data(self, data: dict) -> tuple[dict, bool]:
        if "build_kernel" not in data:
            return data, False
        normalized = self._normalize_build_kernel(str(data["build_kernel"]))
        if normalized == data["build_kernel"]:
            return data, False
        return {**data, "build_kernel": normalized}, True

    def _gate_build_kernel(self, cfg: RouterConfig) -> RouterConfig:
        """Normalize any legacy in-memory value to the only live kernel."""
        normalized = self._normalize_build_kernel(cfg.build_kernel)
        if normalized != cfg.build_kernel:
            return cfg.model_copy(update={"build_kernel": normalized})
        return cfg

    def save(self, config: RouterConfig) -> RouterConfig:
        """Persist the full config (atomically) and return it.

        Normalizes `build_kernel` first so legacy/unknown values never persist."""
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
        cfg = self.load()
        return self.save(cfg.model_copy(update={"encoders": encoders}))

    def save_tts(self, tts: TtsSettings) -> RouterConfig:
        """Persist the audio-overview TTS settings (toggle, bundled-vs-remote, voices).
        The agent-server reloads per-request; disabling it also frees the model."""
        cfg = self.load()
        return self.save(cfg.model_copy(update={"tts": tts}))

    def save_image_gen(self, image_gen: ImageGenSettings) -> RouterConfig:
        """Persist the image generation provider (comfyui/openai/openrouter — W-50: no
        bundled procedural tier). The agent-server reloads per-request; a change takes
        effect on the NEXT image-gen. OpenRouter has a FIXED origin + uses the reserved
        OpenRouter key, so its base_url/api_key_env are cleared — a stale value from
        another provider must never carry over and get the OpenRouter Bearer key sent to
        the wrong host."""
        if image_gen.provider == "openrouter":
            image_gen = image_gen.model_copy(update={"base_url": "", "api_key_env": ""})
        cfg = self.load()
        return self.save(cfg.model_copy(update={"image_gen": image_gen}))

    def save_search(self, search: SearchSettings) -> RouterConfig:
        """Persist the configured web-discovery provider over the config.

        When the provider is the bundled in-process tier (ddgs), the persisted
        base_url is cleared so a stale self-host LAN URL (e.g. from a previous
        searxng selection) cannot silently re-engage if the user later switches
        back to searxng without re-entering the URL."""
        if search.provider == "ddgs":
            search = search.model_copy(update={"base_url": ""})
        cfg = self.load()
        return self.save(cfg.model_copy(update={"search": search}))

    def save_role_fallback(self, settings: RoleFallbackSettings) -> None:
        """Persist auxiliary-role local fallback settings over the current config."""
        cfg = self.load()
        self.save(cfg.model_copy(update={"role_fallback": settings}))

    def save_extraction(self, extraction: ExtractionSettings) -> RouterConfig:
        """Persist the extraction provider (local/crawl4ai/firecrawl) over the config.

        When the provider is the bundled in-process tier (local), the persisted
        base_url is cleared so a stale self-host LAN URL (e.g. from a previous
        crawl4ai selection) cannot silently re-engage if the user later switches
        back to crawl4ai without re-entering the URL."""
        if extraction.provider == "local":
            extraction = extraction.model_copy(update={"base_url": ""})
        cfg = self.load()
        return self.save(cfg.model_copy(update={"extraction": extraction}))

    def save_live_browser(self, live_browser: LiveBrowserSettings) -> RouterConfig:
        """Persist the live-browser enable toggle. The agent-server reloads per-request."""
        return self.save(self.load().model_copy(update={"live_browser": live_browser}))

    def save_build_kernel(self, build_kernel: str) -> RouterConfig:
        """Persist the vestigial Build kernel selector after legacy normalization."""
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

    def _migrate_secret_refs_and_trust(self, cfg: RouterConfig) -> tuple[RouterConfig, bool]:
        store = SecretStore()
        diagnostics: list[str] = []
        changed = False

        models = dict(cfg.models)
        for key, entry in cfg.models.items():
            slot = self._model_secret_slot(key, entry)
            purpose = self._model_purpose(entry)
            new_ref = migrate_legacy_secret_ref(
                entry.api_key_env,
                slot=slot,
                url=entry.base_url,
                purpose=purpose,
                store=store,
                diagnostics=diagnostics,
                origin_approved=self.origin_approved,
            )
            if new_ref != (entry.api_key_env or ""):
                models[key] = entry.model_copy(update={"api_key_env": new_ref or None})
                changed = True

        updates: dict[str, object] = {"models": models}
        updates |= self._migrate_settings_secret_refs(cfg, store, diagnostics)
        migrated = cfg.model_copy(update=updates)
        migrated = self._with_security_diagnostics(migrated, diagnostics)
        if migrated.security_diagnostics != cfg.security_diagnostics:
            changed = True
        return migrated, changed

    def _migrate_settings_secret_refs(
        self, cfg: RouterConfig, store: SecretStore, diagnostics: list[str]
    ) -> dict[str, object]:
        updates: dict[str, object] = {}
        pairs = (
            (
                "search",
                cfg.search,
                cfg.search.provider,
                self._search_url(cfg.search),
                f"search:{cfg.search.provider}",
            ),
            (
                "extraction",
                cfg.extraction,
                cfg.extraction.provider,
                self._extraction_url(cfg.extraction),
                f"extraction:{cfg.extraction.provider}",
            ),
            (
                "tts",
                cfg.tts,
                "openai" if cfg.tts.provider == "openai" else cfg.tts.provider,
                self._tts_url(cfg.tts),
                f"tts:{cfg.tts.provider}",
            ),
            (
                "image_gen",
                cfg.image_gen,
                "openai" if cfg.image_gen.provider == "openai" else cfg.image_gen.provider,
                self._image_url(cfg.image_gen),
                f"image:{cfg.image_gen.provider}",
            ),
            (
                "role_fallback",
                cfg.role_fallback,
                "openai",
                cfg.role_fallback.base_url.strip(),
                "role_fallback",
            ),
        )
        for field, settings, slot, url, purpose in pairs:
            ref = getattr(settings, "api_key_env", "")
            new_ref = migrate_legacy_secret_ref(
                ref,
                slot=slot,
                url=url,
                purpose=purpose,
                store=store,
                diagnostics=diagnostics,
                origin_approved=self.origin_approved,
            )
            if new_ref != (ref or ""):
                updates[field] = settings.model_copy(update={"api_key_env": new_ref})
        return updates

    def _with_security_diagnostics(
        self, cfg: RouterConfig, extra: list[str] | None = None
    ) -> RouterConfig:
        diagnostics = list(extra or [])
        for label, url, purpose, secret_ref in self._operator_approvals(cfg):
            origin = origin_for_url(url)
            if origin and not self.origin_approved(url, purpose, secret_ref):
                diagnostics.append(f"{label} origin {origin} awaiting operator approval")
        diagnostics = sorted(set(diagnostics))
        return cfg.model_copy(update={"security_diagnostics": tuple(diagnostics)})

    def _operator_approvals(self, cfg: RouterConfig) -> list[tuple[str, str, str, str]]:
        urls: list[tuple[str, str, str, str]] = []
        for key, entry in cfg.models.items():
            if entry.base_url:
                urls.append(
                    (
                        f"model:{key}",
                        entry.base_url,
                        self._model_purpose(entry),
                        entry.api_key_env or "",
                    )
                )
        if cfg.role_fallback.enabled and cfg.role_fallback.base_url.strip():
            urls.append(
                (
                    "role_fallback",
                    cfg.role_fallback.base_url.strip(),
                    "role_fallback",
                    cfg.role_fallback.api_key_env.strip(),
                )
            )
        if cfg.encoders.remote:
            for name, url in (
                ("reranker", cfg.encoders.reranker_url),
                ("embedder", cfg.encoders.embedder_url),
                ("nli", cfg.encoders.nli_url),
            ):
                if url.strip():
                    urls.append((name, url.strip(), f"encoder:{name}", ""))
        if search_url := self._search_url(cfg.search):
            urls.append(
                (
                    f"search:{cfg.search.provider}",
                    search_url,
                    f"search:{cfg.search.provider}",
                    cfg.search.api_key_env.strip(),
                )
            )
        if extraction_url := self._extraction_url(cfg.extraction):
            urls.append(
                (
                    f"extraction:{cfg.extraction.provider}",
                    extraction_url,
                    f"extraction:{cfg.extraction.provider}",
                    cfg.extraction.api_key_env.strip(),
                )
            )
        if tts_url := self._tts_url(cfg.tts):
            urls.append(
                (
                    f"tts:{cfg.tts.provider}",
                    tts_url,
                    f"tts:{cfg.tts.provider}",
                    cfg.tts.api_key_env.strip(),
                )
            )
        if image_url := self._image_url(cfg.image_gen):
            secret_ref = (
                "openrouter"
                if cfg.image_gen.provider == "openrouter"
                else cfg.image_gen.api_key_env
            )
            urls.append(
                (
                    f"image:{cfg.image_gen.provider}",
                    image_url,
                    f"image:{cfg.image_gen.provider}",
                    secret_ref.strip(),
                )
            )
        for name, raw in cfg.mcp.servers.items():
            if (
                isinstance(raw, dict)
                and raw.get("transport") == "streamable_http"
                and raw.get("url")
            ):
                raw_headers = raw.get("headers")
                headers = raw_headers if isinstance(raw_headers, dict) else {}
                refs = tuple(
                    sorted(str(v).strip() for v in headers.values() if str(v).strip())
                ) or ("",)
                for ref in refs:
                    urls.append((f"mcp:{name}", str(raw["url"]), f"mcp:{name}", ref))
        return urls

    def _model_secret_slot(self, key: str, entry: ModelEntry) -> str:
        if entry.provider == "openrouter" or key.startswith("or-"):
            return "openrouter"
        if entry.provider == "gemma" or "gemma" in entry.model_id.lower():
            return "gemma"
        return entry.provider or key

    def _model_purpose(self, entry: ModelEntry) -> str:
        return f"model:{entry.provider or 'unknown'}"

    def _search_url(self, search: SearchSettings) -> str:
        if search.provider == "tavily":
            return "https://api.tavily.com"
        if search.provider == "brave":
            return search.base_url.strip() or "https://api.search.brave.com"
        if search.provider == "semantic_scholar":
            return search.base_url.strip() or "https://api.semanticscholar.org"
        if search.provider == "searxng":
            return search.base_url.strip()
        return ""

    def _extraction_url(self, extraction: ExtractionSettings) -> str:
        if extraction.provider == "crawl4ai":
            return extraction.base_url.strip()
        if extraction.provider == "firecrawl":
            return extraction.base_url.strip() or "https://api.firecrawl.dev"
        return ""

    def _tts_url(self, tts: TtsSettings) -> str:
        if tts.provider == "speaches":
            return tts.base_url.strip()
        if tts.provider == "openai":
            return tts.base_url.strip() or "https://api.openai.com"
        return ""

    def _image_url(self, image_gen: ImageGenSettings) -> str:
        if image_gen.provider == "openrouter":
            return "https://openrouter.ai/api/v1"
        if image_gen.provider == "openai":
            return image_gen.base_url.strip() or "https://api.openai.com"
        if image_gen.provider == "comfyui":
            return image_gen.base_url.strip()
        return ""


def _role(value: str) -> ModelRole | None:
    try:
        return ModelRole(value)
    except ValueError:
        return None
