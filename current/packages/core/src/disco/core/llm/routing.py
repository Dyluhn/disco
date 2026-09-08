"""The Router — llm-router-contract.md §4 (v1.2 deterministic config-driven).

The single entry point callers use. v1.2 LOBOTOMY demoted it from intelligent
routing to a deterministic, config-driven lookup pipeline. It still owns:
role→model resolution (now a direct config lookup, no policy), prompt-variant
injection, provider execution, passive cost observation, and emitting the
RoutingDecision. What it NO LONGER owns: the overflow policy, difficulty
assessment, capability-based escalation, and local-failure/stuck/low-confidence
escalation — all preserved DORMANT (commented) at the bottom of this file and in
policy.py / config.py, so the revival path is a diff. See the v1.2 contract.

Guarantees (RT1–RT4) still hold:
  RT1 complete() returns a CompletionResponse with .routing and .usage
      populated, or raises a typed LLMError.
  RT2 (v1.2 amended) the model is selected by config assignment — `default_model`
      / `assignments[role]` — optionally overridden per conversation by the model
      pill (CallContext.model_override). Callers still cannot pin an arbitrary
      name through the request; the override is an explicit operator surface.
  RT3 the matching prompt variant is injected when the caller supplied no system
      message.
  RT4 every call emits exactly one RoutingDecision (success or terminal failure)
      to the sink. The decision's path is "pinned" (settings assignment) or
      "manual" (per-conversation override); reason is "config".

[EXTENSION] `CallContext` threads the loop's runtime signals + the conversation
id (for passive per-conversation cost) and, in v1.2, the per-conversation
`model_override` (the main-screen model pill). `complete`/`stream_complete` take
it as an optional keyword-only `context`; callers using `complete(req)` alone are
unaffected.
"""

from __future__ import annotations

# Compatibility facade: retry constants remain historical monkeypatch seams.
# ruff: noqa: F401
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from typing import Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..events import LLMMessage
from ._router_execution import (
    _AUTH_RETRY_DELAY_S,
    _MAX_ATTEMPTS,
    _RETRY_BACKOFF_BASE_S,
    _ROLE_FALLBACK_MAX_ATTEMPTS,
)
from ._router_execution import (
    execute_complete as _execute_complete,
)
from ._router_execution import (
    execute_stream_complete as _execute_stream_complete,
)
from .config import ROLE_FALLBACK_PROVIDER_KEY, ModelEntry, RouterConfig
from .errors import (
    BudgetExceeded,
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMTransientError,
    NoEligibleModel,
)
from .prompts import (
    PromptProvider,
    StaticPromptProvider,
    default_mode_for_role,
    derive_family,
)
from .provider import ModelProvider
from .request_budget import RequestBudgetEstimate
from .types import (
    DRIVER_ROLES,
    FALLBACK_ELIGIBLE_ROLES,
    CompletionRequest,
    CompletionResponse,
    ModelRole,
    Requirement,
    RoutingDecision,
    StreamChunk,
)

_LOG = logging.getLogger(__name__)

# The routing paths. v1.2 emits "pinned"/"manual"; "local"/"overflow" are kept in
# the Literal for the DORMANT intelligent-routing revival path and for back-compat
# of any persisted decisions.
Path = Literal["local", "overflow", "pinned", "manual", "role_fallback"]


class CallContext(BaseModel):
    """[EXTENSION] Optional per-call runtime context from the loop. See module
    docstring.

    v1.2 adds `model_override`: the model KEY chosen for THIS conversation by the
    main-screen model pill. It overrides the settings assignment for the
    AGENT_DRIVER role only (the model that "leads the show"); other roles always
    follow their settings assignment. None means "use the settings assignment".

    `consecutive_tool_errors`/`last_local_confidence` are now ADVISORY (carried for
    observability); the deterministic router does not route on them."""

    model_config = ConfigDict(frozen=True)
    conversation_id: str | None = None
    consecutive_tool_errors: int = 0
    last_local_confidence: float | None = None
    model_override: str | None = None  # v1.2 per-conversation model pill (driver)


# ---- observability sink (RT4) -----------------------------------------------


@runtime_checkable
class RoutingSink(Protocol):
    """[CONTRACT boundary] Receives one RoutingDecision per call for the
    metrics/audit layer (BoD §18/§20)."""

    def record(self, decision: RoutingDecision) -> None: ...


class NullRoutingSink:
    """Default sink: drops decisions (used when observability isn't wired)."""

    def record(self, decision: RoutingDecision) -> None:  # noqa: D102
        return None


class InMemoryRoutingSink:
    """Collects decisions in a list. For tests and simple local observability."""

    def __init__(self) -> None:
        self.decisions: list[RoutingDecision] = []

    def record(self, decision: RoutingDecision) -> None:
        self.decisions.append(decision)


# ---- cost accounting (§5.2) — PASSIVE in v1.2 -------------------------------


class CostTracker:
    """Running cost, global and per-conversation. [INTERIOR] accounting store.

    `*_daily` global caps are modeled as a single running total in v1 (no day
    rollover); the door to a real time-windowed store is open behind this class.
    v1.2: this still ACCRUES spend on every call (observability), and the hard cap
    is honored as a spend backstop, but cost never chooses a model.
    """

    def __init__(self) -> None:
        self._global = 0.0
        self._per_conversation: dict[str, float] = defaultdict(float)

    def add(self, cost_usd: float, conversation_id: str | None) -> None:
        self._global += cost_usd
        if conversation_id is not None:
            self._per_conversation[conversation_id] += cost_usd

    def global_cost(self) -> float:
        return self._global

    def conversation_cost(self, conversation_id: str | None) -> float:
        if conversation_id is None:
            return 0.0
        return self._per_conversation[conversation_id]


def _hard_cap_exceeded(
    accrued_conv: float,
    accrued_global: float,
    config: RouterConfig,
) -> bool:
    """True if either HARD cap (per-conversation or global) has been reached.
    v1.2: the SOFT caps are observe-only (they no longer suppress anything, since
    there is no discretionary overflow to suppress)."""

    def hit(accrued: float, cap: float | None) -> bool:
        return cap is not None and accrued >= cap

    return hit(accrued_conv, config.hard_budget_usd_per_conversation) or hit(
        accrued_global, config.hard_budget_usd_global_daily
    )


# ---- the router protocol + default implementation ---------------------------


@runtime_checkable
class LLMRouter(Protocol):
    """[CONTRACT] The single entry point for all model calls."""

    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...

    # NOTE: declared as a plain `def` returning an AsyncIterator — the correct
    # type for an async-generator method (you iterate the result directly with
    # `async for`, never `await` it). The contract wrote `async def`, which types
    # as a coroutine-returning-an-iterator and would force `async for x in await
    # ...`; the async-generator form below matches the streaming usage and the
    # fake/real adapters. (Same reasoning applies to ModelProvider.stream_complete.)
    def stream_complete(self, req: CompletionRequest) -> AsyncIterator[StreamChunk]: ...


class DefaultLLMRouter:
    """[CONTRACT] The reference LLMRouter (RT1–RT4), v1.2 deterministic."""

    def __init__(
        self,
        config: RouterConfig,
        providers: dict[str, ModelProvider],
        *,
        prompt_provider: PromptProvider | None = None,
        sink: RoutingSink | None = None,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        self._config = config
        self._providers = providers
        self._prompts = prompt_provider or StaticPromptProvider()
        self._sink = sink or NullRoutingSink()
        self._cost = cost_tracker or CostTracker()
        self._on_success: Callable[[str, RoutingDecision, CallContext], None] | None = None

    def _bind_success_observer(
        self,
        observer: Callable[[str, RoutingDecision, CallContext], None],
    ) -> None:
        self._on_success = observer

    # -- resolution (v1.2: direct config lookup, no policy) -------------------

    def _resolve(
        self, req: CompletionRequest, ctx: CallContext
    ) -> tuple[str, ModelEntry, Path, str, list[str]]:
        """Deterministic role→model resolution. Returns (model key, entry, path,
        reason, overflow_triggers).

        Precedence is owned by `RouterConfig.model_for`: per-conversation override
        (driver only) > settings assignment > default_model.

        REACTIVE error surfacing (no pre-call capability prediction): the router
        does NOT inspect `entry.capabilities` to decide whether the model can
        handle the request. It sends the request as assigned; if the model can't
        do what was asked, the PROVIDER says so and that error is surfaced with its
        real content (see `complete`/`_execute_and_observe`/the loop). The only
        check here is config integrity — that the assignment resolves to a model
        that actually exists in the catalogue (a misconfiguration, not a
        capability judgement)."""
        profile = req.profile

        # The model pill overrides the driver only; other roles follow settings.
        override = ctx.model_override if profile.role == ModelRole.AGENT_DRIVER else None
        key = self._config.model_for(profile.role, override=override)
        path: Path = "manual" if override is not None else "pinned"

        entry = self._config.models.get(key)
        if entry is None:
            # Config integrity (NOT a capability check): the assignment points at a
            # model that isn't in the catalogue. Fail loud — there is nothing to
            # send a request to. Capabilities are never consulted here.
            self._sink.record(
                RoutingDecision(
                    profile=profile,
                    chosen_model=key,
                    provider="",
                    path=path,
                    reason=f"assigned model '{key}' is not in the catalogue",
                    overflow_triggers=["unknown_assignment"],
                )
            )
            raise NoEligibleModel(
                f"role {profile.role.value} is assigned to unknown model '{key}' "
                f"— fix the assignment in Settings"
            )

        # [BP-00] Vision capability guard: an ordinary image-bearing request
        # stays with its assigned model, which MUST support VISION. Dedicated
        # visual inspection is a separate, bounded host capability; it must
        # never inherit a whole agent request, transcript, or tool set here.
        if any(getattr(m, "images", None) for m in req.messages):
            if Requirement.VISION not in (entry.capabilities or []):
                self._sink.record(
                    RoutingDecision(
                        profile=profile,
                        chosen_model=key,
                        provider=entry.provider,
                        path=path,
                        reason=f"model '{key}' does not support vision",
                        overflow_triggers=["capability_gap"],
                    )
                )
                raise NoEligibleModel(
                    f"Request contains images but model '{key}' does not support VISION. "
                    f"Check DISCO_DRIVER_VISION env and driver-local config."
                )

        return key, entry, path, "config", []

    def visual_inspection_route(
        self,
    ) -> tuple[Literal["main", "dedicated", "unavailable"], str | None, str]:
        """Resolve the explicit browser visual-inspection route.

        A configured dedicated model wins even when the main model can accept
        images. Without one, the exact driver assignment handles pixels when it
        advertises VISION. Invalid or text-only choices fail closed so the browser
        can return its honest DOM/text fallback instead of silently changing the
        model that owns the agent turn.
        """

        target_key = self._config.vision_escalation_model
        if target_key is not None:
            target = self._config.models.get(target_key)
            if target is None:
                return "unavailable", None, "vision_model_unknown"
            if Requirement.VISION not in target.capabilities:
                return "unavailable", target_key, "vision_model_text_only"
            return "dedicated", target_key, "configured_vision_model"

        main_key = self._config.model_for(ModelRole.AGENT_DRIVER)
        main = self._config.models.get(main_key)
        if main is None:
            return "unavailable", None, "main_model_unknown"
        if Requirement.VISION in main.capabilities:
            return "main", main_key, "main_model_has_vision"
        return "unavailable", main_key, "no_vision_model_configured"

    # -- prompt injection (RT3, §8) -------------------------------------------

    def _inject_prompt(self, req: CompletionRequest, entry: ModelEntry) -> CompletionRequest:
        if req.messages and req.messages[0].role == "system":
            return req  # caller supplied its own system prompt; do not override
        family = entry.family or derive_family(entry.model_id)
        mode = req.profile.mode or default_mode_for_role(req.profile.role)
        # [C21] Forward the assist gate to the prompt provider. Default-False on
        # the wire (req.assist=False) keeps the capable-model prompt byte-
        # identical; True selects the small-model variant in DriverPrompts.
        system = self._prompts.system_prompt(
            model_family=family,
            mode=mode,
            role=req.profile.role,
            capabilities=entry.capabilities,
            assist=req.assist,
        )
        new_messages = [LLMMessage(role="system", content=system), *req.messages]
        return req.model_copy(update={"messages": new_messages})

    @staticmethod
    def _attach_context_metadata(req: CompletionRequest, ctx: CallContext) -> CompletionRequest:
        if not ctx.conversation_id:
            return req
        metadata = {**(req.metadata or {}), "conversation_id": ctx.conversation_id}
        return req.model_copy(update={"metadata": metadata})

    # -- request budget preview -------------------------------------------------

    def request_budget_preview(
        self, req: CompletionRequest, *, context: CallContext
    ) -> RequestBudgetEstimate | None:
        """Best-effort synchronous budget preview. No routing decision, cost,
        ledger, span, or provider call. Does NOT call _resolve (which writes the
        routing sink on invalid lookups).

        Returns None for: missing model entry, missing provider, image-bearing
        requests, provider lacking the preview method, or any preview failure.
        """
        profile = req.profile

        override = context.model_override if profile.role == ModelRole.AGENT_DRIVER else None
        key = self._config.model_for(profile.role, override=override)
        if key is None:
            return None

        entry = self._config.models.get(key)
        if entry is None:
            return None

        if any(getattr(m, "images", None) for m in req.messages):
            return None

        provider = self._providers.get(entry.provider)
        if provider is None:
            return None

        preview_fn = getattr(provider, "request_budget_preview", None)
        if not callable(preview_fn):
            return None

        try:
            exec_req = self._inject_prompt(self._attach_context_metadata(req, context), entry)
            return cast(RequestBudgetEstimate | None, preview_fn(exec_req, model=entry.model_id))
        except Exception:
            return None

    # -- passive cost backstop (§5.2) -----------------------------------------

    def _enforce_hard_budget(
        self, req: CompletionRequest, entry: ModelEntry, path: Path, ctx: CallContext
    ) -> None:
        """v1.2 passive backstop: refuse to SPEND past a HARD cap. This is a spend
        guard, not a routing decision — it never picks a cheaper model, it just
        stops. Local (price 0) work never accrues, so this only ever bites a
        deliberately-assigned paid model."""
        if _hard_cap_exceeded(
            self._cost.conversation_cost(ctx.conversation_id),
            self._cost.global_cost(),
            self._config,
        ):
            self._record_failure(
                req, entry, path, "hard budget cap reached; refusing to spend", None
            )
            raise BudgetExceeded("hard cost cap reached; refusing further spend")

    def _provider_for(self, entry: ModelEntry) -> ModelProvider:
        """Resolve the live provider for a resolved entry. build_providers() SKIPS a
        provider whose origin is not operator-approved / whose secret-ref is not
        allowed for that origin / whose key is undecryptable — so a bare
        ``self._providers[entry.provider]`` raises a RAW KeyError that escapes the
        caller and crashes the run (deep research's uncaught-KeyError trap). Convert
        it into the TERMINAL, named NoEligibleModel the pre-flight + loop already
        classify and surface as a clean StatusEvent(ERROR)."""
        provider = self._providers.get(entry.provider)
        if provider is None:
            raise NoEligibleModel(
                f"model {entry.model_id!r} is unavailable: its endpoint "
                f"{entry.provider!r} was not wired — the origin is not "
                "operator-approved, its secret-ref is not allowed for that origin, "
                "or the API key could not be decrypted. Re-save the model in Settings."
            )
        return provider

    # -- complete (§4.1 step 5–6) — one assigned model, same-model retry ------

    async def complete(
        self, req: CompletionRequest, *, context: CallContext | None = None
    ) -> CompletionResponse:
        ctx = context or CallContext()
        model_key, entry, path, reason, overflow_triggers = self._resolve(req, ctx)
        self._enforce_hard_budget(req, entry, path, ctx)
        exec_req = self._inject_prompt(self._attach_context_metadata(req, ctx), entry)
        provider = self._provider_for(entry)
        return await _execute_complete(
            self,
            model_key=model_key,
            provider=provider,
            exec_req=exec_req,
            model_id=entry.model_id,
            provider_name=entry.provider,
            entry=entry,
            active_path=path,
            active_reason=reason,
            active_triggers=list(overflow_triggers),
            overflow_triggers=overflow_triggers,
            req=req,
            ctx=ctx,
            max_attempts=_MAX_ATTEMPTS,
            fallback_max_attempts=_ROLE_FALLBACK_MAX_ATTEMPTS,
            auth_retry_delay_s=_AUTH_RETRY_DELAY_S,
            backoff_base_s=_RETRY_BACKOFF_BASE_S,
        )

    # -- stream (§3.2) — single resolved route, no mid-stream escalation ------

    async def stream_complete(
        self, req: CompletionRequest, *, context: CallContext | None = None
    ) -> AsyncIterator[StreamChunk]:
        ctx = context or CallContext()
        model_key, entry, path, reason, overflow_triggers = self._resolve(req, ctx)
        self._enforce_hard_budget(req, entry, path, ctx)
        provider = self._provider_for(entry)
        exec_req = self._inject_prompt(self._attach_context_metadata(req, ctx), entry)
        async for chunk in _execute_stream_complete(
            self,
            model_key=model_key,
            provider=provider,
            exec_req=exec_req,
            model_id=entry.model_id,
            provider_name=entry.provider,
            entry=entry,
            active_path=path,
            active_reason=reason,
            active_triggers=list(overflow_triggers),
            req=req,
            ctx=ctx,
            max_attempts=_MAX_ATTEMPTS,
            fallback_max_attempts=_ROLE_FALLBACK_MAX_ATTEMPTS,
            auth_retry_delay_s=_AUTH_RETRY_DELAY_S,
            backoff_base_s=_RETRY_BACKOFF_BASE_S,
        ):
            yield chunk

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _effective_requirements(req: CompletionRequest) -> frozenset[Requirement]:
        requirements = set(req.profile.requirements)
        if any(getattr(message, "images", None) for message in req.messages):
            requirements.add(Requirement.VISION)
        return frozenset(requirements)

    def _role_fallback_target(
        self,
        role: ModelRole,
        *,
        requirements: frozenset[Requirement] = frozenset(),
    ) -> tuple[ModelProvider, str] | None:
        from ._router_execution import _role_fallback_target as _rft

        return _rft(
            self._config,
            self._providers,
            role,
            requirements=requirements,
            role_fallback_provider_key=ROLE_FALLBACK_PROVIDER_KEY,
            driver_roles=DRIVER_ROLES,
            fallback_eligible_roles=FALLBACK_ELIGIBLE_ROLES,
        )

    def _record_failure(
        self,
        req: CompletionRequest,
        entry: ModelEntry | None,
        path: Path,
        reason: str,
        exc: Exception | None,
        *,
        chosen_model: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        from ._router_execution import _record_failure as _rf

        _rf(
            self._sink,
            req,
            entry,
            path,
            reason,
            exc,
            chosen_model=chosen_model,
            provider_name=provider_name,
        )


# === INTELLIGENT ROUTING (DORMANT) — overflow resolution + escalation =========
# Preserved from v1.1 as the revival path (contract v1.2 appendix). The v1.1
# `_resolve` consulted the OverflowPolicy, applied cost governance to suppress or
# force overflow, and defensively forced overflow on a capability gap; `complete`
# ran a retry/ESCALATION loop that promoted local→overflow on transient local
# failure (honoring the hard cap). v1.2 replaced both with the deterministic
# bodies above. To revive: restore policy.py + config.py's dormant fields, re-add
# the `policy`/`DISCRETIONARY_TRIGGERS`/`OverflowSignal` imports and the `policy`
# __init__ param, and swap these bodies back in.
#
# def _resolve(self, req, ctx) -> tuple[ModelEntry, Path, list[str]]:
#     profile = req.profile
#     routing = self._config.roles.get(profile.role)
#     if routing is None:
#         raise LLMError(f"role {profile.role} not configured in RouterConfig")
#     primary = self._config.models[routing.primary]
#     overflow = self._config.models[routing.overflow] if routing.overflow else None
#     reqs = profile.requirements
#     primary_ok = reqs.issubset(primary.capabilities)
#     overflow_ok = overflow is not None and reqs.issubset(overflow.capabilities)
#     if not primary_ok and not overflow_ok:
#         self._sink.record(RoutingDecision(profile=profile, chosen_model="",
#             provider="", path="local", reason="no eligible model for requirements",
#             overflow_triggers=["no_eligible_model"]))
#         raise NoEligibleModel(...)
#     signal = OverflowSignal(difficulty=profile.difficulty,
#         consecutive_tool_errors=ctx.consecutive_tool_errors,
#         last_local_confidence=ctx.last_local_confidence,
#         local_attempts_failed=0, requires=reqs)
#     path, triggers = self._policy.decide(profile, signal, config=self._config)
#     if path == "local" and not primary_ok and overflow_ok:
#         path = "overflow"
#         if "capability_gap" not in triggers: triggers.append("capability_gap")
#     soft, hard = _cap_state(self._cost.conversation_cost(ctx.conversation_id),
#         self._cost.global_cost(), self._config)
#     if path == "overflow":
#         necessity = [t for t in triggers if t not in DISCRETIONARY_TRIGGERS]
#         if hard:
#             if necessity or not primary_ok:
#                 self._sink.record(RoutingDecision(... "hard budget cap reached; "
#                     "overflow disabled" ...))
#                 raise BudgetExceeded("hard cost cap reached; overflow disabled")
#             path, triggers = "local", []
#         elif soft:
#             triggers = necessity
#             if not triggers and primary_ok: path = "local"
#     chosen = overflow if (path == "overflow" and overflow is not None) else primary
#     return chosen, path, triggers
#
# # complete()'s escalation loop (replaced by the same-model retry above):
# #   on LLMTransientError while path == "local":
# #       local_failed += 1
# #       if role_overflow_eligible and overflow_ok and local_failed >=
# #          config.local_retry_before_overflow:
# #           (honor hard cap, else) entry, path = overflow_entry, "overflow"
# #           triggers.append("local_failed"); attempt += 1; continue
# #   else if attempt >= _MAX_ATTEMPTS: raise   (same-model retry kept in v1.2)
# === END DORMANT ==============================================================
