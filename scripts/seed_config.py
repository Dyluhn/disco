"""First-run config seed for the container/compose deploy.

`default_config()` is a developer checkout seed and contains LAN endpoints that
are not reachable on a stranger's machine. The compose image instead starts in
an honest "model not configured" state: no driver endpoint is seeded, Settings
owns the model catalogue, and `disco-verify` is the proof step after the user
adds their local/LAN or paid OpenAI-compatible endpoint.

This writes only when no config file exists yet. After the first write, the file
is authoritative and the Settings UI owns the catalogue.
"""

from __future__ import annotations

import os

from disco.core.llm import (
    ConfigStore,
    ModelEntry,
    ProjectStorageSettings,
    Requirement,
    default_config,
)

_UNCONFIGURED_DRIVER_KEY = "driver-unconfigured"


def main() -> None:
    store = ConfigStore()  # honours PMX_CONFIG
    if store.path.exists():
        print(f"[seed] config already at {store.path} — leaving it (Settings UI owns it)")
        return

    def _env(name: str, default: str) -> str:
        return os.environ.get(f"DISCO_{name}") or os.environ.get(f"PMX_{name}", default)

    projects_root = _env("PROJECTS_ROOT", "/data/projects")

    unconfigured_driver = ModelEntry(
        model_id="not-configured",
        provider="unconfigured",
        context_window=32_768,
        base_url=None,
        capabilities=frozenset(
            {Requirement.TOOL_CALLING, Requirement.JSON_MODE, Requirement.LONG_CONTEXT}
        ),
        pricing_mode="free",
    )

    base = default_config()
    # Keep the paid OpenRouter examples (usable after the user stores a key), but
    # DROP every developer LAN endpoint. Empty assignments make every role fall
    # back to the default model; once the user sets a real default, all roles can
    # run without separately reassigning each row.
    kept = {
        k: v
        for k, v in base.models.items()
        if v.provider == "openrouter" or "openrouter" in (v.base_url or "")
    }
    models = {_UNCONFIGURED_DRIVER_KEY: unconfigured_driver, **kept}

    cfg = base.model_copy(
        update={
            "models": models,
            "default_model": _UNCONFIGURED_DRIVER_KEY,
            "assignments": {},
            "projects": ProjectStorageSettings(projects_root=projects_root),
            # sandbox / search (ddgs) / extraction (local) / encoders (bundled ONNX)
            # keep their already-correct keyless defaults.
        }
    )
    store.save(cfg)
    print(
        f"[seed] wrote {store.path}: model not configured yet; "
        f"open Settings to add a driver endpoint; projects -> {projects_root}"
    )


if __name__ == "__main__":
    main()
