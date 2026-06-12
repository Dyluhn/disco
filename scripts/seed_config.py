"""First-run config seed for the container/compose deploy.

`default_config()` seeds the developer's LAN model endpoints — useless to a stranger.
This writes a RouterConfig pointed at the DEPLOYER's driver endpoint (from env) — but
ONLY when no config file exists yet. After the first write, the file is authoritative
and the Settings UI owns the catalogue (config_store treats it as the source of truth),
so this never overwrites a user's edits. Run by the container entrypoint before the
servers start.

Env it reads (all optional, with deploy-friendly defaults):
  PMX_DRIVER_BASE_URL  the OpenAI-compatible /v1 endpoint of the local/BYO model
  PMX_DRIVER_MODEL_ID  the model id the endpoint serves
  PMX_DRIVER_CTX       its context window
  PMX_PROJECTS_ROOT    where Build workspaces persist (a /data subdir in compose)
"""

from __future__ import annotations

import os

from disco.core.llm import (
    ConfigStore,
    ModelEntry,
    ProjectStorageSettings,
    default_config,
)
from disco.core.llm.types import ModelRole

_DRIVER_KEY = "driver"


def main() -> None:
    store = ConfigStore()  # honours PMX_CONFIG
    if store.path.exists():
        print(f"[seed] config already at {store.path} — leaving it (Settings UI owns it)")
        return

    base_url = os.environ.get("PMX_DRIVER_BASE_URL", "http://llm:8080/v1")
    model_id = os.environ.get("PMX_DRIVER_MODEL_ID", "local-model")
    ctx = int(os.environ.get("PMX_DRIVER_CTX", "32768"))
    projects_root = os.environ.get("PMX_PROJECTS_ROOT", "/data/projects")

    driver = ModelEntry(
        model_id=model_id, provider=_DRIVER_KEY, context_window=ctx, base_url=base_url
    )

    base = default_config()
    # Keep the keyed-API fallback entries (OpenRouter — usable once the user pastes a
    # key in Settings) but DROP the dev LAN-IP entries a stranger can't reach. Add the
    # deployer's driver, and point every generative role at it (NLI still runs in-process
    # via the bundled encoders, so its role assignment is just a resolvable placeholder).
    kept = {
        k: v
        for k, v in base.models.items()
        if v.provider == "openrouter" or "openrouter" in (v.base_url or "")
    }
    models = {_DRIVER_KEY: driver, **kept}
    assignments = {role: _DRIVER_KEY for role in ModelRole}

    cfg = base.model_copy(
        update={
            "models": models,
            "default_model": _DRIVER_KEY,
            "assignments": assignments,
            "projects": ProjectStorageSettings(projects_root=projects_root),
            # sandbox / search (ddgs) / extraction (local) / encoders (bundled ONNX)
            # keep their already-correct keyless defaults.
        }
    )
    store.save(cfg)
    print(f"[seed] wrote {store.path}: driver '{model_id}' @ {base_url}, projects → {projects_root}")


if __name__ == "__main__":
    main()
