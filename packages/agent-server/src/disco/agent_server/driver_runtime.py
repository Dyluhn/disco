"""Owned driver routing, context resolution, catalog, and preflight state.

The owners use concrete stores and narrow Protocol ports. Live-probe authority
stays with ``runtime.py`` through ``DriverProbeSeams`` so its established
module-level monkeypatch seams remain authoritative after wiring.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, TypedDict

from disco.core import LLMMessage, SkillStore, render_skills_for_prompt
from disco.core.inspect import routing_sink_for
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    ConfigStore,
    DefaultLLMRouter,
    DriverPrompts,
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
    ModelEntry,
    ModelRole,
    NoEligibleModel,
    Requirement,
    RouterConfig,
    RoutingDecision,
    SecretStore,
)
from disco.core.llm.secret_refs import resolve_provider_secret, secret_ref_allowed_for_origin
from disco.core.llm.wiring import build_providers, probe_all_vision_with_approvals
from disco.core.loop import host_verify_authoritative_enabled

from .driver_context import DriverContextResolutionError, ResolvedDriverContext
from .driver_context_state import DriverContextState

logger = logging.getLogger(__name__)

_GENERATIVE_ROLES = (
    ModelRole.AGENT_DRIVER,
    ModelRole.RAG_ANSWERER,
    ModelRole.QUERY_REWRITER,
    ModelRole.SUMMARIZER,
)


class LiveModelProbe(TypedDict):
    model_id: str | None
    n_ctx: int | None


class DriverProbeSeams(Protocol):
    """Late-bound access to runtime.py's monkeypatch-compatible probe seams."""

    def probe_live_model(
        self,
        base_url: str | None,
        api_key: str | None = None,
        model_id: str | None = None,
    ) -> LiveModelProbe: ...

    def do_live_model_probe(
        self,
        base_url: str,
        api_key: str | None,
        model_id: str | None = None,
    ) -> LiveModelProbe: ...

    def model_label(self, model_id: str) -> str: ...


class DriverSelections(Protocol):
    """The settings-owned inputs used for one conversation's driver."""

    def model_override(self, conversation_id: str) -> str | None: ...


class DriverPromptState(Protocol):
    """Dynamic executor state that changes prompt routing without owning models."""

    def workflow_router_enabled(self) -> bool: ...

    def workflow_router_active(self, conversation_id: str) -> bool: ...

    def appkit_mode_active(self, conversation_id: str) -> bool: ...


class DriverReadinessObserver(Protocol):
    """Accept successful live routing as exact, bounded readiness evidence."""

    def __call__(
        self,
        bound_conversation_id: str | None,
        model_key: str,
        decision: RoutingDecision,
        context: CallContext,
    ) -> None: ...


class DriverRuntime:
    """Model metadata, routing composition, probes, policy, and context authority."""

    def __init__(
        self,
        *,
        config_store: ConfigStore,
        secret_store: SecretStore,
        skill_store: SkillStore,
        prompt_state: DriverPromptState,
        selections: DriverSelections,
        probes: DriverProbeSeams,
        contexts: DriverContextState,
        injected_router: DefaultLLMRouter | None = None,
        enable_thinking: bool = False,
    ) -> None:
        self._config_store = config_store
        self._secret_store = secret_store
        self._skill_store = skill_store
        self._prompt_state = prompt_state
        self._selections = selections
        self._probes = probes
        self._contexts = contexts
        self._injected_router = injected_router
        self._enable_thinking = enable_thinking
        self._readiness_observer: DriverReadinessObserver | None = None

    def bind_readiness_observer(self, observer: DriverReadinessObserver) -> None:
        self._readiness_observer = observer

    @property
    def contexts(self) -> DriverContextState:
        return self._contexts

    def config_now(self) -> RouterConfig:
        if self._injected_router is not None:
            return self._injected_router._config
        return self._config_store.load()

    def router(
        self,
        pick: str | None = None,
        *,
        enable_thinking: bool | None = None,
        surface: str | None = None,
        autonomous: bool = False,
        conversation_id: str | None = None,
        appkit_mode: bool = False,
    ) -> DefaultLLMRouter:
        if self._injected_router is not None:
            return self._injected_router
        cfg = self._config_store.load()
        if pick and pick in cfg.models:
            assignments = {**cfg.assignments, **{role: pick for role in _GENERATIVE_ROLES}}
            cfg = cfg.model_copy(update={"assignments": assignments})
        thinking = self._enable_thinking if enable_thinking is None else enable_thinking
        providers = build_providers(
            cfg,
            enable_thinking=thinking,
            origin_approved=self._origin_approved,
        )
        skills_block = render_skills_for_prompt(self._skill_store.enabled(), surface=surface)
        flavor = "agent" if surface == "agent" else "build"
        workflow_active: Callable[[], bool] | None = None
        if self._prompt_state.workflow_router_enabled() and surface == "agent" and conversation_id:
            cid = conversation_id

            def _workflow_active() -> bool:
                return self._prompt_state.workflow_router_active(cid)

            workflow_active = _workflow_active
        appkit_active: Callable[[], bool] | None = None
        if appkit_mode and conversation_id:
            cid = conversation_id

            def _appkit_active() -> bool:
                return self._prompt_state.appkit_mode_active(cid)

            appkit_active = _appkit_active

        def _observe_success(
            model_key: str,
            decision: RoutingDecision,
            context: CallContext,
        ) -> None:
            observer = self._readiness_observer
            if observer is not None:
                observer(conversation_id, model_key, decision, context)

        router = DefaultLLMRouter(
            cfg,
            providers,
            prompt_provider=DriverPrompts(
                skills_block=skills_block,
                flavor=flavor,
                autonomous=autonomous,
                host_verify_authoritative=host_verify_authoritative_enabled(),
                workflow_router_active=workflow_active,
                appkit_mode=appkit_mode,
                appkit_mode_active=appkit_active,
            ),
            sink=routing_sink_for(conversation_id),
        )
        router._bind_success_observer(_observe_success)
        return router

    def context_window(self, conversation_id: str | None = None) -> int | None:
        try:
            cfg = self.config_now()
            override = (
                self._selections.model_override(conversation_id)
                if conversation_id is not None
                else None
            )
            key = cfg.model_for(ModelRole.AGENT_DRIVER, override=override)
            if key not in cfg.models and override is not None:
                key = cfg.model_for(ModelRole.AGENT_DRIVER)
            entry = cfg.entry_for(key)
            if not entry.base_url or not self._origin_approved(
                entry.base_url,
                f"model:{entry.provider}",
                entry.api_key_env,
            ):
                return entry.context_window
            if not secret_ref_allowed_for_origin(entry.api_key_env, entry.base_url):
                return entry.context_window
            api_key = self._resolve_secret(entry.api_key_env)
            if entry.api_key_env and not api_key:
                return entry.context_window
            live = self._probes.probe_live_model(entry.base_url, api_key, entry.model_id)
            return live["n_ctx"] or entry.context_window
        except Exception:  # noqa: BLE001 — context metadata must not block composition
            return None

    async def resolve_context(self, conversation_id: str) -> ResolvedDriverContext:
        override = self._selections.model_override(conversation_id)
        unresolved_key = override or "<configured-default>"
        key, entry = self._selected_entry(override, unresolved_key)
        base_url = entry.base_url or None
        try:
            approved = base_url is not None and self._origin_approved(
                base_url,
                f"model:{entry.provider}",
                entry.api_key_env,
            )
            secret_allowed = base_url is None or secret_ref_allowed_for_origin(
                entry.api_key_env,
                base_url,
            )
            api_key = self._resolve_secret(entry.api_key_env) if base_url else None
        except Exception as exc:
            raise DriverContextResolutionError(
                model_key=key,
                provider=entry.provider,
                reason=f"driver credential policy could not be resolved ({type(exc).__name__})",
            ) from exc
        if base_url is None or not approved:
            source = "unapproved_origin" if base_url else None
            return await self._contexts.resolve(
                model_key=key,
                entry=entry,
                probe=None,
                unavailable_source=source,
            )
        if not secret_allowed:
            return await self._contexts.resolve(
                model_key=key,
                entry=entry,
                probe=None,
                unavailable_source="disallowed_secret_ref",
            )
        if entry.api_key_env and not api_key:
            return await self._contexts.resolve(
                model_key=key,
                entry=entry,
                probe=None,
                unavailable_source="missing_secret",
            )

        async def probe() -> dict[str, Any]:
            result = await asyncio.to_thread(
                self._probes.probe_live_model,
                base_url,
                api_key,
                entry.model_id,
            )
            return dict(result)

        return await self._contexts.resolve(
            model_key=key,
            entry=entry,
            probe=probe,
            unavailable_source=None,
        )

    async def prewarm_model_probe(self) -> None:
        try:
            cfg = self._config_store.load()
            entry = cfg.entry_for(cfg.model_for(ModelRole.AGENT_DRIVER))
            if not entry.base_url or not self._origin_approved(
                entry.base_url,
                f"model:{entry.provider}",
                entry.api_key_env,
            ):
                return
            if not secret_ref_allowed_for_origin(entry.api_key_env, entry.base_url):
                return
            api_key = self._resolve_secret(entry.api_key_env)
            if entry.api_key_env and not api_key:
                return
            await asyncio.to_thread(
                self._probes.do_live_model_probe,
                entry.base_url,
                api_key,
                entry.model_id,
            )
        except Exception:  # noqa: BLE001 — best-effort startup warm
            pass

    async def prewarm_vision_probe(self) -> None:
        try:
            self._config_store.apply_vision_probe(
                await probe_all_vision_with_approvals(
                    self._config_store.load(),
                    origin_approved=self._origin_approved,
                )
            )
        except Exception:  # noqa: BLE001 — static config remains authoritative fallback
            pass

    def catalog(self) -> dict[str, object]:
        cfg = self._config_store.load()
        models: list[dict[str, object]] = []
        try:
            default = cfg.model_for(ModelRole.AGENT_DRIVER)
        except Exception:  # noqa: BLE001 — no assignment means no highlighted default
            default = None

        # A provider/model pair is one executable route, but the same model ID
        # served by another provider is a distinct selectable route. Within an
        # exact route alias group, prefer the configured driver default; if it
        # cannot be wired, retain the first eligible alias as before.
        groups: dict[tuple[str, str], list[tuple[str, ModelEntry]]] = {}
        for key, entry in cfg.models.items():
            groups.setdefault((entry.provider, entry.model_id), []).append((key, entry))
        for candidates in groups.values():
            candidates.sort(key=lambda item: item[0] != default)
            for key, entry in candidates:
                model = self._catalog_model(key, entry)
                if model is not None:
                    models.append(model)
                    break
        selected = default if any(model["id"] == default for model in models) else None
        return {"models": models, "default": selected}

    def _selected_entry(
        self,
        override: str | None,
        unresolved_key: str,
    ) -> tuple[str, ModelEntry]:
        try:
            cfg = self.config_now()
            key = cfg.model_for(ModelRole.AGENT_DRIVER, override=override)
            if key not in cfg.models and override is not None:
                key = cfg.model_for(ModelRole.AGENT_DRIVER)
            return key, cfg.entry_for(key)
        except Exception as exc:
            raise DriverContextResolutionError(
                model_key=unresolved_key,
                provider="<unresolved>",
                reason=f"invalid driver configuration ({type(exc).__name__})",
            ) from exc

    def _catalog_model(
        self,
        key: str,
        entry: ModelEntry,
    ) -> dict[str, object] | None:
        if entry.base_url is None or Requirement.TOOL_CALLING not in entry.capabilities:
            return None
        if not self._origin_approved(
            entry.base_url,
            f"model:{entry.provider}",
            entry.api_key_env,
        ):
            return None
        if not secret_ref_allowed_for_origin(entry.api_key_env, entry.base_url):
            return None
        api_key = self._resolve_secret(entry.api_key_env)
        if entry.api_key_env and not api_key:
            return None
        live = self._probes.probe_live_model(entry.base_url, api_key, entry.model_id)
        pricing_mode = entry.pricing_mode
        if pricing_mode is None:
            pricing_mode = "free" if entry.price_out_per_m == 0.0 else "metered"
        return {
            "id": key,
            "label": self._probes.model_label(live["model_id"] or entry.model_id),
            "provider": "openrouter" if entry.provider == "openrouter" else "local",
            "free": pricing_mode == "free",
            "pricing_mode": pricing_mode,
            "context_window": live["n_ctx"] or entry.context_window,
            "capabilities": sorted(capability.value for capability in entry.capabilities),
        }

    def _resolve_secret(self, name: str | None) -> str | None:
        return resolve_provider_secret(name, self._secret_store)

    def _origin_approved(
        self,
        url: str,
        purpose: str,
        secret_ref: str | None = "",
    ) -> bool:
        return self._config_store.approvals.origin_approved(
            url,
            purpose,
            secret_ref,
            secret_store=self._secret_store,
        )

    def _model_override(self, conversation_id: str) -> str | None:
        return self._selections.model_override(conversation_id)

    def _has_injected_router(self) -> bool:
        return self._injected_router is not None


class DriverPreflight:
    """Own bounded, singleflight driver readiness and per-conversation proof."""

    _TTL_S = 60.0
    _TIMEOUT_S = 10.0
    _ATTEMPTS = 3
    _BACKOFF_S = 0.5
    _SHARED_RESULT_TTL_S = 2.0

    def __init__(self, drivers: DriverRuntime) -> None:
        self._drivers = drivers
        self._ok: dict[str, float] = {}
        self._inflight: dict[
            str,
            tuple[asyncio.Task[tuple[str | None, bool]], float | None],
        ] = {}
        self._proven: set[tuple[str, ModelRole, str]] = set()

    async def check(
        self,
        conversation_id: str | None,
        *,
        override: str | None = None,
        role: ModelRole = ModelRole.AGENT_DRIVER,
    ) -> str | None:
        if self._drivers._has_injected_router():
            return None
        cid = conversation_id or ""
        override = override if override is not None else self._drivers._model_override(cid)
        cfg = self._drivers.config_now()
        key = self._model_key(cfg, override, role)
        proof = (cid, role, key)
        cached = self._ok.get(key)
        if cached is not None and time.monotonic() - cached < self._TTL_S:
            self._proven.add(proof)
            self._record_success(cid, cfg, key, override, role, "cached")
            return None
        slot = self._fresh_slot(key)
        created = slot is None
        if slot is None:
            task = asyncio.create_task(
                self._probe(cid, key=key, override=override, role=role),
                name=f"driver-preflight:{key}",
            )
            self._inflight[key] = (task, None)
            task.add_done_callback(
                lambda completed, model_key=key: self._complete(model_key, completed)
            )
        else:
            task = slot[0]
        try:
            result, transient = await asyncio.shield(task)
        except asyncio.CancelledError:
            self._drop_cancelled(key, task)
            raise
        except Exception:
            self._drop_if_current(key, task)
            raise
        if result is None:
            self._drop_if_current(key, task)
            self._proven.add(proof)
            if not created:
                self._record_success(cid, cfg, key, override, role, "shared")
            return None
        self._complete(key, task)
        if self._accept_concurrent_live_success(transient, proof, cfg, override):
            return None
        if transient and proof in self._proven:
            logger.warning(
                "driver pre-flight for %r (role=%s) failed transiently (%s) but "
                "already succeeded in conversation %s — proceeding",
                key,
                role,
                result,
                cid or "<none>",
            )
            return None
        self._record_failure(cid, cfg, key, override, role)
        return result

    def discard_conversation(self, conversation_id: str) -> None:
        self._proven = {proof for proof in self._proven if proof[0] != conversation_id}

    def _accept_concurrent_live_success(
        self,
        transient: bool,
        proof: tuple[str, ModelRole, str],
        cfg: RouterConfig,
        override: str | None,
    ) -> bool:
        conversation_id, role, key = proof
        refreshed = self._ok.get(key)
        if not transient or refreshed is None or time.monotonic() - refreshed >= self._TTL_S:
            return False
        self._proven.add(proof)
        self._record_success(conversation_id, cfg, key, override, role, "live")
        return True

    def observe_success(
        self,
        bound_conversation_id: str | None,
        model_key: str,
        decision: RoutingDecision,
        context: CallContext,
    ) -> None:
        if decision.path not in ("pinned", "manual"):
            return
        cfg = self._drivers.config_now()
        role = decision.profile.role
        override = context.model_override if role == ModelRole.AGENT_DRIVER else None
        if self._model_key(cfg, override, role) != model_key:
            return
        entry = cfg.models.get(model_key)
        if (
            entry is None
            or entry.model_id != decision.chosen_model
            or entry.provider != decision.provider
        ):
            return
        self._ok[model_key] = time.monotonic()
        conversation_id = context.conversation_id or bound_conversation_id or ""
        if conversation_id:
            self._proven.add((conversation_id, role, model_key))

    async def aclose(self) -> None:
        tasks = {slot[0] for slot in self._inflight.values()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

    def _fresh_slot(
        self,
        key: str,
    ) -> tuple[asyncio.Task[tuple[str | None, bool]], float | None] | None:
        slot = self._inflight.get(key)
        if slot is None:
            return None
        task, completed_at = slot
        if (
            task.done()
            and completed_at is not None
            and time.monotonic() - completed_at >= self._SHARED_RESULT_TTL_S
        ):
            self._inflight.pop(key, None)
            return None
        return slot

    @staticmethod
    def _model_key(
        cfg: RouterConfig,
        override: str | None,
        role: ModelRole,
    ) -> str:
        if override and override in cfg.models:
            return override
        try:
            return cfg.model_for(role)
        except Exception:  # noqa: BLE001 — resolution failure gets a generic label
            return "?"

    def _complete(
        self,
        key: str,
        task: asyncio.Task[tuple[str | None, bool]],
    ) -> None:
        current = self._inflight.get(key)
        if current is None or current[0] is not task or current[1] is not None:
            return
        if task.cancelled():
            self._inflight.pop(key, None)
            return
        try:
            result, _transient = task.result()
        except BaseException:
            self._inflight.pop(key, None)
            return
        if result is None:
            self._inflight.pop(key, None)
            return
        completed_at = time.monotonic()
        self._inflight[key] = (task, completed_at)
        task.get_loop().call_later(
            self._SHARED_RESULT_TTL_S,
            self._expire,
            key,
            task,
            completed_at,
        )

    def _expire(
        self,
        key: str,
        task: asyncio.Task[tuple[str | None, bool]],
        completed_at: float,
    ) -> None:
        if self._inflight.get(key) == (task, completed_at):
            self._inflight.pop(key, None)

    async def _probe(
        self,
        conversation_id: str,
        *,
        key: str,
        override: str | None,
        role: ModelRole,
    ) -> tuple[str | None, bool]:
        router = self._drivers.router(pick=override, conversation_id=conversation_id)
        request = CompletionRequest(
            profile=CapabilityProfile(role=role),
            messages=[LLMMessage(role="user", content="ping")],
            max_tokens=1,
        )
        transient_reason: str | None = None
        for attempt in range(self._ATTEMPTS):
            try:
                await asyncio.wait_for(
                    router.complete(
                        request,
                        context=CallContext(
                            conversation_id=conversation_id,
                            model_override=override,
                        ),
                    ),
                    self._TIMEOUT_S,
                )
                transient_reason = None
                break
            except TimeoutError:
                transient_reason = (
                    f"Driver '{key}' unreachable: no response within "
                    f"{self._TIMEOUT_S:.0f}s (pre-flight timed out)"
                )
            except (LLMContentFiltered, LLMContextWindowExceeded):
                transient_reason = None
                break
            except NoEligibleModel as exc:
                return f"Driver '{key}' is misconfigured: {exc}", False
            except LLMAuthError as exc:
                return f"Driver '{key}' rejected the API key: {exc}", False
            except LLMProviderUnavailable as exc:
                return f"Driver '{key}' is unavailable: {exc}", False
            except LLMTransientError as exc:
                transient_reason = f"Driver '{key}' unreachable: {exc}"
            except LLMError as exc:
                return f"Driver '{key}' error: {exc}", False
            except Exception as exc:  # noqa: BLE001 — never expose transport details
                return f"Driver '{key}' pre-flight failed ({type(exc).__name__})", False
            if attempt + 1 < self._ATTEMPTS:
                await asyncio.sleep(self._BACKOFF_S * (attempt + 1))
        if transient_reason is not None:
            return transient_reason, True
        self._ok[key] = time.monotonic()
        return None, False

    def _drop_cancelled(
        self,
        key: str,
        task: asyncio.Task[tuple[str | None, bool]],
    ) -> None:
        current = self._inflight.get(key)
        if current is not None and current[0] is task and task.cancelled():
            self._inflight.pop(key, None)

    def _drop_if_current(
        self,
        key: str,
        task: asyncio.Task[tuple[str | None, bool]],
    ) -> None:
        current = self._inflight.get(key)
        if current is not None and current[0] is task:
            self._inflight.pop(key, None)

    @staticmethod
    def _record_success(
        conversation_id: str,
        cfg: RouterConfig,
        key: str,
        override: str | None,
        role: ModelRole,
        source: str,
    ) -> None:
        sink = routing_sink_for(conversation_id)
        if sink is None:
            return
        entry = cfg.models.get(key)
        sink.record(
            RoutingDecision(
                profile=CapabilityProfile(role=role),
                chosen_model=entry.model_id if entry is not None else key,
                provider=entry.provider if entry is not None else "",
                path="manual" if override is not None else "pinned",
                reason=f"{source} driver preflight success",
                overflow_triggers=["driver_preflight"],
            )
        )

    @staticmethod
    def _record_failure(
        conversation_id: str,
        cfg: RouterConfig,
        key: str,
        override: str | None,
        role: ModelRole,
    ) -> None:
        sink = routing_sink_for(conversation_id)
        if sink is None:
            return
        entry = cfg.models.get(key)
        sink.record(
            RoutingDecision(
                profile=CapabilityProfile(role=role),
                chosen_model=entry.model_id if entry is not None else key,
                provider=entry.provider if entry is not None else "",
                path="manual" if override is not None else "pinned",
                reason="terminal failure: shared driver preflight",
                overflow_triggers=["driver_preflight"],
            )
        )
