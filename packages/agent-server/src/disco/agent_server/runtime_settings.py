"""Persisted per-conversation settings (B0) — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The B0 persisted-
settings accessors move out of runtime.py into a `RuntimeSettings`
collaborator constructed once in `ConversationRuntime`:

  - driver model override:  _load_overrides / _save_overrides / set_model_override
  - surface map:            _load_surfaces / _save_surfaces
  - autonomous (headless):  _load_autonomous / _save_autonomous / set_autonomous
                            / _effective_autonomous / is_autonomous
  - assist tier:            _load_assist / _save_assist / set_assist
                            / _effective_assist / is_assist
                            / _is_small_assist_default

The mutable dicts (`_model_override`, `_surface`, `_autonomous`, `_assist`)
and their sidecar paths stay declared on `ConversationRuntime`; the service is
stateless and reaches them — plus `_router_now`, `_surface_of`, the
`_AUTONOMOUS_SURFACES` set — via a back-reference. `set_surface` / `_surface_of`
stay on the runtime (they own the surface recovery ladder) but call the moved
`_save_surfaces` / `_load_surfaces` through the runtime delegators. Every moved
method keeps a one-line delegator on `ConversationRuntime` because the
test-suite + app routes call each directly on the runtime.
"""

from __future__ import annotations

import json
import os
from typing import Any


class RuntimeSettings:
    def __init__(self, rt: Any) -> None:
        self._rt = rt

    # ---- driver model override ---------------------------------------------

    def _load_overrides(self) -> dict[str, str]:
        if self._rt._override_path and os.path.exists(self._rt._override_path):
            try:
                with open(self._rt._override_path) as f:
                    data = json.load(f)
                return {str(k): str(v) for k, v in data.items() if v}
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty, never crash
                return {}
        return {}

    def _save_overrides(self) -> None:
        if not self._rt._override_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._rt._override_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._rt._model_override, f)
                tmp_name = f.name
            os.replace(tmp_name, self._rt._override_path)
        except Exception:  # noqa: BLE001 — persistence is best-effort, never fatal
            pass

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        """Pin the driver model for a conversation (the Build chat model picker). The id
        is a catalogue KEY; RouterAgent reassigns AGENT_DRIVER to it. Must be set before
        the loop is built (at create time). PERSISTED (B0) so a restart keeps the pick."""
        if model_id:
            self._rt._model_override[conversation_id] = model_id
            self._rt._save_overrides()
            # P3: also persist as the last-selected model so new conversations
            # seed from it by default (server-side, no localStorage).
            self.set_last_selected_model(model_id)

    # ---- last-selected model (P3) ------------------------------------------

    def get_last_selected_model(self) -> str | None:
        """Return the last globally-picked driver model, or None if no pick has
        ever been made. Server-side, per-owner sidecar (B0 pattern)."""
        path = getattr(self._rt, "_last_model_path", "")
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            return data.get("model") or None
        except Exception:  # noqa: BLE001 — corrupt/missing → None, never crash
            return None

    def set_last_selected_model(self, model_id: str | None) -> None:
        """Persist the last-picked driver model (atomic temp-file replace, B0 pattern)."""
        path = getattr(self._rt, "_last_model_path", "")
        if not path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump({"model": model_id}, f)
                tmp_name = f.name
            os.replace(tmp_name, path)
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            pass

    # ---- surface map -------------------------------------------------------

    def _load_surfaces(self) -> dict[str, str]:
        """The persisted surface map (sidecar next to PMX_DB). Unknown values are
        dropped (treated as never-set → the recovery ladder still applies), so a
        hand-edited or future-versioned sidecar can't compose an invalid loop."""
        if self._rt._surface_path and os.path.exists(self._rt._surface_path):
            try:
                with open(self._rt._surface_path) as f:
                    data = json.load(f)
                return {
                    str(k): str(v)
                    for k, v in data.items()
                    if v in self._rt._VALID_SURFACES
                }
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty, never crash
                return {}
        return {}

    def _save_surfaces(self) -> None:
        if not self._rt._surface_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._rt._surface_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._rt._surface, f)
                tmp_name = f.name
            os.replace(tmp_name, self._rt._surface_path)
        except Exception:  # noqa: BLE001 — persistence is best-effort, never fatal
            pass

    # ---- autonomous (headless) ---------------------------------------------

    def _load_autonomous(self) -> dict[str, bool]:
        if self._rt._autonomous_path and os.path.exists(self._rt._autonomous_path):
            try:
                with open(self._rt._autonomous_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty
                return {}
        return {}

    def _save_autonomous(self) -> None:
        if not self._rt._autonomous_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._rt._autonomous_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._rt._autonomous, f)
                tmp_name = f.name
            os.replace(tmp_name, self._rt._autonomous_path)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
        """Mark a conversation autonomous (headless) BEFORE it runs. Persisted (B0)."""
        self._rt._autonomous[conversation_id] = bool(value)
        self._rt._save_autonomous()

    def _effective_autonomous(self, conversation_id: str) -> bool:
        """The SINGLE source of truth for "is this conversation actually running
        headless". Autonomous governs the plan-gate auto-approve + ask/clarify
        suppression, so it only takes effect on surfaces that HAVE a plan gate:
        build, agent, AND deep_research (plan→iterate→report). Gating in ONE place
        keeps the prompt prefix (router), the loop's tool-suppression/auto-approve,
        AND the UI badge from disagreeing. The plain `research` surface has no plan
        gate, so an autonomous=True flag there is uniformly treated as interactive."""
        return (
            self._rt._autonomous.get(conversation_id, False)
            and self._rt._surface_of(conversation_id) in self._rt._AUTONOMOUS_SURFACES
        )

    def is_autonomous(self, conversation_id: str) -> bool:
        # The public read (UI badge via /state extras) — gated, so the badge can't
        # show "autonomous" on a surface that has no headless affordance.
        return self._effective_autonomous(conversation_id)

    # ---- assist tier -------------------------------------------------------

    def _is_small_assist_default(self, entry: Any) -> bool:
        if not entry or not getattr(entry, "base_url", None):
            return False
        base_url = str(entry.base_url).lower()
        is_local = any(x in base_url for x in ("localhost", "127.0.0.1", "192.168.", ".local"))
        return is_local and "openrouter" not in base_url

    def _load_assist(self) -> dict[str, bool]:
        if self._rt._assist_path and os.path.exists(self._rt._assist_path):
            try:
                with open(self._rt._assist_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_assist(self) -> None:
        if not self._rt._assist_path:
            return
        import tempfile
        try:
            dir_name = os.path.dirname(self._rt._assist_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._rt._assist, f)
                tmp_name = f.name
            os.replace(tmp_name, self._rt._assist_path)
        except Exception:  # noqa: BLE001
            pass

    def set_assist(self, conversation_id: str, value: bool = True) -> None:
        """Mark a conversation assist tier (T1). Persisted."""
        self._rt._assist[conversation_id] = bool(value)
        self._rt._save_assist()

    def _effective_assist(self, conversation_id: str) -> bool:
        """The SINGLE source of truth for the assist gate."""
        if conversation_id in self._rt._assist:
            return self._rt._assist[conversation_id]

        # Default policy
        override = self._rt._model_override.get(conversation_id)
        router = self._rt._router_now(pick=override)
        from disco.core.llm import ModelRole
        key = router._config.model_for(ModelRole.AGENT_DRIVER, override=override)
        entry = router._config.models.get(key)
        return self._is_small_assist_default(entry)

    def is_assist(self, conversation_id: str) -> bool:
        return self._effective_assist(conversation_id)
