"""Owned runtime settings state, persistence, policy, and surface recovery.

The boundary uses concrete stores and cohesive owner objects. It never receives
the runtime, a callable-field facade, a dependency bag, or a service locator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, cast

from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    PlanEvent,
    StatusEvent,
)
from disco.core.llm import ConfigStore, DefaultLLMRouter, ModelExecutionPolicy, SecretStore
from disco.core.llm.config import RouterConfig
from disco.core.llm.secret_refs import secret_ref_allowed_for_origin
from disco.core.loop import AgentLoop
from disco.core.store.sqlite import SqliteEventStore
from disco.tools import DefaultToolExecutor, SandboxSession
from disco.tools.projects import ProjectStore, StorageStatus

from .driver_context import ResolvedDriverContext

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

_VALID_SURFACES = frozenset({"research", "build", "agent", "deep_research"})
_BUILD_LIKE_SURFACES = frozenset({"build", "agent"})
_AUTONOMOUS_SURFACES = frozenset({"build", "agent", "deep_research"})


class _RuntimeSettingsRouting:
    """Current routing metadata and origin policy needed by settings reads."""

    def __init__(
        self,
        config_store: ConfigStore,
        secret_store: SecretStore,
        injected_router: DefaultLLMRouter | None,
    ) -> None:
        self._config_store = config_store
        self._secret_store = secret_store
        self._injected_router = injected_router

    def config_now(self) -> RouterConfig:
        if self._injected_router is not None:
            return self._injected_router._config
        return self._config_store.load()

    def origin_approved(self, url: str, purpose: str, secret_ref: str | None) -> bool:
        return self._config_store.origin_approved(
            url,
            purpose,
            secret_ref,
            secret_store=self._secret_store,
        )


class _RuntimeModelBindings:
    """The live/cache state affected by an atomic model settings change."""

    def __init__(
        self,
        *,
        loops: dict[str, AgentLoop],
        tasks: dict[str, asyncio.Task[Any]],
        compose_contexts: dict[str, ResolvedDriverContext],
        resolved_contexts: dict[str, ResolvedDriverContext],
        executors: dict[str, DefaultToolExecutor],
        pending_sessions: dict[str, SandboxSession],
    ) -> None:
        self._loops = loops
        self._tasks = tasks
        self._compose_contexts = compose_contexts
        self._resolved_contexts = resolved_contexts
        self._executors = executors
        self._pending_sessions = pending_sessions

    def loop_exists(self, conversation_id: str) -> bool:
        return conversation_id in self._loops

    def live_task_exists(self, conversation_id: str) -> bool:
        task = self._tasks.get(conversation_id)
        return task is not None and not task.done()

    def compose_model_key(self, conversation_id: str) -> str | None:
        context = self._compose_contexts.get(conversation_id)
        return context.model_key if context is not None else None

    def evict_for_model_change(self, conversation_id: str) -> None:
        if self.live_task_exists(conversation_id):
            return
        self._loops.pop(conversation_id, None)
        self._resolved_contexts.pop(conversation_id, None)
        executor = self._executors.pop(conversation_id, None)
        if conversation_id not in self._pending_sessions:
            session = getattr(executor, "_sandbox", None) if executor is not None else None
            if session is not None:
                self._pending_sessions[conversation_id] = cast(SandboxSession, session)


class _RuntimeSurfaceSettings:
    """Own the durable surface map and its ordered recovery ladder."""

    def __init__(
        self,
        *,
        db_path: str,
        store: SqliteEventStore,
        config_store: ConfigStore,
    ) -> None:
        self._store = store
        self._config_store = config_store
        self._path = f"{db_path}.surfaces.json" if db_path else ""
        self._surfaces = self._load()

    def _load(self) -> dict[str, str]:
        if self._path and os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    data = json.load(f)
                return {str(k): str(v) for k, v in data.items() if v in _VALID_SURFACES}
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty
                return {}
        return {}

    def _save(self) -> None:
        if not self._path:
            return
        import tempfile

        try:
            dir_name = os.path.dirname(self._path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._surfaces, f)
                tmp_name = f.name
            os.replace(tmp_name, self._path)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass

    def _set(self, conversation_id: str, surface: str) -> None:
        self._surfaces[conversation_id] = surface if surface in _VALID_SURFACES else "research"
        self._save()

    def _get(self, conversation_id: str) -> str:
        cached = self._surfaces.get(conversation_id)
        if cached is not None:
            return cached
        try:
            conn0 = getattr(self._store, "_conn", None)
            if conn0 is not None:
                row = conn0.execute(
                    "SELECT surface FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is not None and row["surface"] in _VALID_SURFACES:
                    self._surfaces[conversation_id] = row["surface"]
                    self._save()
                    return row["surface"]
        except Exception:  # noqa: BLE001 — best-effort recovery, fall through
            pass
        project_store = ProjectStore(self._config_store.load().projects.projects_root)
        if project_store.status() == StorageStatus.OK:
            try:
                if project_store.get(conversation_id) is not None:
                    self._surfaces[conversation_id] = "build"
                    self._save()
                    return "build"
            except Exception:  # noqa: BLE001 — best-effort recovery
                pass
        try:
            conn = getattr(self._store, "_conn", None)
            if conn is not None:
                kinds = {
                    row["kind"]
                    for row in conn.execute(
                        "SELECT DISTINCT kind FROM events WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                }
                if "report" in kinds:
                    self._surfaces[conversation_id] = "deep_research"
                    self._save()
                    return "deep_research"
                if "plan" in kinds and "action" not in kinds:
                    self._surfaces[conversation_id] = "deep_research"
                    self._save()
                    return "deep_research"
        except Exception:  # noqa: BLE001 — best-effort recovery
            pass
        return "research"


class _RuntimeModeSettings:
    """Own transient artifact/AppKit identity caches and research source pins."""

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

    def __init__(
        self,
        store: SqliteEventStore,
        appkit_ejections: Mapping[str, bool],
    ) -> None:
        self._store = store
        self._appkit_ejections = appkit_ejections
        self._artifact_mode: dict[str, bool] = {}
        self._appkit_mode: dict[str, bool] = {}
        self._research_sources: dict[str, tuple[str, ...]] = {}

    def _set_artifact(self, conversation_id: str, on: bool) -> None:
        stored_appkit = self._store.conversation_appkit_mode_sync(conversation_id)
        if on and stored_appkit:
            raise ValueError("artifact_mode and appkit_mode are mutually exclusive")
        self._artifact_mode[conversation_id] = bool(on)

    def _artifact_enabled(self, conversation_id: str) -> bool:
        return self._artifact_mode.get(conversation_id, False)

    def _set_appkit(self, conversation_id: str, on: bool) -> None:
        requested = bool(on)
        if requested and self._artifact_mode.get(conversation_id, False):
            raise ValueError("artifact_mode and appkit_mode are mutually exclusive")
        stored = self._store.conversation_appkit_mode_sync(conversation_id)
        if stored is None:
            self._store.create_conversation(conversation_id, appkit_mode=requested)
            stored = requested
        elif stored != requested:
            raise ValueError("appkit_mode is immutable after conversation creation")
        self._appkit_mode[conversation_id] = stored

    def _appkit_enabled(self, conversation_id: str) -> bool:
        from disco.core.flags import appkit_enabled

        stored = self._store.conversation_appkit_mode_sync(conversation_id)
        if stored is True and self._appkit_ejections.get(conversation_id, False):
            return False
        if stored is True and not appkit_enabled():
            raise RuntimeError(
                "AppKit is unavailable on this deployment (DISCO_APPKIT_ENABLED=0); "
                "the governed conversation was not opened as Freeform"
            )
        return stored is True

    def _set_research_sources(self, conversation_id: str, sources: list[str]) -> None:
        clean: list[str] = []
        for raw in sources:
            source = str(raw).strip().lower()
            if source in self._VALID_RESEARCH_SOURCES and source not in clean:
                clean.append(source)
        if clean:
            self._research_sources[conversation_id] = tuple(clean)
        else:
            self._research_sources.pop(conversation_id, None)

    def _get_research_sources(self, conversation_id: str | None) -> tuple[str, ...]:
        if not conversation_id:
            return ()
        return self._research_sources.get(conversation_id, ())


class RuntimeSettings:
    def __init__(
        self,
        *,
        db_path: str,
        store: SqliteEventStore,
        config_store: ConfigStore,
        routing: _RuntimeSettingsRouting,
        model_bindings: _RuntimeModelBindings,
        appkit_ejections: Mapping[str, bool],
    ) -> None:
        self._store = store
        self._config_store = config_store
        self._routing = routing
        self._model_bindings = model_bindings
        self._surface_settings = _RuntimeSurfaceSettings(
            db_path=db_path,
            store=store,
            config_store=config_store,
        )
        self._mode_settings = _RuntimeModeSettings(store, appkit_ejections)

        self._override_path = f"{db_path}.overrides.json" if db_path else ""
        self._autonomous_path = f"{db_path}.autonomous.json" if db_path else ""
        self._assist_path = f"{db_path}.assist.json" if db_path else ""
        self._quiet_path = f"{db_path}.quiet.json" if db_path else ""
        self._last_model_path = f"{db_path}.last_model.json" if db_path else ""

        self._model_overrides = self._load_overrides()
        self._autonomous = self._load_autonomous()
        self._assist = self._load_assist()
        self._quiet = self._load_quiet()

        # Order C: per-conversation lock for atomic pre-kick settings changes.
        # Acquired by apply_settings_change so concurrent PATCHes are serialized
        # and a PATCH can't race the loop-compose+register step in kick().
        self._settings_locks: dict[str, asyncio.Lock] = {}

    def _get_model_override(self, conversation_id: str) -> str | None:
        return self._model_overrides.get(conversation_id)

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
        apply_settings_change (which holds the lock) or from set_model_override
        (non-route, non-concurrent callers that don't need the atomic gate)."""
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
        call ONLY from apply_settings_change (which holds the per-cid lock)."""
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
        uses apply_settings_change so the atomic pristine check covers the change."""
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

    # ---- surface map -------------------------------------------------------

    def _load_surfaces(self) -> dict[str, str]:
        return self._surface_settings._load()

    def _save_surfaces(self) -> None:
        self._surface_settings._save()

    def _set_surface(self, conversation_id: str, surface: str) -> None:
        self._surface_settings._set(conversation_id, surface)

    def _surface_of(self, conversation_id: str) -> str:
        return self._surface_settings._get(conversation_id)

    # ---- autonomous (headless) ---------------------------------------------

    def _load_autonomous(self) -> dict[str, bool]:
        if self._autonomous_path and os.path.exists(self._autonomous_path):
            try:
                with open(self._autonomous_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001 — corrupt/missing → start empty
                return {}
        return {}

    def _save_autonomous(self) -> None:
        if not self._autonomous_path:
            return
        import tempfile

        try:
            dir_name = os.path.dirname(self._autonomous_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._autonomous, f)
                tmp_name = f.name
            os.replace(tmp_name, self._autonomous_path)
        except Exception:  # noqa: BLE001 — best-effort, but VISIBLE (a silent
            # no-op here cost autonomy-across-restart, live-caught 2026-07-03)
            logger.warning("autonomous sidecar save failed", exc_info=True)

    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
        """Mark a conversation autonomous (headless) BEFORE it runs. Persisted (B0)."""
        self._autonomous[conversation_id] = bool(value)
        self._save_autonomous()

    def _effective_autonomous(self, conversation_id: str) -> bool:
        """The SINGLE source of truth for "is this conversation actually running
        headless". Autonomous governs the plan-gate auto-approve + ask/clarify
        suppression, so it only takes effect on surfaces that HAVE a plan gate:
        build, agent, AND deep_research (plan→iterate→report). Gating in ONE place
        keeps the prompt prefix (router), the loop's tool-suppression/auto-approve,
        AND the UI badge from disagreeing. The plain `research` surface has no plan
        gate, so an autonomous=True flag there is uniformly treated as interactive."""
        return (
            self._autonomous.get(conversation_id, False)
            and self._surface_of(conversation_id) in _AUTONOMOUS_SURFACES
        )

    def is_autonomous(self, conversation_id: str) -> bool:
        # The public read (UI badge via /state extras) — gated, so the badge can't
        # show "autonomous" on a surface that has no headless affordance.
        return self._effective_autonomous(conversation_id)

    # ---- quiet mode --------------------------------------------------------

    def _load_quiet(self) -> dict[str, bool]:
        if self._quiet_path and os.path.exists(self._quiet_path):
            try:
                with open(self._quiet_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_quiet(self) -> None:
        if not self._quiet_path:
            return
        import tempfile

        try:
            dir_name = os.path.dirname(self._quiet_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._quiet, f)
                tmp_name = f.name
            os.replace(tmp_name, self._quiet_path)
        except Exception:  # noqa: BLE001
            pass

    def set_quiet(self, conversation_id: str, value: bool = True) -> None:
        """Mark a build-like conversation as quiet. Persisted (B0)."""
        self._quiet[conversation_id] = bool(value)
        self._save_quiet()

    def _effective_quiet(self, conversation_id: str) -> bool:
        return (
            self._quiet.get(conversation_id, False)
            and self._surface_of(conversation_id) in _BUILD_LIKE_SURFACES
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
        if self._assist_path and os.path.exists(self._assist_path):
            try:
                with open(self._assist_path) as f:
                    return {str(k): bool(v) for k, v in json.load(f).items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_assist(self) -> None:
        if not self._assist_path:
            return
        import tempfile

        try:
            dir_name = os.path.dirname(self._assist_path)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as f:
                json.dump(self._assist, f)
                tmp_name = f.name
            os.replace(tmp_name, self._assist_path)
        except Exception:  # noqa: BLE001
            pass

    def _set_assist_unlocked(self, conversation_id: str, value: bool) -> None:
        """Inner setter — does NOT acquire the per-cid lock; call ONLY from
        apply_settings_change (which holds the lock) or from set_assist
        (non-route, non-concurrent callers that don't need the atomic gate)."""
        self._assist[conversation_id] = bool(value)
        self._save_assist()

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

        compose_model_key = self._model_bindings.compose_model_key(conversation_id)
        override = (
            compose_model_key
            if compose_model_key is not None
            else self._model_overrides.get(conversation_id)
        )
        # This is a metadata read used by every /state response (the Assist badge).
        # Building a live router here needlessly resolves/decrypts every provider and
        # logs every disapproved catalogue entry on every poll.  The policy needs only
        # the current config entry; actual model calls still rebuild the live router.
        config = self._routing.config_now()
        key = config.model_for(ModelRole.AGENT_DRIVER, override=override)
        entry = config.models.get(key)
        anchored = entry is not None and Requirement.ANCHORED_EDIT in entry.capabilities
        return resolve_policy(
            assist_override=self._assist.get(conversation_id),
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

        compose_model_key = self._model_bindings.compose_model_key(conversation_id)
        override = (
            compose_model_key
            if compose_model_key is not None
            else self._model_overrides.get(conversation_id)
        )
        # Tool-context metadata resolution needs the selected endpoint, not a live
        # provider object.  Avoid provider construction on read-only setup paths.
        config = self._routing.config_now()
        key = config.model_for(ModelRole.AGENT_DRIVER, override=override)
        entry = config.models.get(key)
        if entry is None or not entry.base_url:
            return None
        if not self._routing.origin_approved(
            entry.base_url,
            f"model:{entry.provider}",
            entry.api_key_env,
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
        if self._model_bindings.loop_exists(conversation_id):
            return False
        if self._model_bindings.live_task_exists(conversation_id):
            return False
        # --- event-store check (async, catches post-restart state) ---
        events = await self._store.get_events(conversation_id)
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
        if self._model_bindings.live_task_exists(conversation_id):
            return (False, False)
        # Authoritative status from the event store (covers post-restart state too).
        state = await self._store.get_state(conversation_id)
        status = state.execution_status
        if status in _TERMINAL_SETTABLE_STATES:
            return (True, True)
        if status == ConversationStatus.IDLE:
            # IDLE splits two ways: an INTERRUPTED run (approved plan, never FINISHED) is
            # a terminal-style resume target → settable; a truly-fresh IDLE conversation
            # falls through to the strict pristine check (which also catches the
            # compose-gap: a loop composed / task scheduled but RUNNING not yet emitted).
            events = await self._store.get_events(conversation_id)
            has_plan = any(isinstance(event, PlanEvent) for event in events)
            has_finished = any(
                isinstance(event, StatusEvent) and event.status == ConversationStatus.FINISHED
                for event in events
            )
            if has_plan and not has_finished:
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
            if self._model_bindings.live_task_exists(conversation_id):
                return False
            if not terminal and self._model_bindings.loop_exists(conversation_id):
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
                self._model_bindings.evict_for_model_change(conversation_id)
            return True

    # ---- artifact_mode (C6) ------------------------------------------------

    def set_artifact_mode(self, conversation_id: str, on: bool) -> None:
        self._mode_settings._set_artifact(conversation_id, on)

    def _effective_artifact_mode(self, conversation_id: str) -> bool:
        return self._mode_settings._artifact_enabled(conversation_id)

    # ---- appkit_mode (EPIC F) ----------------------------------------------

    def set_appkit_mode(self, conversation_id: str, on: bool) -> None:
        self._mode_settings._set_appkit(conversation_id, on)

    def _effective_appkit_mode(self, conversation_id: str) -> bool:
        return self._mode_settings._appkit_enabled(conversation_id)

    # ---- per-query research sources ---------------------------------------

    def set_research_sources(self, conversation_id: str, sources: list[str]) -> None:
        self._mode_settings._set_research_sources(conversation_id, sources)

    def get_research_sources(self, conversation_id: str | None) -> tuple[str, ...]:
        return self._mode_settings._get_research_sources(conversation_id)
