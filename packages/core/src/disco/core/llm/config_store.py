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
from .config import (
    ExtractionSettings,
    ImageGenSettings,
    ModelEntry,
    RouterConfig,
    SearchSettings,
    TtsSettings,
    apply_runtime_capabilities,
    default_config,
)
from .config_approvals import ConfigOriginApprovals
from .config_sections import ConfigSectionWriter
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
        # Per-section persistence (PY-0459): a config-file writer is a distinct
        # authority from this document core. Plain attribute, not a @property —
        # see config_sections.py.
        self.sections = ConfigSectionWriter(self)
        # Origin-approval authority (PY-0459): an approval registry is not a
        # config-file writer. Plain attribute, not a @property — see
        # config_approvals.py.
        self.approvals = ConfigOriginApprovals(self._path)

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
        build_kernel_changed = False
        if data is None:
            cfg = base
        elif "models" in data:  # full config
            data, build_kernel_changed = _normalize_build_kernel_data(data)
            try:
                cfg = RouterConfig.model_validate(data)
            except Exception:  # noqa: BLE001 — a corrupt/stale file must not crash routing
                cfg = base
                build_kernel_changed = False
        else:
            cfg = _apply_overlay(base, data)  # legacy {default_model, assignments}
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
        return _gate_build_kernel(cfg)

    def save(self, config: RouterConfig) -> RouterConfig:
        """Persist the full config (atomically) and return it.

        Normalizes `build_kernel` first so legacy/unknown values never persist."""
        normalized = _normalize_build_kernel(config.build_kernel)
        if normalized != config.build_kernel:
            config = config.model_copy(update={"build_kernel": normalized})
        self._write(config.model_dump(mode="json"))
        return config

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
        if key == cfg.vision_escalation_model:
            raise ValueError(f"{key!r} is the vision model — reassign vision first")
        used_by = [r.value for r, k in cfg.assignments.items() if k == key]
        if used_by:
            raise ValueError(f"{key!r} is assigned to {', '.join(used_by)} — reassign first")
        models = {k: v for k, v in cfg.models.items() if k != key}
        return self.save(cfg.model_copy(update={"models": models}))

    # -- internals ------------------------------------------------------------

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
            slot = _model_secret_slot(key, entry)
            purpose = _model_purpose(entry)
            new_ref = migrate_legacy_secret_ref(
                entry.api_key_env,
                slot=slot,
                url=entry.base_url,
                purpose=purpose,
                store=store,
                diagnostics=diagnostics,
                origin_approved=self.approvals.origin_approved,
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
                _search_url(cfg.search),
                f"search:{cfg.search.provider}",
            ),
            (
                "extraction",
                cfg.extraction,
                cfg.extraction.provider,
                _extraction_url(cfg.extraction),
                f"extraction:{cfg.extraction.provider}",
            ),
            (
                "tts",
                cfg.tts,
                "openai" if cfg.tts.provider == "openai" else cfg.tts.provider,
                _tts_url(cfg.tts),
                f"tts:{cfg.tts.provider}",
            ),
            (
                "image_gen",
                cfg.image_gen,
                "openai" if cfg.image_gen.provider == "openai" else cfg.image_gen.provider,
                _image_url(cfg.image_gen),
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
                origin_approved=self.approvals.origin_approved,
            )
            if new_ref != (ref or ""):
                updates[field] = settings.model_copy(update={"api_key_env": new_ref})
        return updates

    def _with_security_diagnostics(
        self, cfg: RouterConfig, extra: list[str] | None = None
    ) -> RouterConfig:
        diagnostics = list(extra or [])
        for label, url, purpose, secret_ref in _operator_approvals(cfg):
            origin = origin_for_url(url)
            if origin and not self.approvals.origin_approved(url, purpose, secret_ref):
                diagnostics.append(f"{label} origin {origin} awaiting operator approval")
        diagnostics = sorted(set(diagnostics))
        return cfg.model_copy(update={"security_diagnostics": tuple(diagnostics)})


# -- module-level helpers ------------------------------------------------------
# Everything below is a pure function of its arguments (no ConfigStore instance
# state) — build-kernel normalization, the legacy-overlay merge, and the
# per-section URL/diagnostics derivation used by both the security-diagnostics
# pass and the legacy secret-ref migration above. Kept at module scope rather
# than as bound methods: they were never state-dependent, and this is what
# keeps `ConfigStore` itself under the class-size cap (PY-0458).


def _role(value: str) -> ModelRole | None:
    try:
        return ModelRole(value)
    except ValueError:
        return None


def _normalize_build_kernel(build_kernel: str) -> str:
    """Normalize legacy/unknown build-kernel choices to the only live kernel."""
    return "disco" if build_kernel != "disco" else build_kernel


def _normalize_build_kernel_data(data: dict) -> tuple[dict, bool]:
    if "build_kernel" not in data:
        return data, False
    normalized = _normalize_build_kernel(str(data["build_kernel"]))
    if normalized == data["build_kernel"]:
        return data, False
    return {**data, "build_kernel": normalized}, True


def _gate_build_kernel(cfg: RouterConfig) -> RouterConfig:
    """Normalize any legacy in-memory value to the only live kernel."""
    normalized = _normalize_build_kernel(cfg.build_kernel)
    if normalized != cfg.build_kernel:
        return cfg.model_copy(update={"build_kernel": normalized})
    return cfg


def _apply_overlay(base: RouterConfig, overlay: dict) -> RouterConfig:
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
    vision_model = overlay.get("vision_escalation_model", base.vision_escalation_model)
    if not isinstance(vision_model, str) or vision_model not in base.models:
        vision_model = None
    return base.model_copy(
        update={
            "default_model": default_model,
            "assignments": assignments,
            "vision_escalation_model": vision_model,
        }
    )


def _model_secret_slot(key: str, entry: ModelEntry) -> str:
    if entry.provider == "openrouter" or key.startswith("or-"):
        return "openrouter"
    if entry.provider == "gemma" or "gemma" in entry.model_id.lower():
        return "gemma"
    return entry.provider or key


def _model_purpose(entry: ModelEntry) -> str:
    return f"model:{entry.provider or 'unknown'}"


# The keyless hosted MCP search servers. They need no operator approval while
# they carry no credential — only a query leaves the box — so they resolve to an
# origin ONLY once a key is configured for them, at which point they are exactly
# as credentialed as tavily or brave. Same rule as
# `retrieval._provider_wiring._needs_origin_approval`.
_KEYLESS_SEARCH_ORIGINS = {
    "exa": "https://mcp.exa.ai",
    "parallel": "https://search.parallel.ai",
}


def _search_url(search: SearchSettings) -> str:
    if search.provider == "tavily":
        return "https://api.tavily.com"
    if search.provider == "brave":
        return search.base_url.strip() or "https://api.search.brave.com"
    if search.provider == "semantic_scholar":
        return search.base_url.strip() or "https://api.semanticscholar.org"
    if search.provider == "searxng":
        return search.base_url.strip()
    if search.provider in _KEYLESS_SEARCH_ORIGINS and search.api_key_env.strip():
        return search.base_url.strip() or _KEYLESS_SEARCH_ORIGINS[search.provider]
    return ""


def _extraction_url(extraction: ExtractionSettings) -> str:
    if extraction.provider == "crawl4ai":
        return extraction.base_url.strip()
    if extraction.provider == "firecrawl":
        return extraction.base_url.strip() or "https://api.firecrawl.dev"
    return ""


def _tts_url(tts: TtsSettings) -> str:
    if tts.provider == "speaches":
        return tts.base_url.strip()
    if tts.provider == "openai":
        return tts.base_url.strip() or "https://api.openai.com"
    return ""


def _image_url(image_gen: ImageGenSettings) -> str:
    if image_gen.provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if image_gen.provider == "openai":
        return image_gen.base_url.strip() or "https://api.openai.com"
    if image_gen.provider == "comfyui":
        return image_gen.base_url.strip()
    return ""


def _operator_approvals(cfg: RouterConfig) -> list[tuple[str, str, str, str]]:
    """Every URL in `cfg` that needs an operator origin-approval, as
    (label, url, purpose, secret_ref) tuples — one category per helper so
    each stays independently simple (PY-0460)."""
    urls: list[tuple[str, str, str, str]] = []
    urls.extend(_model_operator_approvals(cfg))
    if entry := _role_fallback_operator_approval(cfg):
        urls.append(entry)
    urls.extend(_encoder_operator_approvals(cfg))
    for probe in (
        _search_operator_approval,
        _extraction_operator_approval,
        _tts_operator_approval,
        _image_operator_approval,
    ):
        if entry := probe(cfg):
            urls.append(entry)
    urls.extend(_mcp_operator_approvals(cfg))
    return urls


def _model_operator_approvals(cfg: RouterConfig) -> list[tuple[str, str, str, str]]:
    urls: list[tuple[str, str, str, str]] = []
    for key, entry in cfg.models.items():
        if entry.base_url:
            urls.append(
                (
                    f"model:{key}",
                    entry.base_url,
                    _model_purpose(entry),
                    entry.api_key_env or "",
                )
            )
    return urls


def _role_fallback_operator_approval(cfg: RouterConfig) -> tuple[str, str, str, str] | None:
    if cfg.role_fallback.enabled and cfg.role_fallback.base_url.strip():
        return (
            "role_fallback",
            cfg.role_fallback.base_url.strip(),
            "role_fallback",
            cfg.role_fallback.api_key_env.strip(),
        )
    return None


def _encoder_operator_approvals(cfg: RouterConfig) -> list[tuple[str, str, str, str]]:
    urls: list[tuple[str, str, str, str]] = []
    if cfg.encoders.remote:
        for name, url in (
            ("reranker", cfg.encoders.reranker_url),
            ("embedder", cfg.encoders.embedder_url),
            ("nli", cfg.encoders.nli_url),
        ):
            if url.strip():
                urls.append((name, url.strip(), f"encoder:{name}", ""))
    return urls


def _search_operator_approval(cfg: RouterConfig) -> tuple[str, str, str, str] | None:
    if search_url := _search_url(cfg.search):
        return (
            f"search:{cfg.search.provider}",
            search_url,
            f"search:{cfg.search.provider}",
            cfg.search.api_key_env.strip(),
        )
    return None


def _extraction_operator_approval(cfg: RouterConfig) -> tuple[str, str, str, str] | None:
    if extraction_url := _extraction_url(cfg.extraction):
        return (
            f"extraction:{cfg.extraction.provider}",
            extraction_url,
            f"extraction:{cfg.extraction.provider}",
            cfg.extraction.api_key_env.strip(),
        )
    return None


def _tts_operator_approval(cfg: RouterConfig) -> tuple[str, str, str, str] | None:
    if tts_url := _tts_url(cfg.tts):
        return (
            f"tts:{cfg.tts.provider}",
            tts_url,
            f"tts:{cfg.tts.provider}",
            cfg.tts.api_key_env.strip(),
        )
    return None


def _image_operator_approval(cfg: RouterConfig) -> tuple[str, str, str, str] | None:
    if image_url := _image_url(cfg.image_gen):
        secret_ref = (
            "openrouter" if cfg.image_gen.provider == "openrouter" else cfg.image_gen.api_key_env
        )
        return (
            f"image:{cfg.image_gen.provider}",
            image_url,
            f"image:{cfg.image_gen.provider}",
            secret_ref.strip(),
        )
    return None


def _mcp_operator_approvals(cfg: RouterConfig) -> list[tuple[str, str, str, str]]:
    urls: list[tuple[str, str, str, str]] = []
    for name, raw in cfg.mcp.servers.items():
        if isinstance(raw, dict) and raw.get("transport") == "streamable_http" and raw.get("url"):
            raw_headers = raw.get("headers")
            headers = raw_headers if isinstance(raw_headers, dict) else {}
            refs = tuple(sorted(str(v).strip() for v in headers.values() if str(v).strip())) or (
                "",
            )
            for ref in refs:
                urls.append((f"mcp:{name}", str(raw["url"]), f"mcp:{name}", ref))
    return urls
