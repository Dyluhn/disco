"""The Router — llm-router-contract.md §4 (+ §5.2 cost governance, §5.3 escalation).

The single entry point callers use. It owns: profile→model resolution, the
overflow decision (delegated to an OverflowPolicy), cost governance, prompt-
variant injection, retries/escalation, and emitting the RoutingDecision.

Guarantees (RT1–RT4) are upheld by `DefaultLLMRouter`:
  RT1 complete() returns a CompletionResponse with .routing and .usage
      populated, or raises a typed LLMError.
  RT2 the model is selected only from profile + config + policy; callers
      cannot pin a model name (no name parameter exists).
  RT3 the matching prompt variant is injected when the caller supplied no
      system message.
  RT4 every call emits exactly one RoutingDecision (success or terminal
      failure) to the sink.

[EXTENSION] `CallContext`: the contract defines OverflowSignal inputs (§5) and
per-conversation cost (§5.2) but the §4 `complete(req)` entry point carries only
the request. `complete`/`stream_complete` take an optional keyword-only
`context: CallContext` to thread the loop's runtime signals and the conversation
id. This adds no model-name parameter, so RT2 is preserved, and callers using
`complete(req)` alone are unaffected.
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
    LLMError,
    LLMTransientError,
    NoEligibleModel,
)
from .policy import (
    DISCRETIONARY_TRIGGERS,
    OverflowPolicy,
    OverflowSignal,
    ThresholdOverflowPolicy,
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
    RoutingDecision,
    StreamChunk,
)

# Cap on attempts within a single call (retries + escalations). The loop's
# max_iterations ceiling (event contract) is the ultimate backstop.
_MAX_ATTEMPTS = 5

# The two routing paths; used wherever a RoutingDecision.path is built so the
# value stays a Literal (ty-checked) rather than a bare str.
Path = Literal["local", "overflow"]


class CallContext(BaseModel):
    """[EXTENSION] Optional per-call runtime context from the loop. See module
    docstring. `local_attempts_failed` is tracked by the router itself during
    escalation and is NOT part of this caller-supplied context."""

    model_config = ConfigDict(frozen=True)
    conversation_id: str | None = None
    consecutive_tool_errors: int = 0
    last_local_confidence: float | None = None


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


# ---- cost accounting (§5.2) -------------------------------------------------


class CostTracker:
    """Running cost, global and per-conversation. [INTERIOR] accounting store.

    `*_daily` global caps are modeled as a single running total in v1 (no day
    rollover); the door to a real time-windowed store is open behind this class.
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


def _cap_state(
    accrued_conv: float,
    accrued_global: float,
    config: RouterConfig,
) -> tuple[bool, bool]:
    """Return (soft_exceeded, hard_exceeded). A cap counts as exceeded when the
    accrued cost has reached it (>=)."""

    def hit(accrued: float, cap: float | None) -> bool:
        return cap is not None and accrued >= cap

    soft = hit(accrued_conv, config.soft_budget_usd_per_conversation) or hit(
        accrued_global, config.soft_budget_usd_global_daily
    )
    hard = hit(accrued_conv, config.hard_budget_usd_per_conversation) or hit(
        accrued_global, config.hard_budget_usd_global_daily
    )
    return soft, hard


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


def _reason(path: str, triggers: list[str], role_name: str) -> str:
    if path == "overflow":
        return f"overflow [{', '.join(triggers)}]" if triggers else "overflow"
    return f"local primary for {role_name}"


class DefaultLLMRouter:
    """[CONTRACT] The reference LLMRouter (RT1–RT4)."""

    def __init__(
        self,
        config: RouterConfig,
        providers: dict[str, ModelProvider],
        *,
        policy: OverflowPolicy | None = None,
        prompt_provider: PromptProvider | None = None,
        sink: RoutingSink | None = None,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        self._config = config
        self._providers = providers
        self._policy = policy or ThresholdOverflowPolicy()
        self._prompts = prompt_provider or StaticPromptProvider()
        self._sink = sink or NullRoutingSink()
        self._cost = cost_tracker or CostTracker()

    # -- resolution (§4.1 steps 1–4) + cost governance (§5.2) -----------------

    def _resolve(
        self, req: CompletionRequest, ctx: CallContext
    ) -> tuple[ModelEntry, Path, list[str]]:
        """Return (chosen_entry, path, triggers). Emits + raises on
        NoEligibleModel / hard-budget overflow before any provider is touched."""
        profile = req.profile
        routing = self._config.roles.get(profile.role)
        if routing is None:
            raise LLMError(f"role {profile.role} not configured in RouterConfig")

        primary = self._config.models[routing.primary]
        overflow = self._config.models[routing.overflow] if routing.overflow else None
        reqs = profile.requirements
        primary_ok = reqs.issubset(primary.capabilities)
        overflow_ok = overflow is not None and reqs.issubset(overflow.capabilities)

        # Step 2 — requirement filtering: no eligible model -> typed error (RT4
        # still emits one decision describing the terminal failure).
        if not primary_ok and not overflow_ok:
            self._sink.record(
                RoutingDecision(
                    profile=profile,
                    chosen_model="",
                    provider="",
                    path="local",
                    reason="no eligible model for requirements",
                    overflow_triggers=["no_eligible_model"],
                )
            )
            raise NoEligibleModel(
                f"no model satisfies {sorted(r.value for r in reqs)} for role {profile.role}"
            )

        # Step 3 — overflow policy.
        signal = OverflowSignal(
            difficulty=profile.difficulty,
            consecutive_tool_errors=ctx.consecutive_tool_errors,
            last_local_confidence=ctx.last_local_confidence,
            local_attempts_failed=0,
            requires=reqs,
        )
        path, triggers = self._policy.decide(profile, signal, config=self._config)

        # Defensive: if the local primary can't satisfy requirements, the route
        # must be overflow (rule 1 should have ensured this).
        if path == "local" and not primary_ok and overflow_ok:
            path = "overflow"
            if "capability_gap" not in triggers:
                triggers.append("capability_gap")

        # Cost governance (§5.2), applied around the policy decision.
        soft, hard = _cap_state(
            self._cost.conversation_cost(ctx.conversation_id),
            self._cost.global_cost(),
            self._config,
        )
        if path == "overflow":
            necessity = [t for t in triggers if t not in DISCRETIONARY_TRIGGERS]
            if hard:
                # Hard cap: overflow disabled. If overflow was only discretionary
                # and local can serve, fall back to local; otherwise it's a hard
                # block.
                if necessity or not primary_ok:
                    self._sink.record(
                        RoutingDecision(
                            profile=profile,
                            chosen_model=(overflow.model_id if overflow else ""),
                            provider=(overflow.provider if overflow else ""),
                            path="overflow",
                            reason="hard budget cap reached; overflow disabled",
                            overflow_triggers=triggers,
                        )
                    )
                    raise BudgetExceeded("hard cost cap reached; overflow disabled")
                path, triggers = "local", []
            elif soft:
                # Soft cap: suppress discretionary triggers; keep necessity.
                triggers = necessity
                if not triggers and primary_ok:
                    path = "local"

        chosen = overflow if (path == "overflow" and overflow is not None) else primary
        return chosen, path, triggers

    # -- prompt injection (RT3, §8) -------------------------------------------

    def _inject_prompt(self, req: CompletionRequest, entry: ModelEntry) -> CompletionRequest:
        if req.messages and req.messages[0].role == "system":
            return req  # caller supplied its own system prompt; do not override
        family = entry.family or derive_family(entry.model_id)
        mode = req.profile.mode or default_mode_for_role(req.profile.role)
        system = self._prompts.system_prompt(model_family=family, mode=mode, role=req.profile.role)
        new_messages = [LLMMessage(role="system", content=system), *req.messages]
        return req.model_copy(update={"messages": new_messages})

    # -- complete (§4.1 step 5–6, §5.3 retry/escalation) ----------------------

    async def complete(
        self, req: CompletionRequest, *, context: CallContext | None = None
    ) -> CompletionResponse:
        ctx = context or CallContext()
        entry, path, triggers = self._resolve(req, ctx)
        routing_cfg = self._config.roles[req.profile.role]
        overflow_entry = self._config.models[routing_cfg.overflow] if routing_cfg.overflow else None
        overflow_ok = overflow_entry is not None and req.profile.requirements.issubset(
            overflow_entry.capabilities
        )
        role_overflow_eligible = routing_cfg.overflow_eligible

        attempt = 1
        local_failed = 0
        while True:
            provider = self._providers[entry.provider]
            exec_req = self._inject_prompt(req, entry)
            try:
                resp = await provider.complete(exec_req, model=entry.model_id)
            except (LLMContextWindowExceeded, LLMAuthError, LLMContentFiltered) as exc:
                # Terminal: no retry. Emit one decision, then propagate.
                self._record_failure(req, entry, path, triggers, attempt, exc)
                raise
            except LLMTransientError:
                if path == "local":
                    local_failed += 1
                    can_escalate = (
                        role_overflow_eligible
                        and overflow_ok
                        and overflow_entry is not None
                        and local_failed >= self._config.local_retry_before_overflow
                    )
                    if can_escalate:
                        # Escalate local -> overflow (§5.3), honoring hard cap.
                        _, hard = _cap_state(
                            self._cost.conversation_cost(ctx.conversation_id),
                            self._cost.global_cost(),
                            self._config,
                        )
                        if hard:
                            self._record_failure(
                                req, overflow_entry, "overflow", triggers, attempt, None
                            )
                            raise BudgetExceeded("hard cost cap reached; cannot escalate") from None
                        entry, path = overflow_entry, "overflow"
                        if "local_failed" not in triggers:
                            triggers.append("local_failed")
                        attempt += 1
                        continue
                if attempt >= _MAX_ATTEMPTS:
                    self._record_failure(req, entry, path, triggers, attempt, None)
                    raise
                attempt += 1
                continue
            else:
                decision = RoutingDecision(
                    profile=req.profile,
                    chosen_model=entry.model_id,
                    provider=entry.provider,
                    path=path,
                    reason=_reason(path, triggers, req.profile.role.value),
                    overflow_triggers=triggers,
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
        entry, path, triggers = self._resolve(req, ctx)
        provider = self._providers[entry.provider]
        exec_req = self._inject_prompt(req, entry)
        decision = RoutingDecision(
            profile=req.profile,
            chosen_model=entry.model_id,
            provider=entry.provider,
            path=path,
            reason=_reason(path, triggers, req.profile.role.value),
            overflow_triggers=triggers,
        )
        emitted = False
        async for chunk in provider.stream_complete(exec_req, model=entry.model_id):
            if chunk.done and chunk.final is not None:
                final = chunk.final.model_copy(update={"routing": decision})
                if not emitted:
                    self._sink.record(decision)
                    self._cost.add(final.usage.cost_usd, ctx.conversation_id)
                    emitted = True
                yield chunk.model_copy(update={"final": final})
            else:
                yield chunk

    # -- helpers --------------------------------------------------------------

    def _record_failure(
        self,
        req: CompletionRequest,
        entry: ModelEntry | None,
        path: Path,
        triggers: list[str],
        attempt: int,
        exc: Exception | None,
    ) -> None:
        self._sink.record(
            RoutingDecision(
                profile=req.profile,
                chosen_model=(entry.model_id if entry else ""),
                provider=(entry.provider if entry else ""),
                path=path,
                reason=f"terminal failure: {type(exc).__name__ if exc else 'retries exhausted'}",
                overflow_triggers=triggers,
                attempt=attempt,
            )
        )
