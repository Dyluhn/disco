"""Owns the sticky per-conversation model binding.

Split out of ``RuntimeSettings`` (PY-0327 / PY-0328): a genuine authority with
its own override sidecar, its own P3 last-selected-model sidecar, and the
sticky-resolution/eviction policy layered on top of them. The per-cid lock
(Order C) stays on ``RuntimeSettings``, which threads it around the
non-locking setters below.
"""

from __future__ import annotations

import json
import os
from typing import Protocol

from disco.core.llm import ConfigStore


class _ModelChangeEviction(Protocol):
    def evict_for_model_change(self, conversation_id: str) -> None: ...


class ModelBindingSettings:
    def __init__(
        self,
        *,
        db_path: str,
        config_store: ConfigStore,
        model_bindings: _ModelChangeEviction,
    ) -> None:
        self._config_store = config_store
        self._model_bindings = model_bindings
        self._override_path = f"{db_path}.overrides.json" if db_path else ""
        self._last_model_path = f"{db_path}.last_model.json" if db_path else ""
        self._model_overrides = self._load_overrides()

    def _get_model_override(self, conversation_id: str) -> str | None:
        return self._model_overrides.get(conversation_id)

    def forget(self, conversation_id: str) -> None:
        self._model_overrides.pop(conversation_id, None)

    def _evict_model_binding(self, conversation_id: str) -> None:
        self._model_bindings.evict_for_model_change(conversation_id)

    # ---- driver model override ---------------------------------------------

    def _load_overrides(self) -> dict[str, str]:
        if self._override_path and os.path.exists(self._override_path):
            try:
                with open(self._override_path) as f:
                    data = json.load(f)
                return {str(k): str(v) for k, v in data.items() if v}
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty, never crash
                return {}
        return {}

    def _save_overrides(self) -> None:
        if not self._override_path:
            return
        import tempfile

        try:
            dir_name = os.path.dirname(self._override_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._model_overrides, f)
                tmp_name = f.name
            os.replace(tmp_name, self._override_path)
        except Exception:  # noqa: BLE001 — persistence is best-effort, never fatal
            pass

    def _set_model_override_unlocked(self, conversation_id: str, model_id: str | None) -> None:
        """Inner setter — does NOT acquire the per-cid lock; call ONLY from
        RuntimeSettings.apply_settings_change (which holds the lock) or from
        set_model_override (non-route, non-concurrent callers that don't need the
        atomic gate)."""
        if model_id:
            self._model_overrides[conversation_id] = model_id
            self._save_overrides()
            # P3: also persist as the last-selected model so new conversations
            # seed from it by default (server-side, no localStorage).
            self.set_last_selected_model(model_id)

    def _clear_model_override_unlocked(self, conversation_id: str) -> None:
        """Explicit RESET — drop the per-conversation override so the next kick composes
        the SERVER-DEFAULT driver. Does NOT touch last-selected (the global P3 sticky is a
        convenience for NEW conversations, not this one's pin). Inner (non-locking) form;
        call ONLY from RuntimeSettings.apply_settings_change (which holds the per-cid
        lock)."""
        if self._model_overrides.pop(conversation_id, None) is not None:
            self._save_overrides()

    def _resolve_sticky_model(self) -> str | None:
        """The validated last-selected model (P3 sticky), or None. Mirrors the cfg guard in
        conversations._resolve_model so a stale/deleted sticky key never composes — used to
        seed an explicit-null model_override on the PRISTINE pre-kick path (NOT terminal)."""
        last = self.get_last_selected_model()
        if last:
            cfg = self._config_store.load()
            if last in cfg.models:
                return last
        return None

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        """Pin the driver model for a conversation (the Build chat model picker). The id
        is a catalogue KEY; RouterAgent reassigns AGENT_DRIVER to it. Must be set before
        the loop is built (at create time). PERSISTED (B0) so a restart keeps the pick.

        Non-route callers (create-conversation, tests) use this directly; the PATCH route
        uses RuntimeSettings.apply_settings_change so the atomic pristine check covers the
        change."""
        self._set_model_override_unlocked(conversation_id, model_id)

    # ---- last-selected model (P3) ------------------------------------------

    def get_last_selected_model(self) -> str | None:
        """Return the last globally-picked driver model, or None if no pick has
        ever been made. Server-side, per-owner sidecar (B0 pattern)."""
        path = self._last_model_path
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
        path = self._last_model_path
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
