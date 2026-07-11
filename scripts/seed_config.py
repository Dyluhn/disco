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
    # A brand-new install ships EXACTLY ONE catalogue entry: the honest
    # "not configured" driver. No developer LAN endpoints, and no pre-seeded
    # paid models either — the old seed kept the OpenRouter examples
    # (claude-3.5-sonnet as `driver-overflow`, gemini-3-flash), which surfaced
    # in Settings as models the user never added and cannot use without a key
    # (a false affordance, found on the 2026-07-09 fresh-install walkthrough).
    # Users add models through Settings → provider browse, which is the flow
    # that also stores the key. Empty assignments make every role fall back to
    # the default model; once the user sets a real default, all roles run
    # without separately reassigning each row.
    models = {_UNCONFIGURED_DRIVER_KEY: unconfigured_driver}

    cfg = base.model_copy(
        update={
            "models": models,
            "default_model": _UNCONFIGURED_DRIVER_KEY,
            "assignments": {},
            # dev default points at the (dropped) or-gemini-3-flash entry; a
            # None target simply disables vision escalation until the user
            # picks a vision-capable model.
            "vision_escalation_model": None,
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
