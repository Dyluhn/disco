"""The overflow policy — llm-router-contract.md §5 (OpenRouter from the start).

The live, tested decision layer: local vs overflow. Pluggable so the rule SET is
swappable, but the interface and the default rules are contractual. Cost
governance (§5.2) is applied by the router *around* this decision (the policy
stays pure of runtime cost state); this module owns rules 1–5 (§5.1).
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from .config import RouterConfig
from .types import CapabilityProfile, Difficulty, ModelRole, Requirement

# Discretionary triggers suppressed when the soft budget cap is exceeded (§5.2).
DISCRETIONARY_TRIGGERS = frozenset({"hard_step", "low_confidence"})

# Roles that default to local-only (never overflow) unless config opts in (§5.1).
_LOCAL_ONLY_ROLES = frozenset(
    {ModelRole.RAG_ANSWERER, ModelRole.QUERY_REWRITER, ModelRole.SUMMARIZER}
)


class OverflowSignal(BaseModel):
    """Runtime inputs the policy may use beyond the static profile."""

    model_config = ConfigDict(frozen=True)
    difficulty: Difficulty
    consecutive_tool_errors: int = 0  # from the loop (stuck-adjacent)
    last_local_confidence: float | None = None  # if the caller estimates one
    local_attempts_failed: int = 0  # local calls already failed this step
    requires: frozenset[Requirement] = frozenset()


@runtime_checkable
class OverflowPolicy(Protocol):
    """[CONTRACT] Decides local vs overflow and may request escalation.
    Returns (path, triggers)."""

    def decide(
        self, profile: CapabilityProfile, signal: OverflowSignal, *, config: RouterConfig
    ) -> tuple[Literal["local", "overflow"], list[str]]: ...


class ThresholdOverflowPolicy:
    """[CONTRACT] The default policy: route to overflow if ANY rule fires, else
    local. Thresholds come from config (none hardcoded)."""

    def decide(
        self, profile: CapabilityProfile, signal: OverflowSignal, *, config: RouterConfig
    ) -> tuple[Literal["local", "overflow"], list[str]]:
        triggers: list[str] = []
        role = profile.role
        routing = config.roles.get(role)
        role_overflow_eligible = bool(routing and routing.overflow_eligible)

        # Rule 1 — capability gap: a required capability the local primary lacks
        # but the overflow model has. Fires regardless of role eligibility
        # (it is a hard necessity), provided an overflow model exists.
        if signal.requires and routing is not None:
            local = config.models.get(routing.primary)
            overflow = config.models.get(routing.overflow) if routing.overflow else None
            local_caps = local.capabilities if local else frozenset()
            overflow_caps = overflow.capabilities if overflow else frozenset()
            gap = {r for r in signal.requires if r not in local_caps}
            if gap and gap.issubset(overflow_caps):
                triggers.append("capability_gap")

        # Roles that are local-only never take the discretionary/eligible rules.
        eligible = role_overflow_eligible and role not in _LOCAL_ONLY_ROLES

        if eligible:
            # Rule 2 — hard step on a routing-eligible role (discretionary).
            if signal.difficulty == Difficulty.HARD:
                triggers.append("hard_step")
            # Rule 3 — local failure escalation.
            if signal.local_attempts_failed >= config.local_retry_before_overflow:
                triggers.append("local_failed")
            # Rule 4 — stuck recovery (hand a struggling step to a stronger model).
            if signal.consecutive_tool_errors >= config.errors_before_overflow:
                triggers.append("stuck_recovery")
            # Rule 5 — low confidence (discretionary).
            if (
                signal.last_local_confidence is not None
                and signal.last_local_confidence < config.confidence_floor
            ):
                triggers.append("low_confidence")

        path: Literal["local", "overflow"] = "overflow" if triggers else "local"
        return path, triggers
