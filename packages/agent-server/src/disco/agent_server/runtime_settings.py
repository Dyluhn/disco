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
  - atomic settings gate:   apply_settings_change / _conversation_is_pristine
                            per-cid asyncio.Lock in _settings_locks

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

import asyncio
import json
import os
from typing import Any

from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    PlanEvent,
    StatusEvent,
)
from disco.core.llm import ModelExecutionPolicy


class RuntimeSettings:
    def __init__(self, rt: Any) -> None:
        self._rt = rt
        # Order C: per-conversation lock for atomic pre-kick settings changes.
        # Acquired by apply_settings_change so concurrent PATCHes are serialized
        # and a PATCH can't race the loop-compose+register step in kick().
        self._settings_locks: dict[str, asyncio.Lock] = {}

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

    def _set_model_override_unlocked(
        self, conversation_id: str, model_id: str | None
    ) -> None:
        """Inner setter — does NOT acquire the per-cid lock; call ONLY from
        apply_settings_change (which holds the lock) or from set_model_override
        (non-route, non-concurrent callers that don't need the atomic gate)."""
        if model_id:
            self._rt._model_override[conversation_id] = model_id
            self._rt._save_overrides()
            # P3: also persist as the last-selected model so new conversations
            # seed from it by default (server-side, no localStorage).
            self.set_last_selected_model(model_id)

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        """Pin the driver model for a conversation (the Build chat model picker). The id
        is a catalogue KEY; RouterAgent reassigns AGENT_DRIVER to it. Must be set before
        the loop is built (at create time). PERSISTED (B0) so a restart keeps the pick.

        Non-route callers (create-conversation, tests) use this directly; the PATCH route
        uses apply_settings_change so the atomic pristine check covers the change."""
        self._set_model_override_unlocked(conversation_id, model_id)

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

    def _set_assist_unlocked(self, conversation_id: str, value: bool) -> None:
        """Inner setter — does NOT acquire the per-cid lock; call ONLY from
        apply_settings_change (which holds the lock) or from set_assist
        (non-route, non-concurrent callers that don't need the atomic gate)."""
        self._rt._assist[conversation_id] = bool(value)
        self._rt._save_assist()

    def set_assist(self, conversation_id: str, value: bool = True) -> None:
        """Mark a conversation assist tier (T1). Persisted.

        Non-route callers (create-conversation, tests) use this directly; the PATCH route
        uses apply_settings_change so the atomic pristine check covers the change."""
        self._set_assist_unlocked(conversation_id, value)

    def _effective_policy(self, conversation_id: str) -> ModelExecutionPolicy:
        """The SINGLE source of truth for model-tier execution — the ONLY place that
        reads `ModelEntry.tier` and `Requirement.ANCHORED_EDIT`. Resolved fresh from the
        conversation's driver model + explicit overrides; callers thread the returned
        immutable object into the loop / executor / tool scope / prompt / request so all
        consumers read the same decision (no second classifier, no stale boolean).

        Precedence: explicit per-conversation assist toggle > ModelEntry.tier > hosting
        heuristic (local && !openrouter). Behavior-preserving: with no tier metadata set,
        a local driver still resolves to the weak/assist tier exactly as before."""
        from disco.core.llm import ModelRole, resolve_policy
        from disco.core.llm.types import Requirement

        override = self._rt._model_override.get(conversation_id)
        router = self._rt._router_now(pick=override)
        key = router._config.model_for(ModelRole.AGENT_DRIVER, override=override)
        entry = router._config.models.get(key)
        anchored = entry is not None and Requirement.ANCHORED_EDIT in entry.capabilities
        return resolve_policy(
            assist_override=self._rt._assist.get(conversation_id),
            entry_tier=getattr(entry, "tier", None),
            hosting_weak_default=self._is_small_assist_default(entry),
            anchored_edit=anchored,
        )

    def _effective_assist(self, conversation_id: str) -> bool:
        """Back-compat shim — the assist gate is now one facet of the resolved policy."""
        return self._effective_policy(conversation_id).assist

    def is_assist(self, conversation_id: str) -> bool:
        return self._effective_assist(conversation_id)

    # ---- atomic pre-kick settings gate (Order C) ---------------------------

    async def _conversation_is_pristine(self, conversation_id: str) -> bool:
        """True iff no work/run events have been recorded and no loop has been
        composed or scheduled for this conversation.

        NON-pristine (returns False) when ANY of the following hold:
          (i)  cid in _loops — loop already composed (even before RUNNING);
          (ii) cid in _tasks with a non-done task — run already scheduled;
          (iii) event store has any PlanEvent, ActionEvent, AGENT MessageEvent,
                or non-IDLE StatusEvent.

        SETUP events are intentionally ignored: ENVIRONMENT MessageEvent and
        DatasourceEvent from pre-kick uploads (files.py:191) must remain patchable
        so the user can change the model after attaching a file."""
        # --- in-memory checks (cheap, no I/O) ---
        if conversation_id in self._rt._loops:
            return False
        task = self._rt._tasks.get(conversation_id)
        if task is not None and not task.done():
            return False
        # --- event-store check (async, catches post-restart state) ---
        events = await self._rt._store.get_events(conversation_id)
        for event in events:
            if isinstance(event, PlanEvent):
                return False
            if isinstance(event, ActionEvent):
                return False
            if (
                isinstance(event, MessageEvent)
                and event.source == EventSource.AGENT
            ):
                return False
            if (
                isinstance(event, StatusEvent)
                and event.status != ConversationStatus.IDLE
            ):
                return False
        return True

    async def apply_settings_change(
        self,
        conversation_id: str,
        *,
        model_override: str | None = None,
        assist: bool | None = None,
    ) -> bool:
        """Atomically apply pre-kick settings under the per-cid lock.

        Acquires the lock ONCE, checks _conversation_is_pristine, and — if
        pristine — calls the inner (non-locking) setters. Returns True on
        success (settings applied); False when the conversation already has
        work/run events or a composed loop/live task (the caller should
        respond 409; settings are left UNMUTATED on False).

        Callers MUST NOT hold the per-cid lock already (the inner setters are
        non-reentrant to avoid deadlock)."""
        lock = self._settings_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            if not await self._conversation_is_pristine(conversation_id):
                return False
            # FINAL synchronous guard (closes the compose→register race): the pristine
            # check above does `await get_events`, and `kick()` is SYNC and does NOT take
            # this lock — so a kick scheduled during that await composes + registers
            # `_loops[cid]` AFTER the in-memory check inside _conversation_is_pristine
            # already passed. Re-check the in-memory composition here with NO await before
            # the (synchronous) mutation, so settings can never change under an
            # already-composed loop / live task.
            if conversation_id in self._rt._loops:
                return False
            task = self._rt._tasks.get(conversation_id)
            if task is not None and not task.done():
                return False
            if model_override is not None:
                self._set_model_override_unlocked(conversation_id, model_override)
            if assist is not None:
                self._set_assist_unlocked(conversation_id, assist)
            return True

    # ---- artifact_mode (C6) ------------------------------------------------

    def set_artifact_mode(self, conversation_id: str, on: bool) -> None:
        """Mark a conversation as artifact-mode (C6). In-memory only — the flag
        is set at create time from the body and is not needed to survive a restart
        (artifact-mode conversations are short-lived authoring sessions)."""
        self._rt._artifact_mode[conversation_id] = bool(on)

    def _effective_artifact_mode(self, conversation_id: str) -> bool:
        """True when the conversation was created with artifact_mode=True."""
        return self._rt._artifact_mode.get(conversation_id, False)
