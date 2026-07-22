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
  - quiet mode:             _load_quiet / _save_quiet / set_quiet
                            / _effective_quiet / is_quiet
  - atomic settings gate:   apply_settings_change / _conversation_is_pristine
                            per-cid asyncio.Lock in _settings_locks

The mutable dicts (`_model_override`, `_surface`, `_autonomous`, `_assist`, `_quiet`)
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
import logging
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

# Terminal / parked statuses where a deliberate model (or assist) swap is COHERENT:
# the prior turn has fully concluded (or cooperatively stopped), so re-pinning the
# driver before the NEXT kick (resume / replan) can't land incoherently mid-step.
# RUNNING and the gate-awaiting states are deliberately excluded — a swap there would
# change the brain underneath an in-flight plan/step (runthru-v2 ROOT-1's real target).
_TERMINAL_SETTABLE_STATES = frozenset(
    {
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
        ConversationStatus.FINISHED,
        ConversationStatus.PAUSED,
    }
)


logger = logging.getLogger(__name__)


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

    def _set_model_override_unlocked(self, conversation_id: str, model_id: str | None) -> None:
        """Inner setter — does NOT acquire the per-cid lock; call ONLY from
        apply_settings_change (which holds the lock) or from set_model_override
        (non-route, non-concurrent callers that don't need the atomic gate)."""
        if model_id:
            self._rt._model_override[conversation_id] = model_id
            self._rt._save_overrides()
            # P3: also persist as the last-selected model so new conversations
            # seed from it by default (server-side, no localStorage).
            self.set_last_selected_model(model_id)

    def _clear_model_override_unlocked(self, conversation_id: str) -> None:
        """Explicit RESET — drop the per-conversation override so the next kick composes
        the SERVER-DEFAULT driver. Does NOT touch last-selected (the global P3 sticky is a
        convenience for NEW conversations, not this one's pin). Inner (non-locking) form;
        call ONLY from apply_settings_change (which holds the per-cid lock)."""
        if self._rt._model_override.pop(conversation_id, None) is not None:
            self._rt._save_overrides()

    def _resolve_sticky_model(self) -> str | None:
        """The validated last-selected model (P3 sticky), or None. Mirrors the cfg guard in
        conversations._resolve_model so a stale/deleted sticky key never composes — used to
        seed an explicit-null model_override on the PRISTINE pre-kick path (NOT terminal)."""
        last = self.get_last_selected_model()
        if last:
            cfg = self._rt._config_store.load()
            if last in cfg.models:
                return last
        return None

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
                return {str(k): str(v) for k, v in data.items() if v in self._rt._VALID_SURFACES}
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
        except Exception:  # noqa: BLE001 — best-effort, but VISIBLE (a silent
            # no-op here cost autonomy-across-restart, live-caught 2026-07-03)
            logger.warning("autonomous sidecar save failed", exc_info=True)

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

    # ---- quiet mode --------------------------------------------------------

    def _load_quiet(self) -> dict[str, bool]:
        if self._rt._quiet_path and os.path.exists(self._rt._quiet_path):
            try:
                with open(self._rt._quiet_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_quiet(self) -> None:
        if not self._rt._quiet_path:
            return
        import tempfile

        try:
            dir_name = os.path.dirname(self._rt._quiet_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._rt._quiet, f)
                tmp_name = f.name
            os.replace(tmp_name, self._rt._quiet_path)
        except Exception:  # noqa: BLE001
            pass

    def set_quiet(self, conversation_id: str, value: bool = True) -> None:
        """Mark a build-like conversation as quiet. Persisted (B0)."""
        self._rt._quiet[conversation_id] = bool(value)
        self._rt._save_quiet()

    def _effective_quiet(self, conversation_id: str) -> bool:
        return (
            self._rt._quiet.get(conversation_id, False)
            and self._rt._surface_of(conversation_id) in self._rt._BUILD_LIKE_SURFACES
        )

    def is_quiet(self, conversation_id: str) -> bool:
        return self._effective_quiet(conversation_id)

    # ---- assist tier -------------------------------------------------------

    def _is_small_assist_default(self, entry: Any) -> bool:
        # Assist is EXPLICIT-toggle-only (Dylan's requirement): never auto-enabled by
        # hosting. The old heuristic (local && !openrouter → weak) sandbagged capable
        # local models — e.g. Qwen 27B, which is NOT a quality compromise — purely for
        # running on localhost. Hosting no longer implies the assist tier; the per-
        # conversation UI toggle (or an explicit ModelEntry.tier in config) is the only
        # way in. So this default is always False.
        return False

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

        compose_snapshot = self._rt._resolved_context_for_compose.get(conversation_id)
        override = (
            compose_snapshot.model_key
            if compose_snapshot is not None
            else self._rt._model_override.get(conversation_id)
        )
        # This is a metadata read used by every /state response (the Assist badge).
        # Building a live router here needlessly resolves/decrypts every provider and
        # logs every disapproved catalogue entry on every poll.  The policy needs only
        # the current config entry; actual model calls still rebuild the live router.
        config = self._rt._routing_config_now()
        key = config.model_for(ModelRole.AGENT_DRIVER, override=override)
        entry = config.models.get(key)
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

    def _effective_driver_endpoint(
        self, conversation_id: str
    ) -> tuple[str, str, str | None] | None:
        """ROOT-5: the conversation's EFFECTIVE (override-aware) AGENT_DRIVER endpoint as
        (base_url, model_id, api_key_env) — for LLM-using tools (slides_generate) so a
        deck is authored by the model the user PICKED for this conversation, not the
        global default. None when the resolved entry has no live base_url (the tool then
        falls back to the global resolver). Mirrors _effective_policy's entry resolution."""
        from disco.core.llm import ModelRole

        compose_snapshot = self._rt._resolved_context_for_compose.get(conversation_id)
        override = (
            compose_snapshot.model_key
            if compose_snapshot is not None
            else self._rt._model_override.get(conversation_id)
        )
        # Tool-context metadata resolution needs the selected endpoint, not a live
        # provider object.  Avoid provider construction on read-only setup paths.
        config = self._rt._routing_config_now()
        key = config.model_for(ModelRole.AGENT_DRIVER, override=override)
        entry = config.models.get(key)
        if entry is None or not entry.base_url:
            return None
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        if not self._rt._origin_approved(
            entry.base_url, f"model:{entry.provider}", entry.api_key_env
        ):
            return None
        if not secret_ref_allowed_for_origin(entry.api_key_env, entry.base_url):
            return None
        return (entry.base_url, entry.model_id, entry.api_key_env)

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
            if isinstance(event, MessageEvent) and event.source == EventSource.AGENT:
                return False
            if isinstance(event, StatusEvent) and event.status != ConversationStatus.IDLE:
                return False
        return True

    async def _settable_kind(self, conversation_id: str) -> tuple[bool, bool]:
        """Decide whether a model/assist change may be applied, and whether the
        conversation is in a TERMINAL state (vs the pristine pre-kick state).

        Returns (settable, terminal):
          - (False, _)     → reject (409): a run is ACTIVELY in flight (live task),
                              or the status is RUNNING / a gate-awaiting state — a
                              mid-step driver swap would be incoherent.
          - (True, True)   → a deliberate TERMINAL swap (ERROR/STUCK/FINISHED/PAUSED, or
                              IDLE-with-an-unfinished-approved-plan): allowed. The caller
                              evicts the cached loop so the NEXT kick re-resolves the model.
          - (True, False)  → the pristine PRE-KICK path (fresh IDLE conversation, possibly
                              with setup-only events / uploads): allowed exactly as before.

        A live in-flight task is the hard "actively running" signal and rejects in EVERY
        case — even a status that reads terminal can't be mutated while its task drains."""
        # Never mutate settings under a live run (the only genuinely-incoherent case).
        task = self._rt._tasks.get(conversation_id)
        if task is not None and not task.done():
            return (False, False)
        # Authoritative status from the event store (covers post-restart state too).
        state = await self._rt._store.get_state(conversation_id)
        status = state.execution_status
        if status in _TERMINAL_SETTABLE_STATES:
            return (True, True)
        if status == ConversationStatus.IDLE:
            # IDLE splits two ways: an INTERRUPTED run (approved plan, never FINISHED) is
            # a terminal-style resume target → settable; a truly-fresh IDLE conversation
            # falls through to the strict pristine check (which also catches the
            # compose-gap: a loop composed / task scheduled but RUNNING not yet emitted).
            from .runtime import _has_unfinished_plan

            events = await self._rt._store.get_events(conversation_id)
            if _has_unfinished_plan(events):
                return (True, True)
            return (await self._conversation_is_pristine(conversation_id), False)
        # RUNNING / WAITING_FOR_CONFIRMATION / AWAITING_* → mid-run, not settable.
        return (False, False)

    async def apply_settings_change(
        self,
        conversation_id: str,
        *,
        model_override: str | None = None,
        assist: bool | None = None,
        model_provided: bool | None = None,
    ) -> bool:
        """Atomically apply a model/assist change under the per-cid lock.

        Acquires the lock ONCE, classifies the conversation via `_settable_kind`, and —
        when settable — calls the inner (non-locking) setters. Returns True on success
        (settings applied); False when a run is actively in flight or the conversation is
        mid-step (the caller responds 409; settings are left UNMUTATED on False).

        State-aware (terminal model swap): a TERMINAL conversation (ERROR / STUCK /
        FINISHED / PAUSED, or IDLE-with-unfinished-plan) IS settable — the user may change
        the model before resuming/replanning. After mutating, the cached loop + executor
        (composed against the OLD model) are evicted so the NEXT kick re-resolves the
        driver/summarizer/policy/tool-scope with the NEW model. The pristine pre-kick path
        is byte-identical to before (incl. the compose→register race guard).

        `model_provided` is a tri-state distinguishing "the caller explicitly sent a
        model_override field" from "it was omitted" — needed so an explicit NULL (the user
        choosing DEFAULT on a terminal conversation = reset-to-default) is APPLIED as a
        CLEAR, not silently ignored (the #24 silent-ignore class). None ⇒ inferred from
        model_override (a non-None value counts as provided), preserving every existing
        caller that passes a concrete key. The PATCH route passes the explicit
        `model_override in model_fields_set` so a provided-null is honored.

        Null semantics depend on state: on a TERMINAL conversation a provided-null CLEARS
        the override (next turn = server default); on the PRISTINE pre-kick path a
        provided-null seeds the P3 sticky last-selected (unchanged convenience), never a
        clear — so a create-time seed is preserved.

        Callers MUST NOT hold the per-cid lock already (the inner setters are
        non-reentrant to avoid deadlock)."""
        if model_provided is None:
            model_provided = model_override is not None
        lock = self._settings_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            settable, terminal = await self._settable_kind(conversation_id)
            if not settable:
                return False
            # FINAL synchronous guard (closes the compose→register race): `_settable_kind`
            # awaits the store, and `kick()` is SYNC and does NOT take this lock — so a kick
            # scheduled during that await composes + registers `_loops[cid]` + a live task
            # AFTER the check passed. Re-check with NO await before the (synchronous)
            # mutation. A live task disqualifies in EVERY case; a freshly-composed loop
            # disqualifies the pristine pre-kick path (the terminal path expects a cached
            # loop and evicts it below).
            live = self._rt._tasks.get(conversation_id)
            if live is not None and not live.done():
                return False
            if not terminal and conversation_id in self._rt._loops:
                return False
            if model_provided:
                if model_override:
                    # Pin a concrete catalogue key.
                    self._set_model_override_unlocked(conversation_id, model_override)
                elif terminal:
                    # Explicit DEFAULT (null) on a terminal conversation = reset-to-default.
                    self._clear_model_override_unlocked(conversation_id)
                else:
                    # Explicit null pre-kick = P3 sticky-seed convenience (NOT a clear).
                    sticky = self._resolve_sticky_model()
                    if sticky:
                        self._set_model_override_unlocked(conversation_id, sticky)
            if assist is not None:
                self._set_assist_unlocked(conversation_id, assist)
            # Terminal swap: drop the loop/executor bound to the OLD model so the next
            # kick re-composes with the NEW one (the model-pill-silently-ignored fix).
            if terminal and (model_provided or assist is not None):
                self._rt._evict_loop_for_model_change(conversation_id)
            return True

    # ---- artifact_mode (C6) ------------------------------------------------

    def set_artifact_mode(self, conversation_id: str, on: bool) -> None:
        """Mark a conversation as artifact-mode (C6). In-memory only — the flag
        is set at create time from the body and is not needed to survive a restart
        (artifact-mode conversations are short-lived authoring sessions)."""
        stored_appkit = self._rt._store.conversation_appkit_mode_sync(conversation_id)
        if on and stored_appkit:
            raise ValueError("artifact_mode and appkit_mode are mutually exclusive")
        self._rt._artifact_mode[conversation_id] = bool(on)

    def _effective_artifact_mode(self, conversation_id: str) -> bool:
        """True when the conversation was created with artifact_mode=True."""
        return self._rt._artifact_mode.get(conversation_id, False)

    # ---- appkit_mode (EPIC F) ----------------------------------------------

    def set_appkit_mode(self, conversation_id: str, on: bool) -> None:
        """Capture immutable AppKit identity for legacy/internal create callers.

        The public create route writes the bit with the rest of the conversation
        row. This compatibility seam may create a missing row, but it can never
        flip an existing identity in either direction.
        """

        requested = bool(on)
        if requested and self._rt._artifact_mode.get(conversation_id, False):
            raise ValueError("artifact_mode and appkit_mode are mutually exclusive")
        stored = self._rt._store.conversation_appkit_mode_sync(conversation_id)
        if stored is None:
            self._rt._store.create_conversation(conversation_id, appkit_mode=requested)
            stored = requested
        elif stored != requested:
            raise ValueError("appkit_mode is immutable after conversation creation")
        # Retain the old map as a non-authoritative compatibility cache for code
        # inspecting runtime internals. Every effective read below goes to SQLite.
        self._rt._appkit_mode[conversation_id] = stored

    def _effective_appkit_mode(self, conversation_id: str) -> bool:
        """Return immutable AppKit identity or fail closed when unavailable.

        The deployment switch may block AppKit, but it may never turn a governed
        conversation into Freeform. Re-enabling restores composition without
        changing the stored identity.
        """
        from disco.core.flags import appkit_enabled

        stored = self._rt._store.conversation_appkit_mode_sync(conversation_id)
        if stored is True and self._rt._build_platform.appkit_ejected.get(conversation_id, False):
            return False
        if stored is True and not appkit_enabled():
            raise RuntimeError(
                "AppKit is unavailable on this deployment (DISCO_APPKIT_ENABLED=0); "
                "the governed conversation was not opened as Freeform"
            )
        return stored is True

    # ---- per-query research sources ---------------------------------------

    _VALID_RESEARCH_SOURCES = frozenset(
        {
            "ddgs",
            "arxiv",
            "news",
            "semantic_scholar",
            "searxng",
            "tavily",
            "brave",
            "site_scoped",
        }
    )

    def set_research_sources(self, conversation_id: str, sources: list[str]) -> None:
        if not hasattr(self._rt, "_research_sources"):
            self._rt._research_sources = {}
        clean: list[str] = []
        for raw in sources:
            source = str(raw).strip().lower()
            if source in self._VALID_RESEARCH_SOURCES and source not in clean:
                clean.append(source)
        if clean:
            self._rt._research_sources[conversation_id] = tuple(clean)
        else:
            self._rt._research_sources.pop(conversation_id, None)

    def get_research_sources(self, conversation_id: str | None) -> tuple[str, ...]:
        if not conversation_id or not hasattr(self._rt, "_research_sources"):
            return ()
        return self._rt._research_sources.get(conversation_id, ())
