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

from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..events import LLMMessage
from .config import ModelEntry, RouterConfig
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
from .types import (
    CompletionRequest,
    CompletionResponse,
    ModelRole,
    Requirement,
    RoutingDecision,
    StreamChunk,
)

# Cap on same-model transient retries within a single call. The loop's
# max_iterations ceiling (event contract) is the ultimate backstop. v1.2: this is
# pure resilience (retry the SAME assigned model), never model escalation.
_MAX_ATTEMPTS = 5

# The routing paths. v1.2 emits "pinned"/"manual"; "local"/"overflow" are kept in
# the Literal for the DORMANT intelligent-routing revival path and for back-compat
# of any persisted decisions.
Path = Literal["local", "overflow", "pinned", "manual"]


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

    # -- resolution (v1.2: direct config lookup, no policy) -------------------

    def _resolve(
        self, req: CompletionRequest, ctx: CallContext
    ) -> tuple[ModelEntry, Path, str, list[str]]:
        """Deterministic role→model resolution. Returns (entry, path, reason,
        overflow_triggers).

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

        # [BP-00] Vision capability guard: if the request carries images, the
        # model MUST support VISION. DF-08: when the primary lacks vision,
        # escalate to the configured vision_escalation_model instead of raising.
        if any(getattr(m, "images", None) for m in req.messages):
            if Requirement.VISION not in (entry.capabilities or []):
                escalated = self._try_vision_escalation(profile, key, entry, path)
                if escalated is not None:
                    return escalated
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
                    f"Check PMX_DRIVER_VISION env and driver-local config."
                )

        return entry, path, "config", []

    # -- vision escalation (DF-08) ---------------------------------------------

    def _try_vision_escalation(
        self, profile, key: str, entry: ModelEntry, original_path: Path
    ) -> tuple[ModelEntry, Path, str, list[str]] | None:
        """DF-08: escalate an image-bearing request to the configured vision
        model when the primary lacks VISION. Returns (entry, path, reason,
        overflow_triggers) or None if escalation is not possible (guard stays
        hard). Does NOT emit to the sink — the caller (complete) owns the one
        and only RoutingDecision per call (RT4)."""
        target_key = self._config.vision_escalation_model
        if target_key is None:
            return None
        target = self._config.models.get(target_key)
        if target is None:
            return None
        if Requirement.VISION not in (target.capabilities or []):
            return None
        return target, "overflow", f"vision escalation: {key} lacks VISION → {target_key}", ["vision_escalation"]

    # -- prompt injection (RT3, §8) -------------------------------------------

    def _inject_prompt(self, req: CompletionRequest, entry: ModelEntry) -> CompletionRequest:
        if req.messages and req.messages[0].role == "system":
            return req  # caller supplied its own system prompt; do not override
        family = entry.family or derive_family(entry.model_id)
        mode = req.profile.mode or default_mode_for_role(req.profile.role)
        system = self._prompts.system_prompt(
            model_family=family, mode=mode, role=req.profile.role, capabilities=entry.capabilities
        )
        new_messages = [LLMMessage(role="system", content=system), *req.messages]
        return req.model_copy(update={"messages": new_messages})

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

    # -- complete (§4.1 step 5–6) — one assigned model, same-model retry ------

    async def complete(
        self, req: CompletionRequest, *, context: CallContext | None = None
    ) -> CompletionResponse:
        ctx = context or CallContext()
        entry, path, reason, overflow_triggers = self._resolve(req, ctx)
        self._enforce_hard_budget(req, entry, path, ctx)
        exec_req = self._inject_prompt(req, entry)
        provider = self._providers[entry.provider]

        attempt = 1
        while True:
            try:
                resp = await provider.complete(exec_req, model=entry.model_id)
            except (LLMContextWindowExceeded, LLMAuthError, LLMContentFiltered) as exc:
                # Terminal: no retry, no escalation. Emit one decision, propagate.
                self._record_failure(req, entry, path, reason, exc)
                raise
            except LLMTransientError:
                # Resilience only: retry the SAME assigned model (never switch).
                if attempt >= _MAX_ATTEMPTS:
                    self._record_failure(req, entry, path, reason, None)
                    raise
                attempt += 1
                continue
            else:
                decision = RoutingDecision(
                    profile=req.profile,
                    chosen_model=entry.model_id,
                    provider=entry.provider,
                    path=path,
                    reason=reason,
                    overflow_triggers=list(overflow_triggers),
                    attempt=attempt,
                )
                self._sink.record(decision)
                self._cost.add(resp.usage.cost_usd, ctx.conversation_id)
                return resp.model_copy(update={"routing": decision})

    # -- stream (§3.2) — single resolved route, no mid-stream escalation ------

    async def stream_complete(
        self, req: CompletionRequest, *, context: CallContext | None = None
    ) -> AsyncIterator[StreamChunk]:
        ctx = context or CallContext()
        entry, path, reason, overflow_triggers = self._resolve(req, ctx)
        self._enforce_hard_budget(req, entry, path, ctx)
        provider = self._providers[entry.provider]
        exec_req = self._inject_prompt(req, entry)

        # Same-model transient retry as `complete` — but GUARDED: a stream can only
        # be restarted while NOTHING has reached the consumer yet. Once a chunk is
        # yielded downstream (the watch-it-write body lands in the UI) we cannot
        # un-emit it, so replaying would duplicate content. A transient that fires
        # before the first chunk → retry; mid-stream → propagate (the loop surfaces
        # it as an AgentErrorEvent and the model retries the action on its next turn).
        attempt = 1
        while True:
            decision = RoutingDecision(
                profile=req.profile,
                chosen_model=entry.model_id,
                provider=entry.provider,
                path=path,
                reason=reason,
                overflow_triggers=list(overflow_triggers),
                attempt=attempt,
            )
            yielded_any = False
            recorded = False
            try:
                async for chunk in provider.stream_complete(exec_req, model=entry.model_id):
                    if chunk.done and chunk.final is not None:
                        final = chunk.final.model_copy(update={"routing": decision})
                        if not recorded:
                            self._sink.record(decision)
                            self._cost.add(final.usage.cost_usd, ctx.conversation_id)
                            recorded = True
                        yielded_any = True
                        yield chunk.model_copy(update={"final": final})
                    else:
                        yielded_any = True
                        yield chunk
                return  # stream completed cleanly
            except (LLMContextWindowExceeded, LLMAuthError, LLMContentFiltered) as exc:
                self._record_failure(req, entry, path, reason, exc)
                raise
            except LLMTransientError:
                if yielded_any or attempt >= _MAX_ATTEMPTS:
                    self._record_failure(req, entry, path, reason, None)
                    raise
                attempt += 1
                continue

    # -- helpers --------------------------------------------------------------

    def _record_failure(
        self,
        req: CompletionRequest,
        entry: ModelEntry | None,
        path: Path,
        reason: str,
        exc: Exception | None,
    ) -> None:
        kind = type(exc).__name__ if exc else "retries exhausted"
        self._sink.record(
            RoutingDecision(
                profile=req.profile,
                chosen_model=(entry.model_id if entry else ""),
                provider=(entry.provider if entry else ""),
                path=path,
                reason=f"terminal failure: {kind} ({reason})",
                overflow_triggers=[],
            )
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
